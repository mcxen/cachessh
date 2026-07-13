import base64
import logging
import os
import queue
import shlex
import socket
import threading

import paramiko

from webssh.worker import sessions


class AuthorizedKeys(object):
    def __init__(self, filename):
        self.filename = os.path.expanduser(filename) if filename else ''
        self.fingerprints = set()
        if self.filename:
            self.load()

    def load(self):
        with open(self.filename, 'r') as fp:
            for line in fp:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                fields = line.split()
                for index, value in enumerate(fields):
                    if value.startswith(('ssh-', 'ecdsa-', 'sk-')):
                        try:
                            key = paramiko.PKey.from_type_string(
                                value, base64.b64decode(fields[index + 1])
                            )
                        except (IndexError, ValueError, paramiko.SSHException):
                            logging.warning('Ignoring invalid authorized key')
                        else:
                            self.fingerprints.add(key.get_fingerprint())
                        break

    def contains(self, key):
        return key.get_fingerprint() in self.fingerprints


class GatewayServer(paramiko.ServerInterface):
    def __init__(self, authorized_keys):
        self.authorized_keys = authorized_keys
        self.ready = threading.Event()
        self.command = None
        self.term = 'xterm-256color'
        self.width = 80
        self.height = 24
        self.attachment = None
        self.authenticated_token = None

    def get_allowed_auths(self, username):
        return 'publickey,password'

    def check_auth_password(self, username, password):
        worker = sessions.get(password)
        if worker and not worker.closed:
            self.authenticated_token = password
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        if self.authorized_keys and self.authorized_keys.contains(key):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == 'session':
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height,
                                  pixelwidth, pixelheight, modes):
        self.term = term.decode('ascii', 'replace') \
            if isinstance(term, bytes) else term
        self.width = width
        self.height = height
        return True

    def check_channel_shell_request(self, channel):
        self.ready.set()
        return True

    def check_channel_exec_request(self, channel, command):
        self.command = command.decode('utf-8', 'replace') \
            if isinstance(command, bytes) else command
        self.ready.set()
        return True

    def check_channel_window_change_request(self, channel, width, height,
                                            pixelwidth, pixelheight):
        attachment = self.attachment
        if attachment:
            attachment.resize(width, height)
        else:
            self.width = width
            self.height = height
        return True


class SSHAttachment(object):
    """Make a Paramiko server channel look like a web handler."""
    def __init__(self, channel, worker, loop, src_addr):
        self.channel = channel
        self.worker = worker
        self.loop = loop
        self.src_addr = src_addr
        self.messages = queue.Queue()
        self.closed = False
        self.writer = threading.Thread(target=self._write_messages)
        self.writer.daemon = True
        self.writer.start()

    def write_message(self, data, binary=False):
        if not self.closed:
            self.messages.put(data)

    def _write_messages(self):
        while True:
            data = self.messages.get()
            if data is None:
                return
            try:
                self.channel.sendall(data)
            except (EOFError, OSError, socket.error):
                self.close(reason='SSH client disconnected')
                return

    def resize(self, cols, rows):
        self.loop.add_callback(self.worker.resize, cols, rows)

    def close(self, reason=None):
        if self.closed:
            return
        self.closed = True
        self.messages.put(None)
        try:
            self.channel.close()
        except (EOFError, OSError, socket.error):
            pass


class SSHGateway(object):
    def __init__(self, loop, address, port, host_key_file,
                 authorized_keys_file):
        self.loop = loop
        self.address = address
        self.port = port
        self.host_key_file = os.path.expanduser(host_key_file)
        self.authorized_keys = AuthorizedKeys(authorized_keys_file)
        self.host_key = self.load_or_create_host_key()
        self.sock = None

    def load_or_create_host_key(self):
        filename = self.host_key_file
        if os.path.exists(filename):
            return paramiko.RSAKey.from_private_key_file(filename)
        dirname = os.path.dirname(filename)
        if dirname and not os.path.isdir(dirname):
            os.makedirs(dirname)
        key = paramiko.RSAKey.generate(3072)
        key.write_private_key_file(filename)
        os.chmod(filename, 0o600)
        return key

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.address, self.port))
        self.sock.listen(20)
        thread = threading.Thread(target=self._accept)
        thread.daemon = True
        thread.start()
        logging.info('CacheSSH gateway listening on %s:%s',
                     self.address or '0.0.0.0', self.port)
        return self

    def _accept(self):
        while True:
            try:
                client, addr = self.sock.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._serve, args=(client, addr))
            thread.daemon = True
            thread.start()

    def _serve(self, client, addr):
        transport = paramiko.Transport(client)
        transport.add_server_key(self.host_key)
        server = GatewayServer(self.authorized_keys)
        try:
            transport.start_server(server=server)
            channel = transport.accept(15)
            if channel is None or not server.ready.wait(15):
                return
            token = server.authenticated_token or self._get_token(
                channel, server.command
            )
            worker = sessions.get(token)
            if not worker or worker.closed:
                channel.sendall(b'\r\nSession not found.\r\n')
                return
            attachment = SSHAttachment(channel, worker, self.loop, addr)
            server.attachment = attachment
            self.loop.add_callback(self._attach, worker, attachment,
                                   server.width, server.height)
            while transport.is_active() and not attachment.closed:
                data = channel.recv(32768)
                if not data:
                    break
                text = data.decode('utf-8', 'replace')
                self.loop.add_callback(worker.write_input, text)
            self.loop.add_callback(worker.detach_handler, attachment)
            attachment.close()
        except (EOFError, OSError, socket.error, paramiko.SSHException):
            logging.exception('SSH gateway connection failed from %s:%s',
                              *addr)
        finally:
            transport.close()

    def _attach(self, worker, attachment, width, height):
        worker.set_handler(attachment)
        worker.resize(width, height)

    def _get_token(self, channel, command):
        if command:
            try:
                args = shlex.split(command)
            except ValueError:
                args = []
            if len(args) == 2 and args[0] == 'attach':
                return args[1]
            channel.sendall(b'Usage: attach <token>\r\n')
            return ''

        channel.sendall(b'CacheSSH token: ')
        value = bytearray()
        while len(value) < 128:
            char = channel.recv(1)
            if not char or char in (b'\r', b'\n'):
                break
            value.extend(char)
        channel.sendall(b'\r\n')
        return bytes(value).decode('ascii', 'ignore').strip()
