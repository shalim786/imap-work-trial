"""Initialize an empty hosted UID schema once; never reset existing identities."""
import sys
import time

import psycopg

from agentmail_imap.config import Config
from agentmail_imap.postgres_store import PostgresUIDStore, SCHEMA

# Session-scoped lock survives the schema transaction commit and serializes boots.
INITIALIZATION_LOCK = 70419938418522
EXPECTED_COLUMNS = {
    'settings': 'name,value',
    'mailboxes': 'id,namespace,inbox,uidvalidity,next_uid,revision',
    'message_uids': 'mailbox_id,message_id,uid,active,recent',
    'request_budgets': 'credential_hash,next_request,cooldown_until',
    'mailbox_leases': 'namespace,inbox,owner,fence,expires_at',
    'mailbox_snapshots': 'namespace,inbox,credential_hash,created_at,payload',
}
EXPECTED_TABLES = set(EXPECTED_COLUMNS)


def ensure_database(dsn):
    with psycopg.connect(dsn, connect_timeout=10, autocommit=True) as db:
        db.execute("SET statement_timeout = '120s'")
        db.execute('SELECT pg_advisory_lock(%s)', (INITIALIZATION_LOCK,))
        try:
            tables = db.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").fetchall()
            if not tables:
                with db.transaction():
                    db.execute(SCHEMA)
                    db.execute("INSERT INTO settings VALUES ('generation',%s)", (int(time.time()),))
            actual_tables = {row[0] for row in db.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").fetchall()}
            if actual_tables != EXPECTED_TABLES:
                raise ValueError('Unexpected or incomplete hosted UID schema')
            # Check all runtime tables are queryable before accepting the schema.
            for table in sorted(EXPECTED_TABLES):
                db.execute(f'SELECT {EXPECTED_COLUMNS[table]} FROM {table} LIMIT 0')
            # Existing or partial schemas must pass the server's normal validation.
            # Never repair by deleting data or reassigning UIDs.
            store = PostgresUIDStore(dsn)
            store.close()
        finally:
            db.execute('SELECT pg_advisory_unlock(%s)', (INITIALIZATION_LOCK,))


def main():
    try:
        config = Config.from_env()
        if config.storage_backend != 'postgres':
            raise ValueError('Hosted initialization requires PostgreSQL')
        ensure_database(config.postgres_conninfo())
    except Exception:
        print('Hosted UID initialization failed; database unavailable or schema invalid', file=sys.stderr)
        return 1
    print('Hosted UID schema ready', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
