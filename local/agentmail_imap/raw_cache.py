"""Private, bounded raw files; authorization is checked by callers, never cached."""
import asyncio
import contextlib
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path


class CacheFull(Exception):
    pass


@dataclass
class Entry:
    path: Path
    size: int
    expires: float
    pins: int = 0


class RawCache:
    def __init__(self, directory, max_bytes, ttl, available_bytes):
        self.directory = Path(directory) / 'cache'
        self.max_bytes, self.ttl = max_bytes, ttl
        self.available_bytes = available_bytes
        self.entries = OrderedDict()
        self.bytes = 0
        self.hits = self.misses = 0
        self.lock = asyncio.Lock()
        self.key_locks = {}
        self.janitor = None

    def _remove(self, key):
        entry = self.entries.pop(key)
        entry.path.unlink(missing_ok=True)
        self.bytes -= entry.size

    def expire(self):
        now = time.monotonic()
        for key, entry in list(self.entries.items()):
            if not entry.pins and entry.expires <= now:
                self._remove(key)

    async def clean_loop(self):
        while True:
            await asyncio.sleep(min(self.ttl, 30))
            async with self.lock:
                self.expire()

    @contextlib.asynccontextmanager
    async def lease(self, key, size, download, validate=None):
        if not self.max_bytes or size > self.max_bytes:
            raise CacheFull()
        # Refcounts bound the per-identity locks to concurrent users, including
        # waiters; completed/failed/expired identities leave no lock behind.
        lock, users = self.key_locks.get(key, (asyncio.Lock(), 0))
        self.key_locks[key] = (lock, users + 1)
        entry = None
        try:
            async with lock:
                async with self.lock:
                    self.expire()
                    entry = self.entries.get(key)
                    if entry:
                        self.hits += 1
                        entry.pins += 1
                        self.entries.move_to_end(key)
                    else:
                        self.misses += 1
                        for old, candidate in list(self.entries.items()):
                            if self.bytes + size <= self.max_bytes and size <= self.available_bytes():
                                break
                            if not candidate.pins:
                                self._remove(old)
                        if self.bytes + size > self.max_bytes or size > self.available_bytes():
                            raise CacheFull()
                        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                        if self.janitor is None:
                            self.janitor = asyncio.create_task(self.clean_loop())
                        path = self.directory / uuid.uuid4().hex
                        self.bytes += size  # Also reserve in-flight downloads.
                if entry is None:
                    try:
                        path.touch(mode=0o600)
                        await download(path)
                        if path.stat().st_size != size:
                            raise ValueError('Raw size mismatch')
                        os.chmod(path, 0o600)
                    except BaseException:
                        path.unlink(missing_ok=True)
                        self.bytes -= size
                        raise
                    entry = Entry(path, size, time.monotonic() + self.ttl, pins=1)
                    self.entries[key] = entry
                elif validate is not None:
                    # Cached content never grants access by itself.
                    await validate()
            yield entry.path
        finally:
            if entry:
                entry.pins -= 1
                if not entry.pins and entry.expires <= time.monotonic():
                    self._remove(key)
            lock, users = self.key_locks[key]
            if users == 1:
                del self.key_locks[key]
            else:
                self.key_locks[key] = (lock, users - 1)

    async def close(self):
        if self.janitor:
            self.janitor.cancel()
            await asyncio.gather(self.janitor, return_exceptions=True)
        for key in list(self.entries):
            self._remove(key)
        if self.directory.exists():
            self.directory.rmdir()
