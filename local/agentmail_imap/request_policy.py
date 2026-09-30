"""Credential-shared request pacing and cancellation-friendly retry waits."""
import asyncio
import contextlib
import contextvars
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

retry_forever = contextvars.ContextVar('retry_forever', default=False)


@contextlib.contextmanager
def persistent_retries():
    token = retry_forever.set(True)
    try:
        yield
    finally:
        retry_forever.reset(token)


def retry_after(headers):
    value = next((v for k, v in (headers or {}).items() if k.lower() == 'retry-after'), None)
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


class RequestPolicy:
    def __init__(self, requests_per_second=0, concurrency=2, retry_initial=1, retry_max=60):
        self.interval = 1 / requests_per_second if requests_per_second else 0
        self.retry_initial, self.retry_max = retry_initial, retry_max
        self.slots = asyncio.Semaphore(concurrency)
        self.download_slots = asyncio.Semaphore(concurrency)
        self.lock = asyncio.Lock()
        self.next_request = self.blocked_until = 0

    async def wait(self):
        # Reserve at dispatch time; recheck after sleep because another request
        # may have extended the shared cooldown while this waiter slept.
        while True:
            async with self.lock:
                now = time.monotonic()
                delay = max(self.next_request, self.blocked_until) - now
                if delay <= 0:
                    self.next_request = now + self.interval
                    return
            await asyncio.sleep(delay)

    async def wait_cooldown(self):
        # Signed hosts obey cooldowns without consuming API rate reservations.
        while True:
            delay = self.blocked_until - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    def defer(self, failure, headers=None):
        delay = max(self.retry_initial * 2 ** min(failure, 20), self.interval)
        delay = min(delay, self.retry_max)
        specified = retry_after(headers)
        if specified is not None:
            delay = max(delay, specified)
        self.blocked_until = max(self.blocked_until, time.monotonic() + delay)
        return delay
