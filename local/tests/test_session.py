"""Failure and notification tests without sockets or upstream services."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from agentmail_imap.adapter import ApiError
from agentmail_imap.config import Config
from agentmail_imap.models import Message
from agentmail_imap.server import Server, Session, CommandError

RAW=b'Subject: original\r\n\r\ncontents\r\n'
class Writer:
    def __init__(self): self.data=bytearray(); self.closed=False
    def write(self,value): self.data.extend(value)
    async def drain(self): pass
    def close(self): self.closed=True
    def is_closing(self): return self.closed
    async def wait_closed(self): pass

class Store:
    def claim_recent(self,*args,**kwargs): return set()

class FakeAdapter:
    namespace='synthetic'
    def __init__(self):
        self.authorize=AsyncMock(); self.raw_info=AsyncMock(); self.close=AsyncMock()
        self.downloads=[]; self.fail_download=None
    async def download(self,inbox,message_id,path,**kwargs):
        self.downloads.append(message_id)
        if message_id==self.fail_download: raise ApiError()
        path.write_bytes(RAW)
    async def set_seen(self,*args): raise AssertionError('PEEK must not mutate')

class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.server=Server(Config(temp_dir=Path(self.directory.name),raw_cache_bytes=0),Store())
        self.addAsyncCleanup(self.server.close)
        self.writer=Writer(); self.session=Session(self.server,asyncio.StreamReader(),self.writer)
        self.session.adapter=FakeAdapter(); self.session.inbox='candidate'; self.session.key='test'; self.session.selected=True
        self.session.messages=[Message('a',uid=2,size=len(RAW)),Message('b',uid=5,size=len(RAW)),Message('c',uid=8,size=len(RAW))]
        self.session.uidvalidity=123; self.session.uidnext=9
    def output(self): return bytes(self.writer.data)
    def pending_removal(self): self.session.pending=(123,10,[self.session.messages[0],self.session.messages[2],Message('d',uid=9,size=len(RAW))])
    async def test_noop_reannounces_flags_even_when_session_snapshot_matches(self):
        async def refresh():
            self.session.pending=(123,9,list(self.session.messages))
        self.session.refresh=refresh
        await self.session.command('A','NOOP')
        self.assertIn(b'* 1 FETCH (UID 2 FLAGS (\\Seen))',self.output())
        self.assertEqual(self.output().count(b' FETCH ('),3)

    async def test_large_metadata_fetch_authorizes_once_and_does_not_download(self):
        self.session.messages=[Message(str(i),uid=i,size=len(RAW)) for i in range(1,514)]
        await self.session.command('A','UID FETCH 1:* (UID FLAGS RFC822.SIZE)')
        self.assertEqual(self.session.adapter.authorize.await_count,1)
        self.assertEqual(self.session.adapter.downloads,[])
        self.assertEqual(self.output().count(b' FETCH ('),513)
        self.assertTrue(self.output().endswith(b'A OK FETCH completed\r\n'))

    async def test_metadata_authorization_is_time_based_and_stops_at_revocation(self):
        for revoked in (False,True):
            with self.subTest(revoked=revoked):
                self.writer.data.clear()
                self.session.adapter.authorize=AsyncMock(side_effect=[None,ApiError(status=403,revoked=True)] if revoked else None)
                now=[100.0]
                async def drain(): now[0]+=31
                self.writer.drain=drain
                with patch('agentmail_imap.server.monotonic',side_effect=lambda:now[0]):
                    if revoked:
                        with self.assertRaises(ApiError): await self.session.command('A','UID FETCH 2:8 UID')
                    else:
                        await self.session.command('A','UID FETCH 2:8 UID')
                self.assertEqual(self.session.adapter.authorize.await_count,2)
                self.assertIn(b'* 1 FETCH',self.output())
                self.assertIn(b'* 2 FETCH',self.output())
                if revoked: self.assertNotIn(b'* 3 FETCH',self.output())
                else: self.assertIn(b'* 3 FETCH',self.output())

    async def test_idle_keeps_coherent_old_sequence_view(self):
        self.pending_removal(); await self.session.publish(False)
        self.assertEqual(self.output(),b''); self.assertEqual([m.uid for m in self.session.messages],[2,5,8]); self.assertIsNotNone(self.session.pending)
    async def test_ordinary_fetch_defers_expunge_and_uses_old_sequence(self):
        self.pending_removal(); await self.session.command('A','FETCH 2 UID')
        self.assertIn(b'* 2 FETCH (UID 5)',self.output()); self.assertNotIn(b'EXPUNGE',self.output())
        self.assertIsNotNone(self.session.pending)
    async def test_uid_fetch_can_publish_removal_before_new_sequence(self):
        self.pending_removal(); await self.session.command('A','UID FETCH 8 UID')
        self.assertEqual(self.output(),b'* 2 EXPUNGE\r\n* 3 EXISTS\r\n* 2 FETCH (UID 8)\r\nA OK FETCH completed\r\n')
        self.assertIsNone(self.session.pending)
    async def test_noop_delivers_queued_removal(self):
        self.pending_removal(); self.session.refresh=AsyncMock()
        await self.session.command('A','NOOP')
        self.assertTrue(self.output().startswith(b'* 2 EXPUNGE\r\n* 3 EXISTS\r\n'))
        self.assertIn(b'* 3 FETCH (UID 9 FLAGS (\\Seen))',self.output())
        self.assertTrue(self.output().endswith(b'A OK NOOP completed\r\n'))
    async def test_multiple_removals_descend(self):
        self.session.pending=(123,9,[self.session.messages[0]])
        await self.session.publish(True)
        self.assertEqual(self.output(),b'* 3 EXPUNGE\r\n* 2 EXPUNGE\r\n* 1 EXISTS\r\n')
    async def test_revoked_before_command_returns_no_message(self):
        self.session.adapter.authorize.side_effect=ApiError(revoked=True)
        with self.assertRaises(ApiError): await self.session.command('A','UID FETCH 2 BODY.PEEK[]')
        self.assertEqual(self.output(),b''); self.assertEqual(self.session.adapter.downloads,[])
    async def test_revoked_before_first_result_returns_no_message(self):
        self.session.adapter.authorize.side_effect=[None,ApiError(revoked=True)]
        with self.assertRaises(ApiError): await self.session.command('A','UID FETCH 2:8 BODY.PEEK[]')
        self.assertEqual(self.output(),b''); self.assertEqual(self.server.temp_bytes,0)
        self.assertEqual(list(Path(self.directory.name).iterdir()),[])
    async def test_later_preparation_failure_emits_zero_fetch(self):
        self.session.adapter.fail_download='c'
        with self.assertRaises(ApiError): await self.session.command('A','UID FETCH 2:8 BODY.PEEK[]')
        self.assertEqual(self.output(),b''); self.assertEqual(self.session.adapter.downloads,['a','b','c'])
        self.assertEqual(self.server.temp_bytes,0); self.assertEqual(list(Path(self.directory.name).iterdir()),[])
    async def test_batch_count_limit_splits_ordered_groups(self):
        self.server.config=replace(self.server.config,max_batch_messages=2)
        await self.session.command('A','UID FETCH 2:8 BODY.PEEK[]')
        self.assertEqual(self.session.adapter.downloads,['a','b','c'])
        self.assertLess(self.output().index(b'* 1 FETCH'),self.output().index(b'* 2 FETCH'))
        self.assertLess(self.output().index(b'* 2 FETCH'),self.output().index(b'* 3 FETCH'))
    async def test_byte_limits_release_partial_reservation(self):
        for setting,value in [('max_message_bytes',len(RAW)-1),('max_temp_bytes',len(RAW)+1)]:
            with self.subTest(setting=setting):
                self.server.config=replace(Config(temp_dir=Path(self.directory.name),raw_cache_bytes=0),**{setting:value})
                with self.assertRaises(CommandError): await self.session.command('A','UID FETCH 2:8 BODY.PEEK[]')
                self.assertEqual(self.output(),b''); self.assertEqual(self.server.temp_bytes,0)
                self.assertEqual(list(Path(self.directory.name).iterdir()),[])

    async def test_byte_limit_splits_groups_and_later_failure_keeps_only_prefix(self):
        self.server.config=replace(self.server.config,max_batch_bytes=len(RAW)+1)
        self.session.adapter.fail_download='b'
        with self.assertRaises(ApiError): await self.session.command('A','UID FETCH 2:8 BODY.PEEK[]')
        self.assertIn(b'* 1 FETCH',self.output())
        self.assertNotIn(b'* 2 FETCH',self.output())
        self.assertNotIn(b'* 3 FETCH',self.output())
        self.assertEqual(self.server.temp_bytes,0)

    async def test_parallel_preparation_never_reorders_results(self):
        original=self.session.adapter.download
        completed=[]
        async def download(inbox,identifier,path,**kwargs):
            if identifier=='a': await asyncio.sleep(0.02)
            await original(inbox,identifier,path,**kwargs)
            completed.append(identifier)
        self.session.adapter.download=download
        await self.session.command('A','UID FETCH 2:8 BODY.PEEK[]')
        self.assertEqual(completed,['b','c','a'])
        self.assertLess(self.output().index(b'* 1 FETCH'),self.output().index(b'* 2 FETCH'))
        self.assertLess(self.output().index(b'* 2 FETCH'),self.output().index(b'* 3 FETCH'))

    async def test_disconnect_cancels_fetch_work(self):
        started=asyncio.Event(); cancelled=asyncio.Event()
        async def work():
            started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        task=asyncio.create_task(self.session.until_disconnect(work()))
        await started.wait()
        self.session.reader.feed_eof()
        await asyncio.wait_for(task,1)
        self.assertTrue(cancelled.is_set())
    async def test_noop_temporary_refresh_warning_and_ok(self):
        self.session.refresh=AsyncMock(side_effect=ApiError())
        await self.session.command('A','NOOP')
        self.assertIn(b'* OK [ALERT]',self.output()); self.assertTrue(self.output().endswith(b'A OK NOOP completed\r\n'))
        self.assertEqual([m.uid for m in self.session.messages],[2,5,8])
    async def test_noop_revoked_refresh_is_failure(self):
        self.session.refresh=AsyncMock(side_effect=ApiError(revoked=True))
        with self.assertRaises(ApiError): await self.session.command('A','NOOP')
        self.assertEqual(self.output(),b'')
