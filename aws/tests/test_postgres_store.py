"""Run only against a dedicated empty test database, never production."""
import asyncio
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from pathlib import Path

from agentmail_imap.models import Message
from agentmail_imap.uid_store import UIDStore,UIDStoreError
try:
    from agentmail_imap.postgres_store import PostgresUIDStore
except ImportError:
    PostgresUIDStore = None


@unittest.skipUnless(os.getenv('IMAP_TEST_POSTGRES_DSN') and PostgresUIDStore,'Dedicated PostgreSQL test DSN/dependencies unavailable')
class PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This refuses existing schemas rather than destroying a database.
        cls.dsn = os.environ['IMAP_TEST_POSTGRES_DSN']
        cls.store = PostgresUIDStore.initialize(cls.dsn,uidvalidity=1234)

    @classmethod
    def tearDownClass(cls): cls.store.close()

    def message(self,identifier):
        return Message(identifier,timestamp=datetime(2026,1,1,tzinfo=timezone.utc),labels=['received'],size=1)

    def test_a_migration_preserves_inactive_history(self):
        from scripts.migrate_uid_store import migrate
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'uids.sqlite3'
            source = UIDStore.initialize(path,uidvalidity=4321)
            source.reconcile('import','inbox',[self.message('old'),self.message('stay')])
            source.reconcile('import','inbox',[self.message('stay')],confirmed_removed=['old'])
            source.close()
            self.assertEqual(migrate(path,self.dsn),(1,2))
            self.assertTrue(self.store.check_ready())
            with self.store._db() as db:
                self.assertEqual(db.execute("SELECT value FROM settings WHERE name='schema_version'").fetchone(),(2,))
            validity,next_uid,messages = self.store.reconcile('import','inbox',[self.message('old'),self.message('stay')])
            self.assertEqual((validity,next_uid),(4321,4))
            self.assertEqual({m.message_id:m.uid for m in messages},{'old':3,'stay':2})
            with self.assertRaises(UIDStoreError): migrate(path,self.dsn)

    def test_concurrent_allocate_retire_restore_restart(self):
        inbox = 'identities'
        def update():
            return self.store.reconcile('test',inbox,[self.message('a'),self.message('b')])
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _:update(),range(8)))
        self.assertTrue(all([m.uid for m in r[2]]==[1,2] for r in results))
        rev = self.store.revision('test',inbox)
        self.store.reconcile('test',inbox,[self.message('b')],confirmed_removed=['a'],expected_revision=rev)
        with self.assertRaises(UIDStoreError):
            self.store.reconcile('test',inbox,[self.message('b')],expected_revision=rev)
        other = PostgresUIDStore(self.dsn)
        try:
            validity,next_uid,messages = other.reconcile('test',inbox,[self.message('a'),self.message('b')])
            self.assertEqual((validity,next_uid),(4321,4))
            self.assertEqual({m.message_id:m.uid for m in messages},{'a':3,'b':2})
        finally: other.close()

    def test_shared_pacing_and_cooldown(self):
        async def run():
            fingerprint = b'a'*32
            self.assertEqual(await self.store.reserve_request(fingerprint,1),0)
            self.assertGreater(await self.store.reserve_request(fingerprint,1),0)
            await self.store.cool_down(fingerprint,2)
            self.assertGreater(await self.store.cooldown_delay(fingerprint),1)
            self.assertGreater(await self.store.reserve_request(fingerprint,1),1)
        asyncio.run(run())

    def test_snapshot_and_recent(self):
        result = self.store.reconcile('test','snapshot',[self.message('a')])
        self.store.write_snapshot('test','snapshot','a'*64,result)
        loaded = self.store.read_snapshot('test','snapshot','a'*64,since=0)
        self.assertEqual(loaded,result)
        self.assertIsNone(self.store.read_snapshot('test','snapshot','b'*64,since=0))
        self.assertEqual(self.store.claim_recent('test','snapshot'),{1})
        self.assertEqual(self.store.claim_recent('test','snapshot'),set())
        self.store.invalidate_snapshot('test','snapshot')
        self.assertIsNone(self.store.read_snapshot('test','snapshot','a'*64,since=0))

    def test_expired_owner_is_fenced_after_takeover(self):
        from agentmail_imap.postgres_store import MailboxLease
        old_fence = self.store._try_lease('test','fencing','old',30)
        old = MailboxLease('test','fencing','old',old_fence)
        with self.store._db() as db:
            db.execute("UPDATE mailbox_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE inbox='fencing'")
        new_fence = self.store._try_lease('test','fencing','new',30)
        self.assertGreater(new_fence,old_fence)
        with self.assertRaises(UIDStoreError):
            self.store.reconcile('test','fencing',[self.message('a')],lease=old)
        self.assertEqual(self.store.active_ids('test','fencing'),set())
        new = MailboxLease('test','fencing','new',new_fence)
        self.store.reconcile('test','fencing',[self.message('a')],lease=new)
        with self.assertRaises(UIDStoreError):
            self.store.write_snapshot('test','fencing','a'*64,(1,1,[]),lease=old)
        with self.assertRaises(UIDStoreError):
            self.store.invalidate_snapshot('test','fencing',lease=old)
        self.store.invalidate_snapshot('test','fencing',lease=new)
        self.store._release_lease(new)

    def test_lock_serializes_scan(self):
        async def run():
            sequence=[]
            async def worker(n):
                async with self.store.reconciliation_lock('test','locked'):
                    sequence.append(('start',n))
                    await asyncio.sleep(.05)
                    sequence.append(('end',n))
            await asyncio.gather(worker(1),worker(2))
            self.assertEqual([x[0] for x in sequence],['start','end','start','end'])
        asyncio.run(run())

@unittest.skipUnless(PostgresUIDStore,'PostgreSQL dependencies unavailable')
class LeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiter_does_not_hold_pool_connection(self):
        from unittest.mock import Mock,patch
        store = object.__new__(PostgresUIDStore)
        store._try_lease = Mock(return_value=None)
        store._release_lease = Mock()
        async def cancelled_sleep(seconds):
            store._try_lease.assert_called_once()
            raise asyncio.CancelledError()
        with patch('agentmail_imap.postgres_store.asyncio.sleep',side_effect=cancelled_sleep):
            with self.assertRaises(asyncio.CancelledError):
                async with store.reconciliation_lock('namespace','inbox'):
                    self.fail('Failed lease entered scan')
        store._release_lease.assert_not_called()

    async def test_lease_owner_releases_on_scan_cancellation(self):
        from unittest.mock import Mock
        store = object.__new__(PostgresUIDStore)
        store._try_lease = Mock(return_value=7)
        store._release_lease = Mock()
        with self.assertRaises(asyncio.CancelledError):
            async with store.reconciliation_lock('namespace','inbox') as lease:
                self.assertEqual(lease.fence,7)
                raise asyncio.CancelledError()
        store._release_lease.assert_called_once_with(lease)

    async def test_failed_renewal_cancels_scan_and_fails_closed(self):
        from unittest.mock import Mock,patch
        store = object.__new__(PostgresUIDStore)
        store._try_lease = Mock(return_value=7)
        store._renew_lease = Mock(return_value=False)
        store._release_lease = Mock()
        event = asyncio.Event()
        async def fast_sleep(seconds):
            return
        with patch('agentmail_imap.postgres_store.asyncio.sleep',side_effect=fast_sleep):
            with self.assertRaisesRegex(UIDStoreError,'renewal failed'):
                async with store.reconciliation_lock('namespace','inbox'):
                    await event.wait()
        store._release_lease.assert_called_once()

    def test_stale_fence_prevents_snapshot_invalidation(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        from agentmail_imap.postgres_store import MailboxLease
        store = object.__new__(PostgresUIDStore)
        db = Mock()
        db.execute.return_value.fetchone.return_value = None
        @contextmanager
        def connection():
            yield db
        store._db = connection
        with self.assertRaisesRegex(UIDStoreError,'lease lost'):
            store.invalidate_snapshot('ns','inbox',lease=MailboxLease('ns','inbox','stale',1))
        self.assertEqual(db.execute.call_count,1)
        self.assertIn('mailbox_leases',db.execute.call_args.args[0])
