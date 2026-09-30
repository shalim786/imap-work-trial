import unittest
from datetime import datetime, timezone
from agentmail_imap.protocol import *
class ProtocolTests(unittest.TestCase):
    def test_tokenizer(self):
        self.assertEqual(tokenize('A FETCH 1 (BODY.PEEK[HEADER.FIELDS (DATE SUBJECT)] UID)'),['A','FETCH','1',['BODY.PEEK[HEADER.FIELDS (DATE SUBJECT)]','UID']])
        self.assertEqual(tokenize('"a \\"b" (one (two))'),['a "b',['one',['two']]])
    def test_sets(self):
        self.assertEqual(parse_sequence_set('99:*,2:1,2',[1,2,4,9]),[1,2,9])
        self.assertEqual(parse_sequence_set('1:4294967295',[2,17]),[2,17])
        for bad in ('0','1:2:3','1,','-1'):
            with self.assertRaises(ProtocolError): parse_sequence_set(bad,[1])
    def test_fetch(self):
        attrs=parse_fetch_attributes('(UID BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)]<1.5> BODY[1.MIME])')
        self.assertEqual(attrs[1].response_name,'BODY[HEADER.FIELDS (SUBJECT DATE)]<1>')
        self.assertFalse(attrs[1].sets_seen); self.assertTrue(attrs[2].sets_seen)
        self.assertEqual(len(parse_fetch_attributes('FULL')),5)
    def test_search(self):
        m=SearchMessage(2,9,{'\\Seen'},datetime(2026,9,29,tzinfo=timezone.utc),100,b'Subject: Hello\r\nDate: Mon, 28 Sep 2026 10:00:00 +0000\r\n\r\nWorld',3,12)
        self.assertTrue(parse_search('OR UNSEEN (SEEN SUBJECT "hello") UID 9:12 SENTBEFORE 29-Sep-2026').evaluate(m))
        self.assertFalse(parse_search('NOT BODY "world"').evaluate(m))
        self.assertFalse(parse_search('UID *').evaluate(m))
        with self.assertRaises(UnsupportedCharset): parse_search('CHARSET KOI8-R ALL')

    def test_search_dates_flags_and_decoding(self):
        raw=b'Subject: =?utf-8?b?w6lsYW4=?=\r\nDate: 30 Sep 2026 10:00:00 +1400\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\nw6lsYW4='
        m=SearchMessage(1,5,{'\\Recent','\\Flagged'},datetime(2026,9,29,tzinfo=timezone.utc),len(raw),raw,1,5)
        for criteria in ('NEW FLAGGED UNSEEN','ON 29-Sep-2026 SENTON 30-Sep-2026','CHARSET UTF-8 SUBJECT "élan"','CHARSET UTF-8 BODY "élan"','CHARSET UTF-8 TEXT "élan"','HEADER Subject ""','1:* UID 9:*'):
            self.assertTrue(parse_search(criteria).evaluate(m),criteria)
        for criteria in ('OLD','BEFORE 29-Sep-2026','SENTBEFORE 30-Sep-2026','HEADER Missing ""','LARGER 9999'):
            self.assertFalse(parse_search(criteria).evaluate(m),criteria)
    def test_bad_syntax(self):
        for criteria in ('OR ALL','NOT','HEADER Subject','BEFORE nonsense','UNKNOWN'):
            with self.assertRaises(ProtocolError): parse_search(criteria)
        for attrs in ('BODY.PEEK[HEADER.FIELDS (SUBJECT)]<0.0>','BODY[0]','BODY[MIME]'):
            with self.assertRaises(ProtocolError): parse_fetch_attributes(attrs)
