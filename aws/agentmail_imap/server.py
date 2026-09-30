"""Async IMAP transport, session state, and mailbox coordination."""
from __future__ import annotations
import asyncio
import base64
import contextlib
import dataclasses
import datetime as dt
import json
import hashlib
import logging
import ssl
import socket
import tempfile
import uuid
from pathlib import Path
from time import monotonic
from .protocol import tokenize, parse_sequence_set, parse_fetch_attributes, parse_search, UnsupportedCharset
from .mime import RawMessage
from .adapter import Adapter, ApiError
from .request_policy import DistributedRequestPolicy, RequestPolicy, persistent_retries
from .raw_cache import RawCache, CacheFull

logger = logging.getLogger(__name__)

class CommandError(Exception):
    def __init__(self, message, status='BAD'):
        self.status = status
        super().__init__(message)

class Server:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.sessions = set()
        self.groups = {}
        self.api_slots = asyncio.Semaphore(config.max_refreshes)
        self.fetch_slots = asyncio.Semaphore(config.max_downloads)
        self.temp_bytes = 0
        self.policies = {}
        self.connection_tasks = set()
        self.cache = RawCache(config.temp_dir, config.raw_cache_bytes, config.raw_cache_ttl,
                              lambda: self.config.max_temp_bytes - self.temp_bytes - self.cache.bytes)
        self.listener = None
        self.draining = False

    async def start(self):
        options = {}
        if self.config.tls_mode == 'implicit':
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.config.tls_cert_file, self.config.tls_key_file)
            options = {'ssl':context, 'ssl_handshake_timeout':self.config.tls_handshake_timeout,
                       'ssl_shutdown_timeout':self.config.tls_shutdown_timeout}
        self.listener = await asyncio.start_server(self.accept, self.config.host, self.config.port,
                                                   limit=self.config.max_command_bytes, **options)
        return self.listener

    async def accept(self, reader, writer):
        if self.draining or len(self.sessions) >= self.config.max_connections:
            writer.write(b'* BYE Connection limit\r\n')
            await writer.drain()
            writer.close()
            return
        self.configure_keepalive(writer)
        session = Session(self, reader, writer)
        self.sessions.add(session)
        task = asyncio.current_task()
        self.connection_tasks.add(task)
        try:
            await session.run()
        finally:
            self.sessions.discard(session)
            self.connection_tasks.discard(task)

    async def start_draining(self, timeout=45):
        """Reject new sessions and allow existing commands to finish within a deadline."""
        self.draining = True
        if self.listener:
            self.listener.close()
            await self.listener.wait_closed()
        deadline = monotonic() + timeout
        while self.sessions and monotonic() < deadline:
            # Idle sessions can reconnect immediately; active FETCH commands get
            # the remaining drain window before forced shutdown.
            for session in list(self.sessions):
                if not session.busy:
                    async with session.write_lock:
                        if not session.busy:
                            with contextlib.suppress(ConnectionError, TimeoutError):
                                async with asyncio.timeout(min(1, max(.01, deadline-monotonic()))):
                                    await session.line('* BYE Server draining; reconnect')
                            session.writer.close()
            await asyncio.sleep(0.1)

    def configure_keepalive(self, writer):
        """Enable transport probes; unsupported platform tuning is best effort."""
        sock = writer.get_extra_info('socket')
        if sock is None:
            return
        settings = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
        idle_option = getattr(socket, 'TCP_KEEPIDLE', getattr(socket, 'TCP_KEEPALIVE', None))
        for option, value in ((idle_option, self.config.tcp_keepalive_idle),
                              (getattr(socket, 'TCP_KEEPINTVL', None), self.config.tcp_keepalive_interval),
                              (getattr(socket, 'TCP_KEEPCNT', None), self.config.tcp_keepalive_count)):
            if option is not None:
                settings.append((socket.IPPROTO_TCP, option, value))
        for level, option, value in settings:
            try:
                sock.setsockopt(level, option, value)
            except OSError:
                logger.warning('TCP keepalive option unavailable option=%s', option)

    async def close(self):
        if self.listener:
            self.listener.close()
            await self.listener.wait_closed()
        for session in list(self.sessions):
            session.writer.close()
        for group in list(self.groups.values()):
            group.task.cancel()
            if group.inflight:
                group.inflight.cancel()
        await asyncio.gather(*(g.task for g in list(self.groups.values())), return_exceptions=True)
        await asyncio.gather(*(g.inflight for g in list(self.groups.values()) if g.inflight), return_exceptions=True)
        for task in list(self.connection_tasks):
            task.cancel()
        await asyncio.gather(*list(self.connection_tasks), return_exceptions=True)
        await self.cache.close()
        self.policies.clear()

    def policy_for(self, key):
        identity = hashlib.sha256(key.encode()).digest()
        if identity not in self.policies:
            policy_class = DistributedRequestPolicy if hasattr(self.store, 'reserve_request') else RequestPolicy
            args = (self.config.api_requests_per_second, self.config.download_concurrency,
                    self.config.retry_initial, self.config.retry_max)
            self.policies[identity] = (policy_class(self.store, identity, *args)
                                      if policy_class is DistributedRequestPolicy else policy_class(*args))
        return self.policies[identity]

    def join(self, session):
        key = (session.inbox, session.key)
        group = self.groups.get(key)
        if group is None:
            group = Group(self, key)
            self.groups[key] = group
        group.sessions.add(session)
        session.group = group

    def leave(self, session):
        group = session.group
        session.group = None
        if group:
            group.sessions.discard(session)
            if not group.sessions:
                group.task.cancel()
                if group.inflight:
                    group.inflight.cancel()
                self.groups.pop(group.key, None)

class Group:
    def __init__(self, server, key):
        self.server, self.key = server, key
        self.sessions = set()
        self.lock = asyncio.Lock()
        self.inflight = None
        self.task = asyncio.create_task(self.loop())

    async def refresh(self, session, *, periodic=False):
        if self.inflight is None or self.inflight.done():
            async def scan():
                async with asyncio.timeout(self.server.config.command_timeout):
                    return await session.scan(periodic=periodic)
            self.inflight = asyncio.create_task(scan())
        return await asyncio.shield(self.inflight)

    async def loop(self):
        while True:
            await asyncio.sleep(self.server.config.refresh_interval)
            eligible = [member for member in self.sessions if member.selected]
            if not eligible:
                continue
            if not self.sessions:
                return
            session = eligible[0]
            try:
                candidate = await self.refresh(session, periodic=True)
                for member in list(self.sessions):
                    member.pending = (candidate[0],candidate[1],list(candidate[2]))
                    # An idle boundary may announce arrivals/flags, but never EXPUNGE.
                    async with member.write_lock:
                        if member.selected and not member.busy:
                            await member.adapter.authorize(member.inbox)
                            await member.publish(False)
            except asyncio.CancelledError:
                raise
            except ApiError as exc:
                logger.warning('Periodic refresh failed status=%s revoked=%s; prior view retained',exc.status,exc.revoked)
                if exc.revoked:
                    for member in list(self.sessions):
                        member.writer.close()
            except Exception as exc:
                logger.warning('Periodic refresh failed category=%s; prior view retained',type(exc).__name__)
                # The last complete view remains valid; next scheduled scan retries.
                continue

class Session:
    def __init__(self, server, reader, writer):
        self.server, self.reader, self.writer = server, reader, writer
        self.adapter = None
        self.inbox = self.key = None
        self.selected = False
        self.readonly = False
        self.messages = []
        self.recent = set()
        self.pending = None
        self.group = None
        self.busy = False
        self.write_lock = asyncio.Lock()
        self.in_response = False
        self.uidvalidity = self.uidnext = 0
        self.observed_events = set()
        self.authorized_at = 0

    async def observe_events(self):
        """Observe event coverage without letting unverified events change UID state."""
        try:
            events, more = await self.adapter.recent_events(self.inbox)
        except ApiError as exc:
            if exc.status == 401:
                raise
            if exc.revoked:
                # Events may require permission beyond message reads. Verify the
                # inbox remains authorized before treating this as optional.
                await self.adapter.authorize(self.inbox)
            logger.warning('Inbox event check failed status=%s code=%s permission=%s; full reconciliation continues', exc.status, exc.code, exc.permission)
            return
        current = {event.event_id for event in events}
        for event in events:
            if event.event_id not in self.observed_events:
                fingerprint = hashlib.sha256(event.message_id.encode()).hexdigest()[:16]
                logger.info('Inbox event type=%s label=%s message_hash=%s',
                            json.dumps(str(event.event_type)[:80]), json.dumps(str(event.label)[:80]), fingerprint)
        logger.info('Inbox event check returned=%s unseen=%s more_pages=%s',
                    len(events), len(current - self.observed_events), more)
        self.observed_events = current

    async def line(self, value):
        self.writer.write(value.encode('utf-8') + b'\r\n')
        await asyncio.wait_for(self.writer.drain(), self.server.config.write_timeout)

    async def read_command(self):
        import re
        raw = await asyncio.wait_for(self.reader.readline(), self.server.config.idle_timeout)
        if not raw:
            return None
        if not raw.endswith(b'\r\n') or len(raw) > self.server.config.max_command_bytes:
            raise CommandError('Invalid command framing')
        self.busy = True
        self.literal_values = {}
        result = raw[:-2]
        while match := re.search(rb'\{(\d+)(\+)?\}$', result):
            size = int(match[1])
            head = result[:match.start()]
            if size > self.server.config.max_literal_bytes or match[2]:
                await self.line('* BYE Unsupported literal framing')
                return None
            if head.split()[1:2] and head.split()[1].upper() == b'APPEND':
                await self.line(head.split()[0].decode('ascii') + ' NO Uploads are disabled')
                return ''
            await self.line('+ Ready for literal')
            literal = await asyncio.wait_for(self.reader.readexactly(size), 30)
            suffix = await asyncio.wait_for(self.reader.readline(), 30)
            if not suffix.endswith(b'\r\n'):
                raise CommandError('Invalid literal framing')
            placeholder = '__literal_' + uuid.uuid4().hex
            self.literal_values[placeholder] = literal.decode('utf-8')
            result = head + placeholder.encode() + suffix[:-2]
            if len(result) > self.server.config.max_command_bytes:
                raise CommandError('Command too large')
        return result.decode('utf-8')

    async def heartbeat(self):
        """Send complete idle responses, never insert bytes into a FETCH literal."""
        try:
            while True:
                await asyncio.sleep(self.server.config.heartbeat_interval)
                if self.busy or self.writer.is_closing():
                    continue
                async with self.write_lock:
                    if not self.busy and not self.writer.is_closing():
                        await self.line('* OK Keepalive')
        except (ConnectionError, TimeoutError):
            self.writer.close()

    async def run(self):
        heartbeat = asyncio.create_task(self.heartbeat())
        try:
            await self.line('* OK AgentMail IMAP bridge ready')
            while not self.writer.is_closing():
                command = await self.read_command()
                if command is None:
                    break
                if not command:
                    continue
                tag = command.split(' ', 1)[0]
                if not tag or any(c in tag for c in '(){%*"\\]'):
                    break
                async with self.write_lock:
                    self.busy = True
                    try:
                        text = command[len(tag):].strip()
                        parts = text.upper().split()
                        is_fetch = parts[:1] == ['FETCH'] or parts[:2] == ['UID','FETCH']
                        async with asyncio.timeout(None if is_fetch else self.server.config.command_timeout):
                            if is_fetch:
                                await self.until_disconnect(self.command(tag, text))
                            else:
                                await self.command(tag, text)
                    except UnsupportedCharset:
                        await self.line(f'{tag} NO [BADCHARSET (US-ASCII UTF-8)] Unsupported search charset')
                    except CommandError as exc:
                        await self.line(f'{tag} {exc.status} {exc}')
                    except ApiError as exc:
                        logger.warning('Command upstream failure status=%s revoked=%s',exc.status,exc.revoked)
                        if self.in_response:
                            break
                        await self.line(f'{tag} NO Upstream access failed')
                        if exc.revoked:
                            await self.line('* BYE Authorization lost')
                            break
                    except (ValueError, TypeError, IndexError) as exc:
                        logger.warning('Command validation failure category=%s',type(exc).__name__)
                        if self.in_response:
                            break
                        await self.line(f'{tag} BAD Invalid arguments')
                    except Exception as exc:
                        logger.warning('Command operational failure category=%s',type(exc).__name__)
                        if self.in_response:
                            break
                        await self.line(f'{tag} NO Operation failed')
                    finally:
                        self.busy = False
        except (ConnectionError, TimeoutError, asyncio.IncompleteReadError, ValueError, CommandError):
            pass
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self.server.leave(self)
            if self.adapter:
                await self.adapter.close()
            self.key = None
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()

    async def scan(self, *, periodic=False):
        async with self.server.api_slots:
            return await self._coordinated_scan(periodic=periodic)

    async def _coordinated_scan(self, *, periodic=False):
        if not hasattr(self.server.store, 'reconciliation_lock'):
            return await self._scan()
        started = await asyncio.to_thread(self.server.store.database_time)
        await self.adapter.authorize(self.inbox)
        namespace = self.adapter.namespace
        identity = hashlib.sha256(self.key.encode()).digest()
        async with self.server.store.reconciliation_lock(namespace, self.inbox) as lease:
            # Foreground refresh can share only a scan completed after it began;
            # background refresh may reuse the latest authorized 60-second view.
            since = started - self.server.config.refresh_interval if periodic else started
            cached = await asyncio.to_thread(self.server.store.read_snapshot,
                namespace, self.inbox, identity, since=since)
            if cached is not None:
                await self.adapter.authorize(self.inbox)
                if self.adapter.namespace != namespace:
                    raise CommandError('Authorization identity changed during refresh','NO')
                await asyncio.to_thread(self.server.store.assert_lease,lease)
                return cached
            result = await self._scan(lease=lease)
            await asyncio.to_thread(self.server.store.write_snapshot,
                namespace, self.inbox, identity, result, lease=lease)
            return result

    async def _scan(self, *, lease=None):
        await self.adapter.authorize(self.inbox)
        namespace = self.adapter.namespace
        await self.observe_events()
        revision = await asyncio.to_thread(self.server.store.revision, namespace, self.inbox)
        active = await asyncio.to_thread(self.server.store.active_ids, namespace, self.inbox)
        messages = await self.adapter.list_messages(self.inbox)
        if len(messages) > 100000:
            raise CommandError('Mailbox metadata limit exceeded', 'NO')
        for member in self.server.sessions:
            if member.inbox == self.inbox and member.adapter and member.adapter.namespace == namespace:
                previous = {m.message_id:m for m in member.messages}
                for message in messages:
                    old = previous.get(message.message_id)
                    if old and (old.size != message.size or old.timestamp != message.timestamp):
                        raise CommandError('Upstream immutable metadata changed','NO')
        removed = []
        for identifier in active - {m.message_id for m in messages}:
            try:
                message = await self.adapter.metadata(self.inbox, identifier)
            except ApiError as exc:
                if exc.status == 404 and not exc.revoked and self.server.config.assume_full_visibility:
                    removed.append(identifier)
                    continue
                raise
            if message.in_inbox:
                messages.append(message)
            else:
                removed.append(identifier)
        if removed:
            # Check inbox access again after the complete scan and missing-ID lookups.
            await self.adapter.authorize(self.inbox)
            if self.adapter.namespace != namespace:
                raise CommandError('Authorization identity changed during refresh','NO')
        kwargs = {'confirmed_removed':removed,'expected_revision':revision}
        if lease is not None: kwargs['lease'] = lease
        result = await asyncio.to_thread(self.server.store.reconcile, namespace, self.inbox, messages, **kwargs)
        if removed:
            logger.info('Mailbox reconciliation retired=%s active=%s',len(removed),len(result[2]))
            for identifier in removed:
                logger.info('Mailbox retired message_hash=%s', hashlib.sha256(identifier.encode()).hexdigest()[:16])
        return result
    async def refresh(self):
        if self.group:
            result = await self.group.refresh(self)
        else:
            result = await self.scan()
        self.pending = (result[0],result[1],list(result[2]))

    async def until_disconnect(self, command):
        work = asyncio.create_task(command)
        async def monitor():
            while not self.writer.is_closing() and not self.reader.at_eof():
                await asyncio.sleep(0.25)
        disconnected = asyncio.create_task(monitor())
        try:
            done, _ = await asyncio.wait((work,disconnected),return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return await work
            self.writer.close()
        finally:
            work.cancel()
            disconnected.cancel()
            await asyncio.gather(work,disconnected,return_exceptions=True)

    async def publish(self, allow_expunge, force_flags=False):
        if self.pending is None:
            return
        validity, next_uid, candidate = self.pending
        new_uids = {m.uid for m in candidate}
        removed = [i for i,m in enumerate(self.messages) if m.uid not in new_uids]
        if removed and not allow_expunge:
            return
        old = {m.uid: m for m in self.messages}
        for i in reversed(removed):
            await self.line(f'* {i+1} EXPUNGE')
        if len(candidate) != len(self.messages) or removed:
            await self.line(f'* {len(candidate)} EXISTS')
        self.messages = list(candidate)
        self.uidvalidity, self.uidnext = validity, next_uid
        self.recent.intersection_update(new_uids)
        newly_recent = await asyncio.to_thread(self.server.store.claim_recent, self.adapter.namespace, self.inbox, readonly=self.readonly)
        self.recent.update(newly_recent)
        if newly_recent:
            await self.line(f'* {len(self.recent)} RECENT')
        for i,m in enumerate(candidate, 1):
            if force_flags or (m.uid in old and m.flags != old[m.uid].flags):
                await self.line(f'* {i} FETCH (UID {m.uid} FLAGS ({" ".join(m.flags)}))')
        self.pending = None

    def require_selected(self):
        if not self.selected:
            raise CommandError('Select INBOX first', 'NO')

    async def command(self, tag, text):
        parts = text.upper().split()
        if parts[:1] == ['FETCH'] or parts[:2] == ['UID','FETCH']:
            with persistent_retries():
                return await self._command(tag, text)
        return await self._command(tag, text)

    async def _command(self, tag, text):
        tokens = tokenize(text)
        def expand(items):
            return [expand(item) if isinstance(item,list) else getattr(self,'literal_values',{}).get(item,item) for item in items]
        tokens = expand(tokens)
        if not tokens or not isinstance(tokens[0], str):
            raise CommandError('Missing command')
        name = tokens.pop(0).upper()
        uid_mode = name == 'UID'
        if uid_mode:
            if not tokens:
                raise CommandError('Missing UID command')
            name = tokens.pop(0).upper()
            if name not in ('FETCH','STORE','SEARCH'):
                raise CommandError('Unsupported UID command')
        if name == 'CAPABILITY':
            await self.line('* CAPABILITY IMAP4rev1 AUTH=PLAIN')
        elif name == 'LOGOUT':
            await self.line('* BYE Logging out')
            await self.line(f'{tag} OK LOGOUT completed')
            self.writer.close()
            return
        elif name in ('LOGIN', 'AUTHENTICATE'):
            if self.adapter:
                raise CommandError('Already authenticated')
            if name == 'LOGIN':
                if len(tokens) != 2 or not all(isinstance(t,str) for t in tokens):
                    raise CommandError('LOGIN requires inbox and key')
                inbox, key = tokens
            else:
                if len(tokens) != 1 or tokens[0].upper() != 'PLAIN':
                    raise CommandError('Unsupported authentication mechanism', 'NO')
                await self.line('+')
                data = await asyncio.wait_for(self.reader.readline(), 30)
                if data == b'*\r\n':
                    raise CommandError('Authentication cancelled')
                authz, inbox_b, key_b = base64.b64decode(data.strip(), validate=True).split(b'\0')
                inbox, key = inbox_b.decode(), key_b.decode()
                if authz and authz != inbox_b:
                    raise CommandError('Authorization identity differs', 'NO')
            adapter = Adapter(self.server.config.api_url, key, policy=self.server.policy_for(key))
            try:
                await adapter.authorize(inbox)
            except Exception as exc:
                logger.warning('Authentication failed category=%s status=%s',type(exc).__name__,getattr(exc,'status',None))
                await adapter.close()
                await self.line(f'{tag} NO Authentication failed')
                await self.line('* BYE Authentication failed')
                self.writer.close()
                return
            self.adapter, self.inbox, self.key = adapter, inbox, key
            self.authorized_at = monotonic()
            self.server.join(self)
            logger.info('Session authenticated')
        elif name == 'NOOP' and not self.selected:
            pass
        else:
            if not self.adapter:
                raise CommandError('Authenticate first', 'NO')
            if name in ('SELECT','EXAMINE'):
                self.selected = False
                self.messages = []
                if len(tokens) != 1 or str(tokens[0]).upper() != 'INBOX':
                    raise CommandError('Only INBOX exists', 'NO')
                await self.refresh()
                self.uidvalidity,self.uidnext,self.messages = self.pending
                self.pending = None
                self.readonly = name == 'EXAMINE'
                self.recent = await asyncio.to_thread(self.server.store.claim_recent, self.adapter.namespace, self.inbox, readonly=self.readonly)
                self.selected = True
                logger.info('Mailbox selected count=%s',len(self.messages))
                self.server.join(self)
                await self.line('* FLAGS (\\Seen \\Flagged \\Recent)')
                await self.line('* OK [PERMANENTFLAGS (' + ('' if self.readonly else '\\Seen') + ')] Supported flags')
                await self.line(f'* {len(self.messages)} EXISTS')
                await self.line(f'* {len(self.recent)} RECENT')
                await self.line(f'* OK [UIDVALIDITY {self.uidvalidity}] Stable identity')
                await self.line(f'* OK [UIDNEXT {self.uidnext}] Next UID')
                unseen = next((i for i,m in enumerate(self.messages,1) if '\\Seen' not in m.flags), None)
                if unseen:
                    await self.line(f'* OK [UNSEEN {unseen}] First unread')
                await self.line(f'{tag} OK [{"READ-ONLY" if self.readonly else "READ-WRITE"}] {name} completed')
                return
            if name != 'NOOP':
                await self.adapter.authorize(self.inbox)
                self.authorized_at = monotonic()
            if name in ('LIST','LSUB'):
                if len(tokens) != 2:
                    raise CommandError('Expected reference and pattern')
                import re
                ref,pattern = tokens
                combined = (ref + pattern) if ref and not str(pattern).upper().startswith('INBOX') else pattern
                expression = '^' + ''.join('.*' if c == '*' else '[^/]*' if c == '%' else re.escape(c) for c in combined) + '$'
                if not pattern:
                    await self.line(f'* {name} (\\Noselect) "/" ""')
                elif re.match(expression,'INBOX',re.I):
                    await self.line(f'* {name} (\\HasNoChildren) "/" "INBOX"')
            elif name == 'STATUS':
                if len(tokens) != 2 or str(tokens[0]).upper() != 'INBOX' or not isinstance(tokens[1],list):
                    raise CommandError('Invalid STATUS')
                await self.refresh()
                validity,next_uid,messages = self.pending
                recent = await asyncio.to_thread(self.server.store.claim_recent, self.adapter.namespace, self.inbox, readonly=True)
                values = {'MESSAGES':len(messages),'RECENT':len(recent),'UIDNEXT':next_uid,'UIDVALIDITY':validity,'UNSEEN':sum('\\Seen' not in m.flags for m in messages)}
                if any(str(a).upper() not in values for a in tokens[1]):
                    raise CommandError('Unsupported STATUS item')
                await self.line('* STATUS INBOX ('+' '.join(f'{a.upper()} {values[a.upper()]}' for a in tokens[1])+')')
            elif name == 'NOOP':
                try:
                    await self.refresh()
                    await self.publish(True, force_flags=True)
                except ApiError as exc:
                    logger.warning('NOOP refresh failed status=%s revoked=%s; prior view retained',exc.status,exc.revoked)
                    if exc.revoked:
                        raise
                    await self.line('* OK [ALERT] Mailbox refresh failed; previous view retained')
                except Exception:
                    await self.line('* OK [ALERT] Mailbox refresh failed; previous view retained')
            elif name in ('FETCH','STORE','SEARCH'):
                self.require_selected()
                await self.publish(uid_mode)
                if name == 'FETCH':
                    await self.fetch(tokens, uid_mode)
                elif name == 'STORE':
                    await self.store_flags(tokens, uid_mode)
                else:
                    await self.search(tokens, uid_mode)
            elif name in ('CHECK','CLOSE'):
                self.require_selected()
                await self.publish(True)
                if name == 'CLOSE':
                    self.server.leave(self)
                    self.selected = False
                    self.messages = []
            elif name in ('CREATE','DELETE','RENAME','COPY','EXPUNGE','APPEND','SUBSCRIBE','UNSUBSCRIBE','MOVE'):
                raise CommandError('Mutation is disabled', 'NO')
            else:
                raise CommandError('Unknown command')
        await self.line(f'{tag} OK {name} completed')

    def matching(self, spec, uid_mode):
        values = [m.uid for m in self.messages] if uid_mode else list(range(1,len(self.messages)+1))
        matched = set(parse_sequence_set(spec, values))
        return [(i,m) for i,m in enumerate(self.messages,1) if (m.uid if uid_mode else i) in matched]

    async def mark_seen(self, message_id, seen):
        store = self.server.store
        if not hasattr(store, 'reconciliation_lock'):
            return await self.adapter.set_seen(self.inbox, message_id, seen)
        namespace = self.adapter.namespace
        async with store.reconciliation_lock(namespace, self.inbox) as lease:
            await asyncio.to_thread(store.invalidate_snapshot, namespace, self.inbox, lease=lease)
            try:
                return await self.adapter.set_seen(self.inbox, message_id, seen)
            finally:
                # A successful upstream update followed by a metadata failure
                # must not leave other workers reusing pre-update flags.
                await asyncio.to_thread(store.invalidate_snapshot, namespace, self.inbox, lease=lease)

    async def store_flags(self, tokens, uid_mode):
        if len(tokens) != 3 or self.readonly:
            raise CommandError('STORE unavailable', 'NO')
        spec,operation,flags = tokens
        operation = operation.upper()
        silent = operation.endswith('.SILENT')
        base = operation.removesuffix('.SILENT')
        if base not in ('FLAGS','+FLAGS','-FLAGS'):
            raise CommandError('Invalid STORE operation')
        flags = flags if isinstance(flags,list) else [flags]
        if any(f.upper() != '\\SEEN' for f in flags):
            raise CommandError('Only Seen is writable', 'NO')
        selected = self.matching(spec, uid_mode)
        if base == 'FLAGS' and any('\\Flagged' in m.flags for _,m in selected):
            raise CommandError('Replacement would change a protected flag','NO')
        for i,m in selected:
            seen = bool(flags) if base == 'FLAGS' else base == '+FLAGS'
            if base != 'FLAGS' and not flags:
                continue
            updated = await self.mark_seen(m.message_id,seen)
            updated.uid = m.uid
            self.messages[i-1] = updated
            if not silent:
                await self.line(f'* {i} FETCH ('+(f'UID {m.uid} ' if uid_mode else '')+'FLAGS ('+' '.join(updated.flags)+'))')

    @contextlib.asynccontextmanager
    async def staged(self, selected):
        if len(selected) > self.server.config.max_batch_messages:
            raise CommandError('Batch message limit exceeded','NO')
        if sum(m.size for _,m in selected) > self.server.config.max_batch_bytes:
            raise CommandError('Temporary byte limit exceeded','NO')
        if any(m.size > self.server.config.max_message_bytes for _,m in selected):
            raise CommandError('Temporary byte limit exceeded','NO')
        with tempfile.TemporaryDirectory(prefix='fetch-',dir=self.server.config.temp_dir) as directory:
            async with contextlib.AsyncExitStack() as leases:
                paths = {}
                reserved = 0
                async def prepare(message):
                    nonlocal reserved
                    async def download(path):
                        async with self.server.fetch_slots:
                            await self.adapter.download(self.inbox,message.message_id,path,
                                                        max_bytes=self.server.config.max_message_bytes)
                        if path.stat().st_size != message.size:
                            raise CommandError('Raw size mismatch','NO')
                    credential = hashlib.sha256((self.key or '').encode()).digest()
                    cache_key = (self.adapter.namespace, self.inbox, credential,
                                 message.message_id, message.size, message.timestamp)
                    async def validate():
                        current = await self.adapter.metadata(self.inbox,message.message_id)
                        if current.size != message.size or current.timestamp != message.timestamp:
                            raise CommandError('Upstream immutable metadata changed','NO')
                    try:
                        path = await leases.enter_async_context(self.server.cache.lease(
                            cache_key,message.size,download,validate))
                    except CacheFull:
                        if self.server.temp_bytes+self.server.cache.bytes+message.size > self.server.config.max_temp_bytes:
                            raise CommandError('Temporary byte limit exceeded','NO')
                        self.server.temp_bytes += message.size
                        reserved += message.size
                        path = Path(directory)/str(message.uid)
                        await download(path)
                    paths[message.uid] = path
                tasks = [asyncio.create_task(prepare(message)) for _,message in selected]
                try:
                    # Parallel preparation, ordered publication. A failed group
                    # cancels its unfinished work; no later group is emitted.
                    await asyncio.gather(*tasks)
                    yield paths
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                    self.server.temp_bytes -= reserved

    async def fetch(self, tokens, uid_mode):
        if len(tokens) < 2:
            raise CommandError('FETCH requires set and attributes')
        selected = self.matching(tokens[0], uid_mode)
        attrs = parse_fetch_attributes(tokens[1] if len(tokens)==2 else tokens[1:])
        needs_raw = any(a.needs_raw for a in attrs)
        inbox_hash = hashlib.sha256(self.inbox.encode()).hexdigest()[:16]
        logger.info('FETCH started inbox_hash=%s total=%s raw=%s',inbox_hash,len(selected),needs_raw)
        async def emit(selected, paths):
            if needs_raw:
                await self.adapter.authorize(self.inbox)
                self.authorized_at = monotonic()
            # Verify every MIME descriptor/section before side effects or output.
            if needs_raw:
                for _,message in selected:
                    prepared_raw = RawMessage(await asyncio.to_thread(paths[message.uid].read_bytes))
                    for attribute in attrs:
                        if attribute.name == 'ENVELOPE': prepared_raw.envelope()
                        elif attribute.name in ('BODY','BODYSTRUCTURE') and attribute.section is None:
                            prepared_raw.bodystructure(attribute.name == 'BODYSTRUCTURE')
                        elif attribute.needs_raw:
                            prepared_raw.section({'RFC822.HEADER':'HEADER','RFC822.TEXT':'TEXT'}.get(attribute.name,attribute.section or ''))
                    del prepared_raw
            if not self.readonly and any(a.sets_seen for a in attrs):
                for i,m in selected:
                    if '\\Seen' not in m.flags:
                        updated = await self.mark_seen(m.message_id,True)
                        updated.uid = m.uid
                        self.messages[i-1] = updated
            for i,original in selected:
                if not needs_raw and monotonic()-self.authorized_at >= self.server.config.metadata_auth_interval:
                    await self.adapter.authorize(self.inbox)
                    self.authorized_at = monotonic()
                m = self.messages[i-1]
                raw = RawMessage(await asyncio.to_thread(paths[m.uid].read_bytes)) if needs_raw else None
                pieces = []
                if m.flags != original.flags and not any(a.name == 'FLAGS' for a in attrs):
                    pieces.append((b'FLAGS',('('+' '.join(m.flags+(['\\Recent'] if m.uid in self.recent else []))+')').encode()))
                if uid_mode and not any(a.name == 'UID' for a in attrs):
                    pieces.append((b'UID',str(m.uid).encode()))
                for a in attrs:
                    name = a.name.upper()
                    if name == 'UID': value = str(m.uid).encode()
                    elif name == 'FLAGS': value = ('('+' '.join(m.flags+(['\\Recent'] if m.uid in self.recent else []))+')').encode()
                    elif name == 'RFC822.SIZE': value = str(m.size).encode()
                    elif name == 'INTERNALDATE': value = m.timestamp.strftime('"%d-%b-%Y %H:%M:%S %z"').encode()
                    elif name == 'ENVELOPE': value = raw.envelope()
                    elif name in ('BODYSTRUCTURE','BODY') and a.section is None: value = raw.bodystructure(name=='BODYSTRUCTURE')
                    else:
                        value = raw.section({'RFC822.HEADER':'HEADER','RFC822.TEXT':'TEXT'}.get(name,a.section or ''))
                        if value is None:
                            pieces.append((a.response_name.encode(),b'NIL'))
                            continue
                        if a.partial:
                            offset,count = a.partial
                            value = value[offset:offset+count]
                        pieces.append((a.response_name.encode(),value,True))
                        continue
                    pieces.append((a.response_name.encode(),value))
                self.in_response = True
                self.writer.write(f'* {i} FETCH ('.encode())
                for n,piece in enumerate(pieces):
                    if n: self.writer.write(b' ')
                    self.writer.write(piece[0]+b' ')
                    if len(piece)==3:
                        self.writer.write(f'{{{len(piece[1])}}}\r\n'.encode())
                        for offset in range(0,len(piece[1]),65536):
                            self.writer.write(piece[1][offset:offset+65536])
                            await asyncio.wait_for(self.writer.drain(),self.server.config.write_timeout)
                    else: self.writer.write(piece[1])
                self.writer.write(b')\r\n')
                await asyncio.wait_for(self.writer.drain(),self.server.config.write_timeout)
                self.in_response = False
        # Bound groups by both count and bytes. Even an enormous FETCH request
        # can make progress without staging all its messages at once.
        position = 0
        while position < len(selected):
            group = []
            size = 0
            requested_limit = self.server.config.fetch_group_messages if needs_raw else self.server.config.metadata_group_messages
            limit = min(requested_limit,self.server.config.max_batch_messages)
            while position < len(selected) and len(group) < limit:
                item = selected[position]
                if needs_raw and group and size+item[1].size > self.server.config.max_batch_bytes:
                    break
                group.append(item)
                size += item[1].size
                position += 1
            if needs_raw:
                async with self.staged(group) as paths:
                    await emit(group,paths)
            else:
                await emit(group,{})
            logger.info('FETCH progress inbox_hash=%s delivered=%s total=%s cache_hits=%s cache_misses=%s',
                        inbox_hash,position,len(selected),self.server.cache.hits,self.server.cache.misses)

    async def search(self, tokens, uid_mode):
        from .protocol import SearchMessage
        charset = None
        if tokens and str(tokens[0]).upper() == 'CHARSET':
            if len(tokens)<3: raise CommandError('Missing search charset')
            charset,tokens = tokens[1],tokens[2:]
        node = parse_search(tokens,charset=charset)
        selected = list(enumerate(self.messages,1))
        async def evaluate(paths):
            found = []
            for i,m in selected:
                raw = await asyncio.to_thread(paths[m.uid].read_bytes) if node.needs_raw else None
                candidate = SearchMessage(sequence=i,uid=m.uid,flags=m.flags+(['\\Recent'] if m.uid in self.recent else []),internaldate=m.timestamp,size=m.size,raw=raw,max_sequence=len(self.messages),max_uid=max((x.uid for x in self.messages),default=0))
                if node.evaluate(candidate): found.append(m.uid if uid_mode else i)
            await self.line('* SEARCH'+(' '+' '.join(map(str,found)) if found else ''))
        if node.needs_raw:
            async with self.staged(selected) as paths: await evaluate(paths)
        else: await evaluate({})
