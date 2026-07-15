import logging
import time
from collections import deque
try:
    import secrets
except ImportError:
    secrets = None
import tornado.websocket
import paramiko

from uuid import uuid4
from tornado.ioloop import IOLoop
from tornado.iostream import _ERRNO_CONNRESET
from tornado.util import errno_from_exception


BUF_SIZE = 32 * 1024
clients = {}  # {ip: {id: worker}}
sessions = {}  # {token: worker}
session_history = deque(maxlen=200)  # recently closed sessions


def remember_worker(worker):
    if getattr(worker, 'history_saved', False):
        return
    worker.history_saved = True
    now = time.time()
    closed_at = getattr(worker, 'closed_at', None) or now
    created_at = getattr(worker, 'created_at', now)
    session_history.appendleft(dict(
        token=getattr(worker, 'token', '') or '',
        id=getattr(worker, 'id', '') or '',
        hostname=getattr(worker, 'hostname', '') or '',
        port=worker.dst_addr[1] if getattr(worker, 'dst_addr', None) else '',
        username=getattr(worker, 'username', '') or '',
        term=getattr(worker, 'term', '') or '',
        client='{}:{}'.format(*worker.src_addr)
        if hasattr(worker, 'src_addr') else '',
        created_at=created_at,
        closed_at=closed_at,
        lifetime_seconds=int(closed_at - created_at),
        output_cache_size=getattr(worker, 'output_cache_size', 0),
        reason=getattr(worker, 'close_reason', '') or ''
    ))


def clear_worker(worker, clients):
    remember_worker(worker)

    for ip, workers in list(clients.items()):
        if worker.id in workers:
            workers.pop(worker.id)

        if not workers:
            clients.pop(ip)

    token = getattr(worker, 'token', None)
    if token and sessions.get(token) is worker:
        sessions.pop(token)


def recycle_worker(worker):
    logging.debug('Worker {} kept alive for persistent session'.format(
        worker.id
    ))


class Worker(object):
    def __init__(self, loop, ssh, chan, dst_addr, token=None,
                 identity=None, hostname=None, username=None, term=None,
                 max_output_cache=256 * 1024):
        self.loop = loop
        self.ssh = ssh
        self.chan = chan
        self.dst_addr = dst_addr
        self.hostname = hostname or dst_addr[0]
        self.username = username
        self.term = term
        self.fd = chan.fileno()
        self.id = self.gen_id()
        self.token = token
        self.identity = identity
        self.created_at = time.time()
        self.attached_at = None
        self.detached_at = None
        self.data_to_dst = []
        self.output_cache = deque()
        self.output_cache_size = 0
        self.max_output_cache = max(0, max_output_cache)
        self.handler = None
        self.mode = IOLoop.READ
        self.closed = False
        self.registered = False
        self.closed_at = None
        self.close_reason = ''
        self.history_saved = False

    def __call__(self, fd, events):
        if events & IOLoop.READ:
            self.on_read()
        if events & IOLoop.WRITE:
            self.on_write()
        if events & IOLoop.ERROR:
            self.close(reason='error event occurred')

    @classmethod
    def gen_id(cls):
        return secrets.token_urlsafe(nbytes=32) if secrets else uuid4().hex

    def set_handler(self, handler):
        old_handler = self.handler
        self.handler = handler
        self.attached_at = time.time()
        self.detached_at = None

        if old_handler and old_handler is not handler:
            old_handler.close(reason='Session attached elsewhere')

        self.replay_output_cache()

    def detach_handler(self, handler):
        if self.handler is handler:
            self.handler = None
            self.detached_at = time.time()

    def add_loop_handler(self):
        if not self.registered:
            self.loop.add_handler(self.fd, self, IOLoop.READ)
            self.registered = True

    def remove_loop_handler(self):
        if self.registered:
            self.loop.remove_handler(self.fd)
            self.registered = False

    def cache_output(self, data):
        self.output_cache.append(data)
        self.output_cache_size += len(data)

        while self.output_cache_size > self.max_output_cache:
            discarded = self.output_cache.popleft()
            self.output_cache_size -= len(discarded)

    def replay_output_cache(self):
        for data in list(self.output_cache):
            if not self.handler:
                return
            try:
                self.handler.write_message(data, binary=True)
            except tornado.websocket.WebSocketClosedError:
                self.handler = None
                return

    def update_handler(self, mode):
        if self.mode != mode:
            self.loop.update_handler(self.fd, mode)
            self.mode = mode
        if mode == IOLoop.WRITE:
            self.loop.call_later(0.1, self, self.fd, IOLoop.WRITE)

    def on_read(self):
        logging.debug('worker {} on read'.format(self.id))
        try:
            data = self.chan.recv(BUF_SIZE)
        except (OSError, IOError) as e:
            logging.error(e)
            if self.chan.closed or errno_from_exception(e) in _ERRNO_CONNRESET:
                self.close(reason='chan error on reading')
        else:
            logging.debug('{!r} from {}:{}'.format(data, *self.dst_addr))
            if not data:
                self.close(reason='chan closed')
                return

            self.cache_output(data)

            try:
                handler = self.handler
                if handler:
                    logging.debug(
                        '{!r} to {}:{}'.format(data, *handler.src_addr)
                    )
                    handler.write_message(data, binary=True)
            except tornado.websocket.WebSocketClosedError:
                self.handler = None

    def on_write(self):
        logging.debug('worker {} on write'.format(self.id))
        if not self.data_to_dst:
            return

        data = ''.join(self.data_to_dst)
        logging.debug('{!r} to {}:{}'.format(data, *self.dst_addr))

        try:
            sent = self.chan.send(data)
        except (OSError, IOError) as e:
            logging.error(e)
            if self.chan.closed or errno_from_exception(e) in _ERRNO_CONNRESET:
                self.close(reason='chan error on writing')
            else:
                self.update_handler(IOLoop.WRITE)
        else:
            self.data_to_dst = []
            data = data[sent:]
            if data:
                self.data_to_dst.append(data)
                self.update_handler(IOLoop.WRITE)
            else:
                self.update_handler(IOLoop.READ)

    def write_input(self, data):
        """Write terminal input supplied by any attached frontend."""
        if self.closed or not data:
            return
        self.data_to_dst.append(data)
        self.on_write()

    def resize(self, cols, rows):
        """Resize the remote PTY for web and SSH frontends."""
        if self.closed:
            return
        try:
            self.chan.resize_pty(width=cols, height=rows)
        except (TypeError, OSError, IOError, paramiko.SSHException):
            logging.debug('Unable to resize worker %s', self.id)

    def close(self, reason=None):
        if self.closed:
            return
        self.closed = True
        self.closed_at = time.time()
        self.close_reason = reason or ''

        logging.info(
            'Closing worker {} with reason: {}'.format(self.id, reason)
        )
        if self.handler:
            self.handler.close(reason=reason)
            self.handler = None
        self.remove_loop_handler()
        try:
            self.chan.close()
        except (EOFError, OSError, IOError) as exc:
            logging.debug('Ignoring channel close error: {}'.format(exc))
        try:
            self.ssh.close()
        except (EOFError, OSError, IOError) as exc:
            logging.debug('Ignoring ssh close error: {}'.format(exc))
        logging.info('Connection to {}:{} lost'.format(*self.dst_addr))

        clear_worker(self, clients)
        logging.debug(clients)
