"""Async IMAP transport, session state, and mailbox coordination."""
from __future__ import annotations
import asyncio
import base64
import contextlib
import dataclasses
import datetime as dt
import json
import logging
import tempfile
import uuid
from pathlib import Path
from .protocol import tokenize, parse_sequence_set, parse_fetch_attributes, parse_search, UnsupportedCharset
from .mime import RawMessage
from .adapter import Adapter, ApiError

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
        self.listener = None

    async def start(self):
        self.listener = await asyncio.start_server(self.accept, self.config.host, self.config.port, limit=self.config.max_command_bytes)
        return self.listener

    async def accept(self, reader, writer):
        if len(self.sessions) >= self.config.max_connections:
            writer.write(b'* BYE Connection limit\r\n')
            await writer.drain()
            writer.close()
            return
        session = Session(self, reader, writer)
        self.sessions.add(session)
        try:
            await session.run()
        finally:
            self.sessions.discard(session)

    async def close(self):
        if self.listener:
            self.listener.close()
            await self.listener.wait_closed()
        for session in list(self.sessions):
            session.writer.close()
        for group in list(self.groups.values()):
            group.task.cancel()
        await asyncio.gather(*(g.task for g in list(self.groups.values())), return_exceptions=True)

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
                self.groups.pop(group.key, None)

class Group:
    def __init__(self, server, key):
        self.server, self.key = server, key
        self.sessions = set()
        self.lock = asyncio.Lock()
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        while True:
            await asyncio.sleep(self.server.config.refresh_interval)
            if not self.sessions:
                return
            session = next(iter(self.sessions))
            try:
                async with self.lock:
                    async with asyncio.timeout(self.server.config.command_timeout):
                        candidate = await session.scan()
                for member in list(self.sessions):
                    member.pending = candidate
                    # An idle boundary may announce arrivals/flags, but never EXPUNGE.
                    async with member.write_lock:
                        if member.selected and not member.busy:
                            await member.adapter.authorize(member.inbox)
                            await member.publish(False)
            except asyncio.CancelledError:
                raise
            except ApiError as exc:
                if exc.revoked:
                    for member in list(self.sessions):
                        member.writer.close()
            except Exception:
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

    async def run(self):
        await self.line('* OK AgentMail IMAP bridge ready')
        try:
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
                        async with asyncio.timeout(self.server.config.command_timeout):
                            await self.command(tag, command[len(tag):].strip())
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
        except (ConnectionError, asyncio.IncompleteReadError, ValueError, CommandError):
            pass
        finally:
            self.server.leave(self)
            if self.adapter:
                await self.adapter.close()
            self.key = None
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()

    async def scan(self):
        async with self.server.api_slots:
            await self.adapter.authorize(self.inbox)
            namespace = self.adapter.namespace
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
                message = await self.adapter.metadata(self.inbox, identifier)
                if 'received' in message.labels:
                    messages.append(message)
                else:
                    removed.append(identifier)
            return await asyncio.to_thread(self.server.store.reconcile, namespace, self.inbox, messages, confirmed_removed=removed, expected_revision=revision)

    async def refresh(self):
        if self.group:
            async with self.group.lock:
                self.pending = await self.scan()
        else:
            self.pending = await self.scan()

    async def publish(self, allow_expunge):
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
            if m.uid in old and m.flags != old[m.uid].flags:
                await self.line(f'* {i} FETCH (FLAGS ({" ".join(m.flags)}))')
        self.pending = None

    def require_selected(self):
        if not self.selected:
            raise CommandError('Select INBOX first', 'NO')

    async def command(self, tag, text):
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
            adapter = Adapter(self.server.config.api_url, key)
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
            logger.info('Session authenticated')
        elif name == 'NOOP' and not self.selected:
            pass
        else:
            if not self.adapter:
                raise CommandError('Authenticate first', 'NO')
            if name in ('SELECT','EXAMINE'):
                self.server.leave(self)
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
                validity,next_uid,messages = await self.scan()
                recent = await asyncio.to_thread(self.server.store.claim_recent, self.adapter.namespace, self.inbox, readonly=True)
                values = {'MESSAGES':len(messages),'RECENT':len(recent),'UIDNEXT':next_uid,'UIDVALIDITY':validity,'UNSEEN':sum('\\Seen' not in m.flags for m in messages)}
                if any(str(a).upper() not in values for a in tokens[1]):
                    raise CommandError('Unsupported STATUS item')
                await self.line('* STATUS INBOX ('+' '.join(f'{a.upper()} {values[a.upper()]}' for a in tokens[1])+')')
            elif name == 'NOOP':
                try:
                    await self.refresh()
                    await self.publish(True)
                except ApiError as exc:
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
            updated = await self.adapter.set_seen(self.inbox,m.message_id,seen)
            updated.uid = m.uid
            self.messages[i-1] = updated
            if not silent:
                await self.line(f'* {i} FETCH ('+(f'UID {m.uid} ' if uid_mode else '')+'FLAGS ('+' '.join(updated.flags)+'))')

    @contextlib.asynccontextmanager
    async def staged(self, selected):
        if len(selected) > self.server.config.max_batch_messages:
            raise CommandError('Batch message limit exceeded','NO')
        async with self.server.fetch_slots:
            with tempfile.TemporaryDirectory(prefix='fetch-',dir=self.server.config.temp_dir) as directory:
                paths = {}
                reserved = 0
                try:
                    for _,m in selected:
                        if m.size > self.server.config.max_message_bytes or reserved+m.size > self.server.config.max_batch_bytes or self.server.temp_bytes+m.size > self.server.config.max_temp_bytes:
                            raise CommandError('Temporary byte limit exceeded','NO')
                        self.server.temp_bytes += m.size
                        reserved += m.size
                        path = Path(directory)/str(m.uid)
                        await self.adapter.download(self.inbox,m.message_id,path,max_bytes=self.server.config.max_message_bytes)
                        if path.stat().st_size != m.size:
                            raise CommandError('Raw size mismatch','NO')
                        paths[m.uid] = path
                    yield paths
                finally:
                    self.server.temp_bytes -= reserved

    async def fetch(self, tokens, uid_mode):
        if len(tokens) < 2:
            raise CommandError('FETCH requires set and attributes')
        selected = self.matching(tokens[0], uid_mode)
        attrs = parse_fetch_attributes(tokens[1] if len(tokens)==2 else tokens[1:])
        needs_raw = any(a.needs_raw for a in attrs)
        async def emit(paths):
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
                        updated = await self.adapter.set_seen(self.inbox,m.message_id,True)
                        updated.uid = m.uid
                        self.messages[i-1] = updated
            for i,original in selected:
                await self.adapter.raw_info(self.inbox,original.message_id)
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
        if needs_raw:
            async with self.staged(selected) as paths:
                await emit(paths)
        else:
            await emit({})

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
                await self.adapter.raw_info(self.inbox,m.message_id)
                raw = await asyncio.to_thread(paths[m.uid].read_bytes) if node.needs_raw else None
                candidate = SearchMessage(sequence=i,uid=m.uid,flags=m.flags+(['\\Recent'] if m.uid in self.recent else []),internaldate=m.timestamp,size=m.size,raw=raw,max_sequence=len(self.messages),max_uid=max((x.uid for x in self.messages),default=0))
                if node.evaluate(candidate): found.append(m.uid if uid_mode else i)
            await self.line('* SEARCH'+(' '+' '.join(map(str,found)) if found else ''))
        if node.needs_raw:
            async with self.staged(selected) as paths: await evaluate(paths)
        else: await evaluate({})
