"""Session-bound official async SDK integration. Never expose upstream error bodies."""
from pathlib import Path
import re
import logging
from urllib.parse import quote, urlsplit, urlunsplit
import httpx
from agentmail import AsyncAgentMail
from agentmail.environment import AgentMailEnvironment
from agentmail.core.api_error import ApiError as SDKError
from .models import Message
from .request_policy import RequestPolicy, retry_forever

logger = logging.getLogger(__name__)
TRANSIENT = {408, 429, 500, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, message='Upstream request failed', *, status=None, revoked=False, code=None, permission=None):
        super().__init__(message)
        self.status = status
        self.revoked = revoked
        self.code = code
        self.permission = permission


class Adapter:
    def __init__(self, api_url: str, key: str, *, transport=None, policy=None):
        parts = urlsplit(api_url.rstrip('/'))
        path = parts.path[:-3] if parts.path.endswith('/v0') else parts.path
        origin = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, '', '')).rstrip('/')
        self.api_url = origin + '/v0'
        self.organization_id = None
        self.namespace = self.api_url
        self.policy = policy or RequestPolicy(retry_initial=0.1, retry_max=1)
        self._http = httpx.AsyncClient(timeout=60, follow_redirects=False, transport=transport)
        self._download_http = httpx.AsyncClient(timeout=60, follow_redirects=False, transport=transport)
        self._client = AsyncAgentMail(api_key=key, environment=AgentMailEnvironment(http=origin, websockets=''), timeout=60, httpx_client=self._http)

    async def _call(self, method, *args, mutation=False, **kwargs):
        failure = 0
        while True:
            headers = None
            try:
                async with self.policy.slots:
                    await self.policy.wait()
                    return await self._call_once(method, *args, mutation=mutation, **kwargs)
            except ApiError as exc:
                if exc.status not in TRANSIENT and not getattr(exc, 'transient', False):
                    raise
                headers = getattr(exc, 'retry_headers', None)
                delay = self.policy.defer(failure, headers)
                if not retry_forever.get() and failure >= 2:
                    raise
                logger.warning('Upstream retry status=%s wait_seconds=%.2f', exc.status, delay)
                failure += 1

    async def _call_once(self, method, *args, mutation=False, **kwargs):
        try:
            # Every attempt passes through our shared limiter; SDK retries would
            # bypass it and obscure Retry-After handling.
            return await method(*args, request_options={'max_retries':0, 'timeout_in_seconds':60}, **kwargs)
        except SDKError as exc:
            status = exc.status_code
            body = exc.body
            code = body.get('code') if isinstance(body, dict) else None
            # Retain only bounded machine-readable codes, never raw error bodies.
            if not isinstance(code, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', code):
                code = None
            permission = None
            if code == 'missing_permission' and isinstance(body, dict):
                details = ' '.join(str(body.get(field, ''))[:1000] for field in ('message','fix'))
                matches = re.findall(r'\b[a-z][a-z0-9_]{0,60}_(?:read|write|create|update|delete|list)\b', details)
                permission = ','.join(sorted(set(matches))) or None
            error = ApiError('Upstream access denied' if status in (401,403) else 'Upstream request failed', status=status, code=code, permission=permission, revoked=status == 401 or (status == 403 and not mutation))
            error.retry_headers = {'retry-after':value for name,value in (exc.headers or {}).items() if name.lower() == 'retry-after'}
            raise error from None
        except httpx.HTTPError:
            error = ApiError('Upstream request failed')
            error.transient = True
            raise error from None
        except (ValueError, TypeError):
            raise ApiError('Upstream request failed') from None

    async def authorize(self, inbox: str) -> str:
        identity = await self._call(self._client.auth.me)
        if identity.inbox_id and identity.inbox_id != inbox:
            raise ApiError('Inbox access denied', status=403, revoked=True)
        await self._call(self._client.inboxes.get, quote(inbox, safe=''))
        self.organization_id = identity.organization_id
        self.namespace = self.api_url + '|' + self.organization_id
        return self.namespace

    @staticmethod
    def _message(data):
        try:
            if not data.message_id or type(data.size) is not int or data.size < 0 or data.timestamp.tzinfo is None:
                raise ValueError()
            return Message(message_id=data.message_id, labels=list(data.labels), timestamp=data.timestamp, size=data.size)
        except (AttributeError, TypeError, ValueError):
            raise ApiError('Invalid upstream message metadata') from None

    async def list_messages(self, inbox: str) -> list[Message]:
        token = None
        tokens = set()
        messages = {}
        while True:
            page = await self._call(self._client.inboxes.messages.list, quote(inbox,safe=''), limit=100, page_token=token, labels=['received'], include_trash=True, include_spam=True, include_blocked=True, include_unauthenticated=True)
            for item in page.messages:
                message = self._message(item)
                if message.in_inbox:
                    previous = messages.get(message.message_id)
                    if previous and (previous.size != message.size or previous.timestamp != message.timestamp):
                        raise ApiError('Upstream identity metadata changed during scan')
                    messages[message.message_id] = message
                    if len(messages) > 100000: raise ApiError('Mailbox metadata limit exceeded')
            token = page.next_page_token
            if not token: return list(messages.values())
            if token in tokens: raise ApiError('Cyclic upstream pagination')
            tokens.add(token)
            if len(tokens) > 10000: raise ApiError('Upstream pagination limit exceeded')

    async def recent_events(self, inbox: str):
        """One diagnostic page; not a complete incremental reconciliation feed."""
        page = await self._call(self._client.inboxes.events.list, quote(inbox, safe=''), limit=100)
        for event in page.events:
            if event.inbox_id != inbox or not event.event_id or not event.message_id:
                raise ApiError('Invalid upstream inbox event')
        return page.events, bool(page.next_page_token)

    async def metadata(self, inbox: str, message_id: str) -> Message:
        return self._message(await self._call(self._client.inboxes.messages.get, quote(inbox,safe=''), quote(message_id,safe='')))

    async def raw_info(self, inbox: str, message_id: str):
        info = await self._call(self._client.inboxes.messages.get_raw, quote(inbox,safe=''), quote(message_id,safe=''))
        if info.message_id != message_id or type(info.size) is not int or info.size < 0:
            raise ApiError('Invalid upstream raw metadata')
        url = urlsplit(info.download_url)
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:
            raise ApiError('Invalid upstream download URL')
        return info

    async def download(self, inbox: str, message_id: str, target, *, max_bytes=None) -> int:
        failure = 0
        while True:
            try:
                async with self.policy.download_slots:
                    return await self._download_once(inbox, message_id, target, max_bytes=max_bytes)
            except ApiError as exc:
                if exc.status not in TRANSIENT and not getattr(exc, 'transient', False):
                    raise
                delay = self.policy.defer(failure, getattr(exc, 'retry_headers', None))
                if not retry_forever.get() and failure >= 2:
                    raise
                logger.warning('Raw download retry status=%s wait_seconds=%.2f', exc.status, delay)
                failure += 1

    async def _download_once(self, inbox: str, message_id: str, target, *, max_bytes=None) -> int:
        info = await self.raw_info(inbox,message_id)
        if max_bytes is not None and info.size > max_bytes: raise ApiError('Message size limit exceeded')
        owned = isinstance(target,(str,Path))
        file = open(target,'wb') if owned else target
        if not owned:
            file.seek(0)
            file.truncate()
        total = 0
        try:
            # Signed download URLs require no API bearer credential, including redirects.
            await self.policy.wait_cooldown()
            async with self._download_http.stream('GET',info.download_url) as response:
                if response.status_code != 200:
                    error = ApiError('Raw download failed',status=response.status_code)
                    error.retry_headers = {'retry-after':response.headers.get('retry-after','')}
                    raise error
                async for chunk in response.aiter_raw(65536):
                    total += len(chunk)
                    if total > info.size or (max_bytes is not None and total > max_bytes): raise ApiError('Raw download size mismatch')
                    file.write(chunk)
            if total != info.size:
                error = ApiError('Raw download truncated')
                error.transient = True
                raise error
            file.flush()
            return total
        except httpx.HTTPError:
            error = ApiError('Raw download failed')
            error.transient = True
            raise error from None
        finally:
            if owned: file.close()

    async def set_seen(self, inbox: str, message_id: str, seen: bool) -> Message:
        await self._call(self._client.inboxes.messages.update,quote(inbox,safe=''),quote(message_id,safe=''),add_labels=['read' if seen else 'unread'],remove_labels=['unread' if seen else 'read'],mutation=True)
        return await self.metadata(inbox,message_id)

    async def close(self):
        await self._http.aclose()
        await self._download_http.aclose()
        self._client = None
