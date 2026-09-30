"""Readiness uses the shared database; draining tasks immediately become unready."""
import asyncio

class HealthServer:
    def __init__(self, store, port):
        self.store = store
        self.port = port
        self.ready = True
        self.listener = None

    def database_ready(self):
        if hasattr(self.store,"check_ready"):
            return self.store.check_ready()
        # SQLite exists solely for isolated protocol test harnesses.
        return self.store.revision("health", "health") >= 0

    async def start(self):
        self.listener = await asyncio.start_server(self.handle, '0.0.0.0', self.port)

    async def handle(self, reader, writer):
        status = 503
        try:
            request = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
            if len(request) <= 4096 and request.split(b'\r\n',1)[0] == b'GET /ready HTTP/1.1' and self.ready:
                if await asyncio.wait_for(asyncio.to_thread(self.database_ready), 5):
                    status = 200
        except Exception:
            pass
        body = b'ready\n' if status == 200 else b'unavailable\n'
        writer.write(f'HTTP/1.1 {status} {"OK" if status == 200 else "Service Unavailable"}\r\nConnection: close\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
        try:
            await asyncio.wait_for(writer.drain(), 3)
        except Exception:
            pass
        finally:
            writer.close()

    async def close(self):
        self.ready = False
        if self.listener:
            self.listener.close()
            await self.listener.wait_closed()
