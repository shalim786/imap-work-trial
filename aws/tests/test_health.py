import asyncio
import unittest
from agentmail_imap.health import HealthServer

class Writer:
    def __init__(self): self.data = b''
    def write(self,data): self.data += data
    async def drain(self): pass
    def close(self): pass

class Store:
    def __init__(self): self.calls = 0
    def check_ready(self):
        self.calls += 1
        return True

class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_readiness_and_draining(self):
        store = Store()
        health = HealthServer(store,8080)
        async def request():
            reader = asyncio.StreamReader()
            reader.feed_data(b'GET /ready HTTP/1.1\r\nHost: localhost\r\n\r\n')
            writer = Writer()
            await health.handle(reader,writer)
            return writer.data
        self.assertIn(b'200 OK',await request())
        health.ready = False
        self.assertIn(b'503 Service Unavailable',await request())
        self.assertEqual(store.calls,1)

    async def test_database_failure_is_sanitized(self):
        class FailingStore:
            def check_ready(self): raise ValueError('secret-password')
        reader = asyncio.StreamReader()
        reader.feed_data(b'GET /ready HTTP/1.1\r\n\r\n')
        writer = Writer()
        await HealthServer(FailingStore(),8080).handle(reader,writer)
        self.assertIn(b'503',writer.data)
        self.assertNotIn(b'secret',writer.data)
