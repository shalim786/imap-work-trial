"""Offline, transactional SQLite -> PostgreSQL identity import. No API calls."""
import argparse
import os
import sqlite3
from pathlib import Path

from agentmail_imap.postgres_store import PostgresUIDStore
from agentmail_imap.uid_store import UIDStore, UIDStoreError


def migrate(source, dsn):
    # Validation opens SQLite in rw mode but refuses missing/replaced/corrupt files.
    source_store = UIDStore(source)
    target = PostgresUIDStore(dsn)
    try:
        with source_store._lock, target._db() as db:
            source_store._check()
            source_store.db.execute('BEGIN')
            try:
                settings = [(name,2 if name=='schema_version' else value) for name,value in source_store.db.execute('SELECT name,value FROM settings')]
                mailboxes = list(source_store.db.execute('SELECT id,namespace,inbox,uidvalidity,next_uid,revision FROM mailboxes'))
                rows = list(source_store.db.execute('SELECT mailbox_id,message_id,uid,active,recent FROM message_uids'))
            finally:
                source_store.db.rollback()
            db.execute('LOCK TABLE settings,mailboxes,message_uids IN ACCESS EXCLUSIVE MODE')
            if db.execute('SELECT 1 FROM mailboxes LIMIT 1').fetchone() or db.execute('SELECT 1 FROM message_uids LIMIT 1').fetchone():
                raise UIDStoreError('Migration requires an empty destination; overwrite refused')
            db.execute('DELETE FROM settings')
            with db.cursor() as cursor:
                cursor.executemany('INSERT INTO settings VALUES (%s,%s)',settings)
                cursor.executemany('INSERT INTO mailboxes(id,namespace,inbox,uidvalidity,next_uid,revision) VALUES (%s,%s,%s,%s,%s,%s)',mailboxes)
                cursor.executemany('INSERT INTO message_uids VALUES (%s,%s,%s,%s,%s)',rows)
            db.execute("SELECT setval(pg_get_serial_sequence('mailboxes','id'),COALESCE(MAX(id),1),COUNT(*)>0) FROM mailboxes")
            if db.execute('SELECT COUNT(*) FROM message_uids').fetchone()[0] != len(rows):
                raise UIDStoreError('Migration verification failed')
            db.execute('DELETE FROM mailbox_snapshots')
            return len(mailboxes),len(rows)
    finally:
        source_store.close()
        target.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sqlite',type=Path,required=True)
    args = parser.parse_args()
    try:
        from agentmail_imap.config import Config
        dsn = Config.from_env().postgres_conninfo()
        mailboxes,rows = migrate(args.sqlite,dsn)
    except Exception:
        parser.exit(1,'UID migration failed; source unchanged, destination transaction rolled back\n')
    print(f'Imported {mailboxes} mailboxes and {rows} UID history rows')


if __name__ == '__main__': main()
