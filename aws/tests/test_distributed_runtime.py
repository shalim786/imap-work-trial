"""Cross-worker refresh and request-budget behavior without cloud resources."""
import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from agentmail_imap.config import Config
from agentmail_imap.models import Message
from agentmail_imap.request_policy import DistributedRequestPolicy
from agentmail_imap.server import Server, Session

class SharedStore:
    def __init__(self):
        self.lock=asyncio.Lock(); self.snapshot=None
    @contextlib.asynccontextmanager
    async def reconciliation_lock(self,*args):
        async with self.lock: yield object()
    def database_time(self):
        from time import time
        return time()
    def read_snapshot(self,*args,**kwargs): return self.snapshot
    def assert_lease(self,lease): pass
    def write_snapshot(self,*args,**kwargs): self.snapshot=args[-1]
    def invalidate_snapshot(self,*args,**kwargs): self.snapshot=None

class DistributedTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_workers_share_completed_scan_and_mutation_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            store=SharedStore()
            servers=[Server(Config(temp_dir=Path(directory)/str(i)),store) for i in range(2)]
            for server in servers:
                self.addAsyncCleanup(server.close)
            sessions=[Session(s,asyncio.StreamReader(),None) for s in servers]
            scanned=0
            async def scan(**kwargs):
                nonlocal scanned
                scanned+=1
                await asyncio.sleep(.01)
                return 123,3,[Message('m',uid=2,labels=['received'])]
            for session in sessions:
                session.key='synthetic'; session.inbox='test'
                session.adapter=AsyncMock();session.adapter.namespace='ns'
                session._scan=scan
            a,b=await asyncio.gather(*(s.scan() for s in sessions))
            self.assertEqual(scanned,1);self.assertEqual(a,b)
            await sessions[0].mark_seen('m',False)
            self.assertIsNone(store.snapshot)
            await sessions[1].scan()
            self.assertEqual(scanned,2)

    async def test_shared_cooldown_is_published_before_retry(self):
        store=AsyncMock(); store.reserve_request=AsyncMock(side_effect=[.001,0]);store.cooldown_delay=AsyncMock(return_value=0)
        policy=DistributedRequestPolicy(store,b'x'*32,5,2,1,60)
        delay=policy.defer(0,{'Retry-After':'7'})
        await policy.wait()
        store.cool_down.assert_awaited_once_with(b'x'*32,delay)
        self.assertEqual(store.reserve_request.await_count,2)
