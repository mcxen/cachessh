import os
import tempfile
import unittest

import paramiko

from webssh.gateway import AuthorizedKeys, GatewayServer, SSHGateway
from webssh.worker import sessions


class TestAuthorizedKeys(unittest.TestCase):
    def test_loads_public_key(self):
        key = paramiko.RSAKey.generate(1024)
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as fp:
            fp.write('{} {} test\n'.format(key.get_name(), key.get_base64()))
            filename = fp.name
        try:
            keys = AuthorizedKeys(filename)
            self.assertTrue(keys.contains(key))
            self.assertEqual(len(keys.fingerprints), 1)
        finally:
            os.unlink(filename)


class TestGateway(unittest.TestCase):
    def test_session_token_can_authenticate_as_password(self):
        class OpenWorker(object):
            closed = False

        token = 'user-chosen-password'
        sessions[token] = OpenWorker()
        try:
            server = GatewayServer(None)
            self.assertEqual(
                server.check_auth_password('cache', token),
                paramiko.AUTH_SUCCESSFUL
            )
            self.assertEqual(server.authenticated_token, token)
            self.assertEqual(
                server.check_auth_password('cache', 'wrong'),
                paramiko.AUTH_FAILED
            )
        finally:
            sessions.pop(token, None)

    def test_creates_and_reloads_host_key(self):
        with tempfile.TemporaryDirectory() as directory:
            authorized = os.path.join(directory, 'authorized_keys')
            host_key = os.path.join(directory, 'host_key')
            key = paramiko.RSAKey.generate(1024)
            with open(authorized, 'w') as fp:
                fp.write('{} {}\n'.format(
                    key.get_name(), key.get_base64()
                ))
            gateway = SSHGateway(None, '', 0, host_key, authorized)
            fingerprint = gateway.host_key.get_fingerprint()
            gateway2 = SSHGateway(None, '', 0, host_key, authorized)
            self.assertEqual(
                fingerprint, gateway2.host_key.get_fingerprint()
            )
            self.assertEqual(os.stat(host_key).st_mode & 0o777, 0o600)
