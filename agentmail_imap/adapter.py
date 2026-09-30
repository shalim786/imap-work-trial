"""Session-bound official async SDK integration. Never expose upstream error bodies."""
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
import httpx
from agentmail import AsyncAgentMail
from agentmail.environment import AgentMailEnvironment
from agentmail.core.api_error import ApiError as SDKError
from .models import Message


class ApiError(Exception):
    def __init__(self, message='Upstream request failed', *, status=None, revoked=False):
        super().__init__(message)
        self.status = status
        self.revoked = revoked


class Adapter:
    def __init__(self, api_url: str, key: str, *, transport=None):
        parts = urlsplit(api_url.rstrip('/'))
        path = parts.path[:-3] if parts.path.endswith('/v0') else parts.path
        origin = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, '', '')).rstrip('/')
        self.api_url = origin + '/v0'
        self.organization_id = None
        self.namespace = self.api_url
        self._http = httpx.AsyncClient(timeout=60, follow_redirects=False, transport=transport)
        self._download_http = httpx.AsyncClient(timeout=60, follow_redirects=False, transport=transport)
        self._client = AsyncAgentMail(api_key=key, environment=AgentMailEnvironment(http=origin, websockets=''), timeout=60, httpx_client=self._http)

    async def _call(self, method, *args, mutation=False, **kwargs):
        try:
            return await method(*args, request_options={'max_retries':2, 'timeout_in_seconds':60}, **kwargs)
        except SDKError as exc:
            status = exc.status_code
            raise ApiError('Upstream access denied' if status in (401,403) else 'Upstream request failed', status=status, revoked=status == 401 or (status == 403 and not mutation)) from None
        except (httpx.HTTPError, ValueError, TypeError):
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
                if 'received' in message.labels:
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
        info = await self.raw_info(inbox,message_id)
        if max_bytes is not None and info.size > max_bytes: raise ApiError('Message size limit exceeded')
        owned = isinstance(target,(str,Path))
        file = open(target,'wb') if owned else target
        total = 0
        try:
            # Signed download URLs require no API bearer credential, including redirects.
            async with self._download_http.stream('GET',info.download_url) as response:
                if response.status_code != 200: raise ApiError('Raw download failed',status=response.status_code)
                async for chunk in response.aiter_raw(65536):
                    total += len(chunk)
                    if total > info.size or (max_bytes is not None and total > max_bytes): raise ApiError('Raw download size mismatch')
                    file.write(chunk)
            if total != info.size: raise ApiError('Raw download size mismatch')
            file.flush()
            return total
        except httpx.HTTPError:
            raise ApiError('Raw download failed') from None
        finally:
            if owned: file.close()

    async def set_seen(self, inbox: str, message_id: str, seen: bool) -> Message:
        await self._call(self._client.inboxes.messages.update,quote(inbox,safe=''),quote(message_id,safe=''),add_labels=['read' if seen else 'unread'],remove_labels=['unread' if seen else 'read'],mutation=True)
        return await self.metadata(inbox,message_id)

    async def close(self):
        await self._http.aclose()
        await self._download_http.aclose()
        self._client = None
