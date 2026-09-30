"""Ordering, cancellation, pacing and cache isolation for large inbox loading."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
import httpx
from agentmail_imap.adapter import Adapter, ApiError
from agentmail_imap.config import Config
from agentmail_imap.raw_cache import RawCache, CacheFull
from agentmail_imap.request_policy import RequestPolicy, persistent_retries, retry_after
from agentmail_imap.server import Server, Session


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_concurrency_is_shared_across_adapters(self):
        policy=RequestPolicy(1000,2,0.001,0.01)
        first=Adapter('https://api.test/v0','key',policy=policy)
        second=Adapter('https://api.test/v0','key',policy=policy)
        self.addAsyncCleanup(first.close);self.addAsyncCleanup(second.close)
        active=peak=0
        async def download(*args,**kwargs):
            nonlocal active,peak
            active+=1;peak=max(peak,active)
            try: await asyncio.sleep(0.01);return 3
            finally: active-=1
        first._download_once=download;second._download_once=download
        await asyncio.gather(*(adapter.download('inbox',str(i),None) for i,adapter in enumerate([first,second,first,second])))
        self.assertEqual(peak,2)

    async def test_persistent_retries_exceed_old_sdk_limit_and_respect_retry_after(self):
        calls=[]
        def respond(request):
            calls.append(time.monotonic())
            if len(calls)<=4:
                return httpx.Response(429,headers={'Retry-After':'0.02'},json={'code':'rate_limit'})
            return httpx.Response(200,json={'count':0,'events':[]})
        adapter=Adapter('https://api.test/v0','key',transport=httpx.MockTransport(respond),
                        policy=RequestPolicy(1000,2,0.001,0.005))
        self.addAsyncCleanup(adapter.close)
        with persistent_retries():
            self.assertEqual(await adapter.recent_events('inbox'),([],False))
        self.assertEqual(len(calls),5)
        self.assertTrue(all(b-a>=0.019 for a,b in zip(calls,calls[1:])))

    async def test_cooldown_is_shared_but_different_keys_are_independent(self):
        shared=RequestPolicy(1000,2,0.01,0.03)
        shared.defer(0,{'Retry-After':'0.04'})
        other=RequestPolicy(1000,2,0.01,0.03)
        started=time.monotonic()
        await other.wait()
        self.assertLess(time.monotonic()-started,0.02)
        await shared.wait()
        self.assertGreaterEqual(time.monotonic()-started,0.039)

    async def test_shared_pacing_and_credential_identity(self):
        policy=RequestPolicy(100,2,0.001,0.01)
        calls=[]
        async def issue():
            await policy.wait();calls.append(time.monotonic())
        await asyncio.gather(*(issue() for _ in range(4)))
        self.assertTrue(all(b-a>=0.009 for a,b in zip(calls,calls[1:])))
        with tempfile.TemporaryDirectory() as root:
            server=Server(Config(temp_dir=Path(root)),None)
            self.assertIs(server.policy_for('a'),server.policy_for('a'))
            self.assertIsNot(server.policy_for('a'),server.policy_for('b'))
            await server.close()

    async def test_permanent_failure_is_not_retried_and_persistent_wait_cancels(self):
        calls=[]
        status=403
        def respond(request):
            calls.append(request)
            return httpx.Response(status,json={'code':'missing_permission'})
        adapter=Adapter('https://api.test/v0','key',transport=httpx.MockTransport(respond),
                        policy=RequestPolicy(1000,2,0.02,0.03))
        self.addAsyncCleanup(adapter.close)
        with persistent_retries():
            with self.assertRaises(ApiError): await adapter.recent_events('inbox')
        self.assertEqual(len(calls),1)
        status=429
        async def forever():
            with persistent_retries(): await adapter.recent_events('inbox')
        task=asyncio.create_task(forever())
        await asyncio.sleep(0.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task

    def test_retry_after_parsing(self):
        self.assertEqual(retry_after({'Retry-After':'3'}),3)
        self.assertIsNone(retry_after({'Retry-After':'nan'}))
        self.assertIsNone(retry_after({'Retry-After':'invalid'}))
        self.assertEqual(retry_after({'Retry-After':'Tue, 01 Jan 2019 00:00:00 GMT'}),0)


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.cache=RawCache(self.tmp.name,6,0.04,lambda:20-self.cache.bytes)
        self.addAsyncCleanup(self.cache.close)
        self.download=AsyncMock(side_effect=lambda path:path.write_bytes(b'abc'))

    async def test_reuse_revalidates_access_and_expiry_and_shutdown_delete_files(self):
        validate=AsyncMock()
        async with self.cache.lease(('inbox','key','a'),3,self.download): pass
        async with self.cache.lease(('inbox','key','a'),3,self.download,validate) as path:
            self.assertEqual(path.read_bytes(),b'abc')
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.download.await_count,1)
        validate.assert_awaited_once()
        await asyncio.sleep(0.09)
        self.assertEqual(self.cache.bytes,0)
        self.assertFalse(path.exists())
        async with self.cache.lease('b',3,self.download): pass
        await self.cache.close()
        self.assertEqual(list(Path(self.tmp.name).iterdir()),[])

    async def test_concurrent_same_identity_downloads_once_and_keys_are_isolated(self):
        async def download(path):
            await asyncio.sleep(0.005);path.write_bytes(b'abc')
        self.download.side_effect=download
        async def use(key):
            async with self.cache.lease(key,3,self.download): await asyncio.sleep(0.005)
        await asyncio.gather(use(('key1','a')),use(('key1','a')))
        self.assertEqual(self.download.await_count,1)
        await use(('key2','a'))
        self.assertEqual(self.download.await_count,2)
        self.assertEqual(self.cache.key_locks,{})

    async def test_lru_capacity_pinned_files_and_failed_download_cleanup(self):
        async with self.cache.lease('a',3,self.download):
            async with self.cache.lease('b',3,self.download):
                with self.assertRaises(CacheFull):
                    async with self.cache.lease('c',3,self.download): pass
                self.assertEqual(self.cache.bytes,6)
        async with self.cache.lease('c',3,self.download): pass
        self.assertNotIn('a',self.cache.entries)
        self.assertEqual(self.cache.bytes,6)
        async def fail(path):
            path.write_bytes(b'a');raise ApiError(status=404)
        with self.assertRaises(ApiError):
            async with self.cache.lease('d',3,fail): pass
        self.assertEqual(self.cache.key_locks,{})
        self.assertEqual(self.cache.bytes,3)

    async def test_cached_data_does_not_bypass_revocation(self):
        async with self.cache.lease('a',3,self.download): pass
        validate=AsyncMock(side_effect=ApiError(status=403,revoked=True))
        with self.assertRaises(ApiError):
            async with self.cache.lease('a',3,self.download,validate):
                self.fail('Revoked content was exposed')
        self.assertEqual(self.cache.entries['a'].pins,0)


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_refreshes_share_scan_and_isolate_session_lists(self):
        with tempfile.TemporaryDirectory() as root:
            server=Server(Config(temp_dir=Path(root)),None)
            self.addAsyncCleanup(server.close)
            one=Session(server,None,None);two=Session(server,None,None)
            for session in (one,two):
                session.inbox='inbox';session.key='key';server.join(session)
            async def scan(*, periodic=False):
                await asyncio.sleep(0.01);return (123,2,[])
            one.scan=AsyncMock(side_effect=scan);two.scan=AsyncMock(side_effect=scan)
            await asyncio.gather(one.refresh(),two.refresh())
            self.assertEqual(one.scan.await_count+two.scan.await_count,1)
            self.assertIsNot(one.pending[2],two.pending[2])
            await one.refresh()
            self.assertEqual(one.scan.await_count+two.scan.await_count,2)
