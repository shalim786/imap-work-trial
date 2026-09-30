import asyncio
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import httpx
from agentmail_imap.adapter import Adapter, ApiError
from agentmail_imap.models import Message
from agentmail_imap.uid_store import UIDStore, UIDStoreError
from agentmail_imap.config import Config


def message(identifier,stamp=1):
    return Message(identifier,labels=['received','unread'],timestamp=datetime(2026,1,stamp,tzinfo=timezone.utc),size=3)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'uids.db'
        self.store = UIDStore.initialize(self.path,uidvalidity=10)
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
    def test_restart_removal_reappearance_and_order(self):
        validity,nxt,items = self.store.reconcile('api|org','inbox',[message('b',2),message('a')])
        self.assertEqual((validity,nxt,[m.uid for m in items]),(10,3,[1,2]))
        self.assertEqual(self.store.claim_recent('api|org','inbox',readonly=True),{1,2})
        self.assertEqual(self.store.claim_recent('api|org','inbox'),{1,2})
        self.assertEqual(self.store.claim_recent('api|org','inbox'),set())
        self.store.close()
        self.store = UIDStore(self.path)
        self.assertEqual(self.store.reconcile('api|org','inbox',[message('a'),message('b',2)])[2][0].uid,1)
        with self.assertRaises(UIDStoreError): self.store.reconcile('api|org','inbox',[message('b',2)])
        self.store.reconcile('api|org','inbox',[message('b',2)],confirmed_removed={'a'},expected_revision=1)
        items = self.store.reconcile('api|org','inbox',[message('a'),message('b',2)])[2]
        self.assertEqual([(m.message_id,m.uid) for m in items],[('b',2),('a',3)])
    def test_namespace_backup_and_missing(self):
        self.store.reconcile('one','inbox',[message('a')])
        self.assertEqual(self.store.reconcile('two','inbox',[message('a')])[2][0].uid,1)
        backup = Path(self.tmp.name)/'backup.db'
        self.store.backup(backup)
        recovered = UIDStore(backup)
        self.assertEqual(recovered.active_ids('one','inbox'),{'a'})
        recovered.close()
        with self.assertRaises(UIDStoreError): UIDStore.initialize(self.path)
        with self.assertRaises(UIDStoreError): UIDStore(Path(self.tmp.name)/'absent')
        self.path.unlink()
        with self.assertRaises(UIDStoreError): self.store.active_ids('one','inbox')
    def test_stale_snapshot_rolls_back(self):
        self.store.reconcile('one','inbox',[message('a')])
        with self.assertRaises(UIDStoreError): self.store.reconcile('one','inbox',[],confirmed_removed={'a'},expected_revision=0)
        self.assertEqual(self.store.active_ids('one','inbox'),{'a'})
    def test_abrupt_process_death_preserves_only_committed_identity(self):
        self.store.reconcile('one','inbox',[message('a')])
        # A separate process dies with an open write transaction, without close or rollback.
        child = subprocess.run([sys.executable,'-c',
            "import sqlite3,os,sys; db=sqlite3.connect(sys.argv[1]); db.execute('BEGIN IMMEDIATE'); "
            "db.execute(\"UPDATE mailboxes SET next_uid=999 WHERE namespace='one'\"); "
            "db.execute(\"INSERT INTO message_uids VALUES (1,'uncommitted',998,1,1)\"); os._exit(17)", str(self.path)])
        self.assertEqual(child.returncode,17)
        self.store.close()
        self.store = UIDStore(self.path)
        validity,nxt,items = self.store.reconcile('one','inbox',[message('a')])
        self.assertEqual((validity,nxt,items[0].uid),(10,2,1))
        self.assertEqual(self.store.active_ids('one','inbox'),{'a'})

    def test_explicit_reset_generation(self):
        self.store.reconcile('one','inbox',[message('a')])
        self.assertEqual(self.store.reset_generation(100),101)
        validity,nxt,items = self.store.reconcile('one','inbox',[message('a')])
        self.assertEqual((validity,nxt,items[0].uid),(101,2,1))

    def test_corrupt_database(self):
        path = Path(self.tmp.name)/'corrupt'
        path.write_bytes(b'not sqlite')
        with self.assertRaises(UIDStoreError): UIDStore(path)
    def test_config_and_flags(self):
        self.assertEqual(Config().refresh_interval,60)
        with self.assertRaises(ValueError): Config(port=-1)
        with self.assertRaises(ValueError): Config(api_url='https://secret:key@host/v0')
        self.assertEqual(Message('id',labels=['read','unread','starred']).flags,[r'\Flagged'])
        self.assertEqual(Message('id',labels=['received']).flags,[r'\Seen'])
        self.assertEqual(Message('id',labels=['received','unread']).flags,[])


class ByteStream(httpx.AsyncByteStream):
    def __init__(self,data): self.data=data
    async def __aiter__(self): yield self.data


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_code_is_retained_without_raw_body(self):
        for supplied, expected in [('missing_permission','missing_permission'),('secret\nvalue',None),({'key':'secret'},None)]:
            adapter=Adapter('https://api.test/v0','test-key',transport=httpx.MockTransport(lambda request: httpx.Response(403,json={'code':supplied,'message':'private error body'})))
            try:
                with self.assertRaises(ApiError) as caught:
                    await adapter.recent_events('i@test')
                self.assertEqual(caught.exception.code,expected)
                self.assertNotIn('private',str(caught.exception))
            finally:
                await adapter.close()

    async def test_sdk_pagination_auth_url_and_download(self):
        requests = []
        def respond(request):
            requests.append(request)
            path = request.url.path
            if path == '/v0/auth/me': return httpx.Response(200,json={'scope_type':'inbox','scope_id':'i@test','inbox_id':'i@test','organization_id':'org'})
            if path == '/v0/inboxes/i@test': return httpx.Response(200,json={'inbox_id':'i@test'})
            if path.endswith('/messages'):
                self.assertEqual(request.url.params['include_trash'],'true')
                self.assertEqual(request.url.params['include_spam'],'true')
                page = request.url.params.get('page_token')
                data = {'message_id':'b' if page else 'a','labels':['received','trash'] if page else ['received'],'timestamp':'2026-01-01T00:00:00Z','size':3}
                return httpx.Response(200,json={'count':1,'messages':[data],**({} if page else {'next_page_token':'next'})})
            if path.endswith('/raw') and request.url.host != 'files.test': return httpx.Response(200,json={'message_id':'a','size':3,'download_url':'https://files.test/raw','expires_at':'2026-01-01T00:00:00Z'})
            if request.url.host == 'files.test':
                self.assertNotIn('authorization',request.headers)
                return httpx.Response(200,stream=ByteStream(b'abc'))
            return httpx.Response(404,json={'message':'unknown'})
        adapter = Adapter('https://api.test/v0','private',transport=httpx.MockTransport(respond))
        try:
            self.assertEqual(await adapter.authorize('i@test'),'https://api.test/v0|org')
            self.assertEqual(len(requests),2)
            self.assertEqual([m.message_id for m in await adapter.list_messages('i@test')],['a'])
            target = io.BytesIO()
            self.assertEqual(await adapter.download('i@test','a',target),3)
            self.assertEqual(target.getvalue(),b'abc')
        finally: await adapter.close()
    async def test_mutation_denial_keeps_read_access_and_errors_hide_secrets(self):
        adapter = Adapter('https://api.test/v0','private',transport=httpx.MockTransport(lambda request:httpx.Response(403,json={'message':'private sensitive URL'})))
        try:
            with self.assertRaises(ApiError) as denied: await adapter.set_seen('inbox','id',True)
            self.assertFalse(denied.exception.revoked)
            self.assertNotIn('private',str(denied.exception))
            with self.assertRaises(ApiError) as denied: await adapter.metadata('inbox','id')
            self.assertTrue(denied.exception.revoked)
        finally: await adapter.close()
    async def test_cancelled_download_closes_stream_and_leaves_owner_in_control(self):
        started = asyncio.Event()
        closed = asyncio.Event()
        class BlockingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'a'
                started.set()
                await asyncio.Event().wait()
            async def aclose(self): closed.set()
        def respond(request):
            if request.url.host == 'files.test': return httpx.Response(200,stream=BlockingStream())
            return httpx.Response(200,json={'message_id':'a','size':3,'download_url':'https://files.test/raw','expires_at':'2026-01-01T00:00:00Z'})
        adapter = Adapter('https://api.test/v0','private',transport=httpx.MockTransport(respond))
        target = io.BytesIO()
        try:
            task = asyncio.create_task(adapter.download('inbox','a',target))
            await asyncio.wait_for(started.wait(),1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertTrue(closed.is_set())
            self.assertFalse(target.closed)
        finally: await adapter.close()

    async def test_raw_size_bound_rejects_before_downloading(self):
        seen = []
        def respond(request):
            seen.append(request.url.host)
            return httpx.Response(200,json={'message_id':'a','size':4,'download_url':'https://files.test/raw','expires_at':'2026-01-01T00:00:00Z'})
        adapter = Adapter('https://api.test/v0','private',transport=httpx.MockTransport(respond))
        try:
            with self.assertRaises(ApiError): await adapter.download('inbox','a',io.BytesIO(),max_bytes=3)
            self.assertEqual(seen,['api.test'])
        finally: await adapter.close()

    async def test_truncated_download(self):
        def respond(request):
            if request.url.host == 'files.test': return httpx.Response(200,stream=ByteStream(b'a'))
            return httpx.Response(200,json={'message_id':'a','size':3,'download_url':'https://files.test/raw','expires_at':'2026-01-01T00:00:00Z'})
        adapter = Adapter('https://api.test/v0','private',transport=httpx.MockTransport(respond))
        try:
            with self.assertRaises(ApiError): await adapter.download('inbox','a',io.BytesIO())
        finally: await adapter.close()

if __name__ == '__main__': unittest.main()
