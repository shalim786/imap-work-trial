"""Real-process acceptance tests using only a private synthetic API."""
import imaplib
import unittest
from unittest.mock import patch

from scripts.smoke_test import Sandbox, bodies, fixtures, ok, run_smoke


class IntegrationTests(unittest.TestCase):
    def test_success_path(self):
        run_smoke()

    def test_readonly_and_denied_mutations(self):
        expected = {mid: raw for mid, raw in fixtures().items() if mid != 'msg_new_arrival'}
        with Sandbox(refresh=30) as box:
            client, _ = box.connect(readonly=True)
            state = box.control('state')
            mapping = bodies(client, expected)
            uid = mapping['msg_received_ascii']
            ok(client.uid('FETCH', str(uid), '(BODY[])'))
            self.assertEqual(box.control('state'), state)
            self.assertEqual(client.uid('STORE', str(uid), '+FLAGS', '(\\Seen)')[0], 'NO')
            for method, arguments in [(client.copy, ('1', 'INBOX')), (client.create, ('Other',)),
                                      (client.delete, ('INBOX',)), (client.expunge, ())]:
                self.assertEqual(method(*arguments)[0], 'NO')
            self.assertEqual(box.control('state'), state)

    def test_later_raw_failure_emits_no_bodies(self):
        with Sandbox(refresh=30) as box:
            client, _ = box.connect()
            box.control('fail-next', {'method': 'GET', 'path_prefix': '/inboxes/candidate%40imap.test/messages/msg_received_utf8/raw', 'status': 503, 'times': 3})
            result, data = client.uid('FETCH', '1:*', '(BODY.PEEK[])')
            self.assertEqual(result, 'NO')
            self.assertFalse(any(isinstance(item, tuple) for item in data), 'failed batch leaked literal data')
            ok(client.noop())
            self.assertFalse([p for p in (box.path / 'raw').glob('**/*') if p.is_file() and p.name != 'owner.pid'])

    def test_login_does_not_list_and_terminal_noop_failure_recovers(self):
        with Sandbox(refresh=30) as box:
            client = imaplib.IMAP4('127.0.0.1', box.port, timeout=10)
            box.client = client
            ok(client.login('candidate@imap.test', 'test_agentmail_key'))
            requests = box.control('requests')['requests']
            self.assertFalse(any(row['path'].endswith('/messages') for row in requests))
            self.assertEqual(ok(client.select('INBOX')), [b'4'])
            expected = {mid: raw for mid, raw in fixtures().items() if mid != 'msg_new_arrival'}
            mapping = bodies(client, expected)
            box.control('fail-next', {'method': 'GET', 'path_prefix': '/inboxes/candidate%40imap.test/messages', 'status': 503, 'times': 3})
            result, data = client.noop()
            self.assertIn(result, ('OK', 'NO'))
            self.assertEqual(bodies(client, expected), mapping)
            ok(client.noop())

    def test_parallel_clients_share_uid_identity(self):
        expected = {mid: raw for mid, raw in fixtures().items() if mid != 'msg_new_arrival'}
        with Sandbox(refresh=30) as box:
            first, _ = box.connect()
            with imaplib.IMAP4('127.0.0.1', box.port, timeout=10) as second:
                ok(second.login('candidate@imap.test', 'test_agentmail_key'))
                ok(second.select('INBOX'))
                self.assertEqual(bodies(first, expected), bodies(second, expected))
                uid = next(iter(bodies(first, expected).values()))
                ok(first.uid('STORE', str(uid), '+FLAGS.SILENT', '(\\Seen)'))
                ok(second.noop())
                flags = ok(second.uid('FETCH', str(uid), '(FLAGS)'))
                self.assertIn(b'\\Seen', b' '.join(flags))


class RunnerCleanupTests(unittest.TestCase):
    def test_assertion_failure_reaps_owned_processes_and_workspace(self):
        box = Sandbox(refresh=30)
        with self.assertRaisesRegex(AssertionError, 'injected'):
            with box:
                raise AssertionError('injected')
        self.assertTrue(all(child.poll() is not None for child in box.children))
        self.assertFalse(box.path.exists())

    def test_configuration_refresh_default_is_sixty_seconds(self):
        from agentmail_imap.config import Config
        self.assertEqual(Config().refresh_interval, 60)

    def test_api_startup_failure_removes_workspace(self):
        box = Sandbox()
        with patch.object(box, 'spawn', side_effect=OSError('injected startup failure')):
            with self.assertRaises(OSError):
                box.__enter__()
        self.assertFalse(box.path.exists())

    def test_server_startup_failure_reaps_api(self):
        box = Sandbox()
        with patch.object(box, 'start_server', side_effect=OSError('injected server failure')):
            with self.assertRaises(OSError):
                box.__enter__()
        self.assertTrue(all(child.poll() is not None for child in box.children))
        self.assertFalse(box.path.exists())

class ProtocolAcceptanceTests(unittest.TestCase):
    def test_auth_plain_search_sections_and_partial_bytes(self):
        import base64
        expected = {mid: raw for mid, raw in fixtures().items() if mid != 'msg_new_arrival'}
        with Sandbox(refresh=30) as box:
            with imaplib.IMAP4('127.0.0.1', box.port, timeout=10) as client:
                ok(client.authenticate('PLAIN', lambda challenge: b'\0candidate@imap.test\0test_agentmail_key'))
                self.assertEqual(ok(client.select('INBOX')), [b'4'])
                mapping = bodies(client, expected)
                ascii_uid = mapping['msg_received_ascii']
                found = ok(client.uid('SEARCH', None, 'UNSEEN'))[0].split()
                self.assertEqual(set(map(int, found)), {uid for mid, uid in mapping.items() if mid != 'msg_received_utf8'})
                found = ok(client.uid('SEARCH', None, 'HEADER', 'Message-ID', 'ascii-001'))[0].split()
                self.assertEqual(list(map(int, found)), [ascii_uid])
                rows = ok(client.uid('FETCH', str(ascii_uid), '(UID BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID)])'))
                header = next(item[1] for item in rows if isinstance(item, tuple))
                self.assertEqual(header, b'Subject: A small deterministic message\r\nMessage-ID: <ascii-001@imap.test>\r\n\r\n')
                rows = ok(client.uid('FETCH', str(mapping['msg_received_utf8']), '(BODY.PEEK[]<300.31>)'))
                partial = next(item[1] for item in rows if isinstance(item, tuple))
                self.assertEqual(partial, expected['msg_received_utf8'][300:331])
                attachment_uid = mapping['msg_received_attachment']
                rows = ok(client.uid('FETCH', str(attachment_uid), '(BODY.PEEK[2])'))
                attachment = next(item[1] for item in rows if isinstance(item, tuple))
                self.assertEqual(base64.b64decode(attachment), b'hello from the harness\r\n')
                ok(client.uid('FETCH', str(attachment_uid), '(ENVELOPE BODYSTRUCTURE)'))
                self.assertEqual(client.uid('SEARCH', 'CHARSET', 'ISO-8859-1', 'ALL')[0], 'NO')

    def test_literal_login_with_raw_tcp_and_rejected_upload(self):
        import socket
        with Sandbox(refresh=30) as box:
            with socket.create_connection(('127.0.0.1', box.port), timeout=5) as sock:
                stream = sock.makefile('rb')
                self.assertTrue(stream.readline().startswith(b'* OK'))
                def exchange(command, tag):
                    sock.sendall(command)
                    lines = []
                    while True:
                        line = stream.readline()
                        self.assertTrue(line, 'unexpected disconnect')
                        lines.append(line)
                        if line.startswith(tag + b' '):
                            return lines
                sock.sendall(b'L1 LOGIN {19}\r\n')
                self.assertTrue(stream.readline().startswith(b'+'))
                sock.sendall(b'candidate@imap.test {18}\r\n')
                self.assertTrue(stream.readline().startswith(b'+'))
                lines = exchange(b'test_agentmail_key\r\n', b'L1')
                self.assertTrue(lines[-1].startswith(b'L1 OK'))
                lines = exchange(b'L2 SELECT INBOX\r\n', b'L2')
                self.assertIn(b'* 4 EXISTS\r\n', lines)
                lines = exchange(b'L3 APPEND INBOX {4}\r\n', b'L3')
                self.assertTrue(lines[-1].startswith(b'L3 NO'))
                lines = exchange(b'L4 NOOP\r\n', b'L4')
                self.assertTrue(lines[-1].startswith(b'L4 OK'))
                lines = exchange(b'L5 LOGOUT\r\n', b'L5')
                self.assertTrue(lines[-1].startswith(b'L5 OK'))
                stream.close()
