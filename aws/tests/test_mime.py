import unittest
from agentmail_imap.mime import RawMessage
RAW=b'Subject: X\r\nX-Test: a\r\n folded\r\nX-Test: b\r\nContent-Type: multipart/mixed; boundary="edge"\r\n\r\npreamble\r\n--edge\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nhello\r\n--edge\r\nContent-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n\r\nYWJj\r\n--edge--\r\nepilogue'
class MimeTests(unittest.TestCase):
    def test_original(self):
        m=RawMessage(RAW); self.assertEqual(m.section(),RAW)
        self.assertEqual(m.section('1'),b'hello')
        self.assertEqual(m.section('2'),b'YWJj')
        self.assertEqual(m.section('1.MIME'),b'Content-Type: text/plain; charset=utf-8\r\n\r\n')
        self.assertEqual(m.section('HEADER.FIELDS (X-TEST)'),b'X-Test: a\r\n folded\r\nX-Test: b\r\n\r\n')
        self.assertEqual(m.section('',(2,3)),RAW[2:5]); self.assertIsNone(m.section('3'))
    def test_structure(self):
        structure=RawMessage(RAW).bodystructure()
        self.assertIn(b'"TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 5 0',structure)
        self.assertIn(b'"APPLICATION" "OCTET-STREAM" NIL NIL NIL "BASE64" 4',structure)
    def test_embedded(self):
        raw=b'Content-Type: message/rfc822\r\n\r\nSubject: nested\r\n\r\nbody\r\n'
        m=RawMessage(raw)
        self.assertEqual(m.section('1.HEADER'),b'Subject: nested\r\n\r\n')
        self.assertEqual(m.section('1.TEXT'),b'body\r\n')
        self.assertEqual(m.section('1.1'),b'body\r\n')

    def test_envelope_groups_and_sender_fallback(self):
        m=RawMessage(b'From: Alice <alice@example.test>\r\nTo: Team: Bob <bob@example.test>;\r\nSubject: demo\r\n\r\n')
        env=m.envelope()
        self.assertEqual(env.count(b'("Alice" NIL "alice" "example.test")'),3)
        self.assertIn(b'(NIL NIL "Team" NIL)',env)
        self.assertIn(b'("Bob" NIL "bob" "example.test") (NIL NIL NIL NIL)',env)
    def test_header_exclusion_and_utf8_octets(self):
        m=RawMessage('Subject: hi\r\nX-Test: yes\r\n\r\né雪'.encode())
        self.assertEqual(m.section('HEADER.FIELDS.NOT (X-Test)'),b'Subject: hi\r\n\r\n')
        self.assertEqual(m.section('TEXT',(1,3)),b'\xa9\xe9\x9b')
        self.assertIsNone(m.section('1.HEADER'))
    def test_nested_multipart(self):
        raw=b'Content-Type: multipart/mixed; boundary=a\r\n\r\n--a\r\nContent-Type: multipart/alternative; boundary=b\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\ninside\r\n--b--\r\n--a--\r\n'
        self.assertEqual(RawMessage(raw).section('1.1'),b'inside')
