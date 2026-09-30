"""Durable identities only: no credentials, headers or email contents."""
from contextlib import closing
import os
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

MAX_UID = 2**32 - 1
SCHEMA = '''
CREATE TABLE settings (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE mailboxes (id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, inbox TEXT NOT NULL,
 uidvalidity INTEGER NOT NULL, next_uid INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 0,
 UNIQUE(namespace,inbox));
CREATE TABLE message_uids (mailbox_id INTEGER NOT NULL REFERENCES mailboxes(id), message_id TEXT NOT NULL,
 uid INTEGER NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)), recent INTEGER NOT NULL DEFAULT 1,
 UNIQUE(mailbox_id,uid));
CREATE UNIQUE INDEX active_identity ON message_uids(mailbox_id,message_id) WHERE active=1;
INSERT INTO settings VALUES ('schema_version',1);
'''


class UIDStoreError(Exception): pass


class UIDStore:
    @classmethod
    def initialize(cls,path, *, uidvalidity=None):
        path = Path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        generation = int(time.time()) if uidvalidity is None else uidvalidity
        if not 0 < generation <= MAX_UID: raise UIDStoreError('Invalid UIDVALIDITY generation')
        try: fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError: raise UIDStoreError('UID database already exists; initialization refuses overwrite') from None
        os.close(fd)
        try:
            with closing(sqlite3.connect(path)) as db:
                db.execute('PRAGMA synchronous=FULL')
                db.executescript(SCHEMA)
                db.execute("INSERT INTO settings VALUES ('generation',?)",(generation,))
                db.commit()
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return cls(path)

    def __init__(self,path):
        self.path = Path(path).absolute()
        self._lock = threading.RLock()
        if not self.path.is_file(): raise UIDStoreError('UID database missing; explicit initialization or recovery required')
        try:
            self.db = sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,check_same_thread=False)
            if self.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok': raise UIDStoreError('UID database corruption; explicit recovery required')
            if self.db.execute("SELECT value FROM settings WHERE name='schema_version'").fetchone() != (1,): raise UIDStoreError('Unsupported UID database schema')
            generation = self.db.execute("SELECT value FROM settings WHERE name='generation'").fetchone()
            if not generation or not 0 < generation[0] <= MAX_UID:
                raise UIDStoreError('Invalid UID generation; explicit recovery required')
            if self.db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise UIDStoreError('Invalid UID identity references; explicit recovery required')
            invalid_mailbox = self.db.execute('SELECT 1 FROM mailboxes WHERE uidvalidity<=0 OR uidvalidity>? OR next_uid<=0 OR next_uid>? OR revision<0 LIMIT 1',(MAX_UID,MAX_UID)).fetchone()
            invalid_uid = self.db.execute('SELECT 1 FROM message_uids u JOIN mailboxes m ON m.id=u.mailbox_id WHERE u.uid<=0 OR u.uid>=m.next_uid LIMIT 1').fetchone()
            if invalid_mailbox or invalid_uid:
                raise UIDStoreError('Invalid UID counters; explicit recovery required')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA busy_timeout=5000')
            stat = self.path.stat()
            self._identity = (stat.st_dev,stat.st_ino)
        except sqlite3.Error:
            raise UIDStoreError('UID database invalid; explicit recovery required') from None

    def _check(self):
        try:
            stat = self.path.stat()
            if (stat.st_dev,stat.st_ino) != self._identity: raise UIDStoreError('UID database replaced; explicit recovery required')
        except OSError: raise UIDStoreError('UID database lost; explicit recovery required') from None

    def _mailbox(self,namespace,inbox):
        return self.db.execute('SELECT id,uidvalidity,next_uid,revision FROM mailboxes WHERE namespace=? AND inbox=?',(namespace,inbox)).fetchone()

    def active_ids(self,namespace,inbox):
        with self._lock:
            self._check()
            mailbox = self._mailbox(namespace,inbox)
            return {row[0] for row in self.db.execute('SELECT message_id FROM message_uids WHERE mailbox_id=? AND active=1',(mailbox[0],))} if mailbox else set()

    def revision(self,namespace,inbox):
        with self._lock:
            self._check()
            mailbox = self._mailbox(namespace,inbox)
            return mailbox[3] if mailbox else 0

    def reconcile(self,namespace,inbox,messages, *, confirmed_removed=(),expected_revision=None):
        messages = list(messages)
        by_id = {m.message_id:m for m in messages}
        if len(by_id) != len(messages): raise UIDStoreError('Duplicate candidate message identities')
        confirmed = set(confirmed_removed)
        if confirmed & by_id.keys(): raise UIDStoreError('Conflicting candidate membership')
        with self._lock:
            self._check()
            try:
                self.db.execute('BEGIN IMMEDIATE')
                mailbox = self._mailbox(namespace,inbox)
                if mailbox is None:
                    generation = self.db.execute("SELECT value FROM settings WHERE name='generation'").fetchone()[0]
                    self.db.execute('INSERT INTO mailboxes(namespace,inbox,uidvalidity) VALUES (?,?,?)',(namespace,inbox,generation))
                    mailbox = self._mailbox(namespace,inbox)
                mid,validity,next_uid,revision = mailbox
                if expected_revision is not None and revision != expected_revision: raise UIDStoreError('Stale mailbox reconciliation; refresh again')
                mappings = dict(self.db.execute('SELECT message_id,uid FROM message_uids WHERE mailbox_id=? AND active=1',(mid,)))
                missing = mappings.keys() - by_id.keys()
                if missing - confirmed: raise UIDStoreError('Missing identities require confirmed folder removal')
                changed = False
                for identifier in missing:
                    self.db.execute('UPDATE message_uids SET active=0,recent=0 WHERE mailbox_id=? AND message_id=? AND active=1',(mid,identifier))
                    changed = True
                for message in sorted(messages,key=lambda m:(m.timestamp,m.message_id)):
                    if message.message_id not in mappings:
                        # Reserve representable UIDNEXT; never expose wrapped zero.
                        if next_uid >= MAX_UID: raise UIDStoreError('UID space exhausted; deliberate recovery required')
                        self.db.execute('INSERT INTO message_uids VALUES (?,?,?,?,?)',(mid,message.message_id,next_uid,1,1))
                        mappings[message.message_id] = next_uid
                        next_uid += 1
                        changed = True
                self.db.execute('UPDATE mailboxes SET next_uid=?,revision=? WHERE id=?',(next_uid,revision+int(changed),mid))
                result = sorted((replace(m,uid=mappings[m.message_id]) for m in messages),key=lambda m:m.uid)
                self.db.commit()
                return validity,next_uid,result
            except sqlite3.Error:
                self.db.rollback()
                raise UIDStoreError('UID database operation failed; recovery may be required') from None
            except Exception:
                self.db.rollback()
                raise

    def claim_recent(self,namespace,inbox, *, readonly=False):
        with self._lock:
            self._check()
            mailbox = self._mailbox(namespace,inbox)
            if not mailbox: return set()
            try:
                self.db.execute('BEGIN IMMEDIATE')
                uids = {row[0] for row in self.db.execute('SELECT uid FROM message_uids WHERE mailbox_id=? AND active=1 AND recent=1',(mailbox[0],))}
                if not readonly: self.db.execute('UPDATE message_uids SET recent=0 WHERE mailbox_id=? AND active=1',(mailbox[0],))
                self.db.commit()
                return uids
            except sqlite3.Error:
                self.db.rollback()
                raise UIDStoreError('UID database operation failed') from None

    def backup(self,destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True,exist_ok=True)
        with self._lock:
            self._check()
            try: fd = os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            except FileExistsError: raise UIDStoreError('Backup destination already exists') from None
            os.close(fd)
            try:
                with closing(sqlite3.connect(destination)) as target:
                    self.db.backup(target)
                    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise UIDStoreError('Backup verification failed')
            except Exception:
                destination.unlink(missing_ok=True)
                raise

    def reset_generation(self, previous_uidvalidity: int):
        """Explicit operator recovery; supplied floor must cover every exposed generation.

        Run only while the application is stopped. Existing higher generations win.
        Identity rows are cleared only after committing a strictly greater generation.
        """
        if type(previous_uidvalidity) is not int or not 0 < previous_uidvalidity < MAX_UID:
            raise UIDStoreError('A known previous UIDVALIDITY below the maximum is required')
        with self._lock:
            self._check()
            try:
                self.db.execute('BEGIN IMMEDIATE')
                current = self.db.execute("SELECT value FROM settings WHERE name='generation'").fetchone()[0]
                highest = self.db.execute('SELECT COALESCE(MAX(uidvalidity),0) FROM mailboxes').fetchone()[0]
                generation = max(previous_uidvalidity,current,highest) + 1
                if generation > MAX_UID: raise UIDStoreError('UIDVALIDITY exhausted; cannot recover safely')
                self.db.execute('DELETE FROM message_uids')
                self.db.execute('DELETE FROM mailboxes')
                self.db.execute("UPDATE settings SET value=? WHERE name='generation'",(generation,))
                self.db.commit()
                return generation
            except Exception:
                self.db.rollback()
                raise

    def close(self):
        with self._lock: self.db.close()
