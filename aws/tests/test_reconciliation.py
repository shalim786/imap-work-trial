"""Deletion is authoritative only under the explicit full-visibility contract."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from agentmail_imap.adapter import ApiError
from agentmail_imap.config import Config
from agentmail_imap.models import Message
from agentmail_imap.server import Server, Session
from agentmail_imap.uid_store import UIDStore
from scripts.smoke_test import Sandbox, ok

class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=UIDStore.initialize(Path(self.tmp.name)/'uids.db',uidvalidity=100)
        self.addCleanup(self.store.close)
        self.messages=[Message('a',timestamp=datetime(2026,1,1,tzinfo=timezone.utc),size=3),Message('b',timestamp=datetime(2026,1,2,tzinfo=timezone.utc),size=4)]
        self.store.reconcile('namespace','inbox',self.messages)
        self.server=Server(Config(assume_full_visibility=True),self.store)
        self.session=Session(self.server,None,None)
        self.session.inbox='inbox'
        self.session.adapter=AsyncMock()
        self.session.adapter.namespace='namespace'
        self.session.adapter.recent_events.return_value=([],False)
        self.session.adapter.list_messages.return_value=[self.messages[1]]
        self.session.adapter.metadata.side_effect=ApiError(status=404)

    async def test_event_observation_deduplicates_without_changing_maps(self):
        from types import SimpleNamespace
        self.session.adapter.recent_events.return_value=([SimpleNamespace(event_id='e1',message_id='a',event_type='label.removed',label='received')],False)
        with self.assertLogs('agentmail_imap.server',level='INFO') as logs:
            await self.session.observe_events()
            await self.session.observe_events()
        self.assertEqual(sum('Inbox event type=' in line for line in logs.output),1)
        self.assertEqual(self.store.active_ids('namespace','inbox'),{'a','b'})

    async def test_event_failure_falls_back_but_revocation_propagates(self):
        self.session.adapter.recent_events.side_effect=ApiError(status=404)
        _,_,messages=await self.session.scan()
        self.assertEqual([m.uid for m in messages],[2])
        self.session.adapter.recent_events.side_effect=ApiError(status=403,revoked=True)
        await self.session.observe_events()
        self.session.adapter.authorize.side_effect=ApiError(status=403,revoked=True)
        with self.assertRaises(ApiError): await self.session.observe_events()
        self.session.adapter.recent_events.side_effect=ApiError(status=401,revoked=True)
        with self.assertRaises(ApiError): await self.session.scan()

    async def test_authoritative_404_retires_and_reappearance_allocates_higher(self):
        validity,nxt,messages=await self.session.scan()
        self.assertEqual((validity,nxt,[(m.message_id,m.uid) for m in messages]),(100,3,[('b',2)]))
        self.assertEqual(self.store.db.execute('SELECT uid,active FROM message_uids ORDER BY uid').fetchall(),[(1,0),(2,1)])
        self.session.adapter.list_messages.return_value=self.messages
        _,nxt,messages=await self.session.scan()
        self.assertEqual((nxt,[(m.message_id,m.uid) for m in messages]),(4,[('b',2),('a',3)]))
        self.assertEqual(self.session.adapter.authorize.await_count,3)

    async def test_without_contract_404_preserves_identity(self):
        self.server.config=replace(self.server.config,assume_full_visibility=False)
        with self.assertRaises(ApiError): await self.session.scan()
        self.assertEqual(self.store.active_ids('namespace','inbox'),{'a','b'})

    async def test_errors_and_failed_final_authorization_never_retire(self):
        for status in (403,429,503):
            with self.subTest(status=status):
                self.session.adapter.metadata.side_effect=ApiError(status=status,revoked=status==403)
                with self.assertRaises(ApiError): await self.session.scan()
                self.assertEqual(self.store.active_ids('namespace','inbox'),{'a','b'})
        self.session.adapter.metadata.side_effect=ApiError(status=404)
        self.session.adapter.authorize.side_effect=[None,ApiError(status=403,revoked=True)]
        with self.assertRaises(ApiError): await self.session.scan()
        self.assertEqual(self.store.active_ids('namespace','inbox'),{'a','b'})

    async def test_incomplete_scan_never_retire(self):
        self.session.adapter.list_messages.side_effect=ApiError(status=503)
        with self.assertRaises(ApiError): await self.session.scan()
        self.assertEqual(self.store.active_ids('namespace','inbox'),{'a','b'})
        self.session.adapter.metadata.assert_not_awaited()

    async def test_trash_retires_without_full_visibility_and_restore_gets_new_uid(self):
        self.server.config=replace(self.server.config,assume_full_visibility=False)
        self.session.adapter.metadata.side_effect=None
        self.session.adapter.metadata.return_value=replace(self.messages[0],labels=['received','trash'])
        _,_,messages=await self.session.scan()
        self.assertEqual([m.uid for m in messages],[2])
        self.assertEqual(self.store.active_ids('namespace','inbox'),{'b'})
        self.session.adapter.list_messages.return_value=self.messages
        _,_,messages=await self.session.scan()
        self.assertEqual([(m.message_id,m.uid) for m in messages],[('b',2),('a',3)])

    def test_visibility_environment_requires_explicit_boolean(self):
        with patch.dict('os.environ',{'IMAP_ASSUME_FULL_VISIBILITY':'true'},clear=True):
            self.assertTrue(Config.from_env('/nonexistent/config').assume_full_visibility)
        with patch.dict('os.environ',{'IMAP_ASSUME_FULL_VISIBILITY':'perhaps'},clear=True):
            with self.assertRaises(ValueError): Config.from_env('/nonexistent/config')

class DeletionIntegrationTests(unittest.TestCase):
    def test_two_sessions_removal_restart_and_reappearance(self):
        import imaplib
        with Sandbox(refresh=30) as box:
            box.stop(box.server)
            box.env['IMAP_ASSUME_FULL_VISIBILITY']='true';box.start_server()
            box.control('add-message',{})
            first,count=box.connect();self.assertEqual(count,4)
            validity=first.response('UIDVALIDITY')[1]
            with imaplib.IMAP4('127.0.0.1',box.port,timeout=10) as second:
                ok(second.login('candidate@imap.test','test_agentmail_key'));ok(second.select())
                box.control('remove-message',{'message_id':'msg_new_arrival'})
                ok(first.noop());ok(second.noop())
                self.assertEqual(ok(first.uid('SEARCH',None,'ALL')),[b'1 2 3'])
                self.assertEqual(ok(second.uid('SEARCH',None,'ALL')),[b'1 2 3'])
                with sqlite3.connect(box.path/'uids.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT uid,active FROM message_uids WHERE message_id=?',('msg_new_arrival',)).fetchall(),[(4,0)])
                    self.assertEqual(db.execute('SELECT next_uid FROM mailboxes').fetchone()[0],5)
            box.restart();first,count=box.connect()
            self.assertEqual(count,3);self.assertEqual(first.response('UIDVALIDITY')[1],validity)
            box.control('add-message',{});ok(first.noop())
            self.assertEqual(ok(first.uid('SEARCH',None,'ALL')),[b'1 2 3 5'])
