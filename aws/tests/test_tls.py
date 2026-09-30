import asyncio
import imaplib
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from agentmail_imap.config import Config
from agentmail_imap.server import Server
from scripts.create_test_certificate import create_certificate
from scripts.smoke_test import Sandbox, INBOX, KEY, fixtures, bodies, ok

class TLSConfigTests(unittest.TestCase):
    def test_incomplete_or_ambiguous_config_fails(self):
        for values in ({'tls_mode':'invalid'}, {'tls_mode':'implicit'},
                       {'tls_mode':'implicit','tls_cert_file':Path('cert')},
                       {'tls_mode':'local','tls_key_file':Path('key')},
                       {'host':'0.0.0.0'}, {'tls_handshake_timeout':0}):
            with self.subTest(values=values), self.assertRaises(ValueError): Config(**values)
        self.assertEqual(Config(host='0.0.0.0',tls_mode='terminated').tls_mode,'terminated')

    def test_environment_tls_default_port(self):
        with patch.dict('os.environ', {'IMAP_TLS_MODE':'implicit','IMAP_TLS_CERT_FILE':'cert','IMAP_TLS_KEY_FILE':'key'},clear=True):
            self.assertEqual(Config.from_env('/nonexistent/env').port,1993)

class TLSStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_certificate_never_binds(self):
        config=Config(port=0,tls_mode='implicit',tls_cert_file=Path('/nonexistent/cert'),tls_key_file=Path('/nonexistent/key'))
        server=Server(config,None)
        with patch('asyncio.start_server',new_callable=AsyncMock) as start:
            with self.assertRaises(OSError): await server.start()
            start.assert_not_called()

class TLSIntegrationTests(unittest.TestCase):
    def test_verified_tls_login_raw_and_rejection(self):
        with Sandbox(refresh=100) as box:
            box.stop(box.server)
            cert,key=create_certificate(box.path/'tls')
            box.env.update(IMAP_TLS_MODE='implicit',IMAP_TLS_CERT_FILE=str(cert),IMAP_TLS_KEY_FILE=str(key),
                           IMAP_TLS_HANDSHAKE_TIMEOUT_SECONDS='1')
            box.start_server()
            context=ssl.create_default_context(cafile=str(cert))
            client=imaplib.IMAP4_SSL('127.0.0.1',box.port,ssl_context=context,timeout=5)
            box.client=client
            self.assertIn(client.sock.version(),('TLSv1.2','TLSv1.3'))
            ok(client.login(INBOX,KEY));self.assertEqual(ok(client.select())[0],b'3')
            expected={mid:raw for mid,raw in fixtures().items() if mid!='msg_new_arrival'}
            self.assertEqual(len(bodies(client,expected)),3)
            ok(client.uid('STORE','1','+FLAGS.SILENT','(\\Seen)'))
            self.assertNotIn(b'STARTTLS',b' '.join(ok(client.capability())))
            client.logout();box.client=None
            before=box.control("requests")["requests"]
            auth_before=sum(r["path"]=="/v0/auth/me" for r in before)
            with self.assertRaises(ssl.SSLCertVerificationError):
                imaplib.IMAP4_SSL('127.0.0.1',box.port,ssl_context=ssl.create_default_context(),timeout=5)
            with socket.create_connection(('127.0.0.1',box.port),timeout=5) as plain:
                plain.sendall(b'A LOGIN synthetic synthetic\r\n')
                try: data=plain.recv(1024)
                except ConnectionResetError: data=b''
                self.assertNotIn(b'* OK',data)
                self.assertNotIn(b'A OK',data)
            requests=box.control('requests')['requests']
            self.assertEqual(sum(r['path']=='/v0/auth/me' for r in requests),auth_before)
