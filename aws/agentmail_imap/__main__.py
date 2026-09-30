"""Explicit UID initialization and one-command local startup."""
import argparse
import dataclasses
import asyncio
import signal
import logging
import sys
from .config import Config
from .temp_storage import run_directory
from .uid_store import UIDStore, UIDStoreError

async def serve(config, store):
    from .server import Server
    from .health import HealthServer
    config.temp_dir.mkdir(parents=True,exist_ok=True)
    server = Server(config,store)
    listener = await server.start()
    health = HealthServer(store,config.health_port)
    await health.start()
    address = listener.sockets[0].getsockname()
    print(f'IMAP ready {address[0]}:{address[1]} mode={config.tls_mode}',flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        loop.add_signal_handler(sig,stop.set)
    try:
        await stop.wait()
    finally:
        health.ready = False
        await server.start_draining(config.drain_seconds)
        await health.close()
        await server.close()

def main():
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',nargs='?',default='serve',choices=['serve','init-db','backup-db','reset-db'])
    parser.add_argument('--destination')
    parser.add_argument('--previous-uidvalidity',type=int)
    args = parser.parse_args()
    store = None
    try:
        config = Config.from_env()
        if config.storage_backend == 'postgres':
            from .postgres_store import PostgresUIDStore
            store_type, location = PostgresUIDStore, config.postgres_conninfo()
        else:
            store_type, location = UIDStore, config.db_path
        if args.action == 'init-db':
            store = store_type.initialize(location)
            print('UID database initialized')
        else:
            store = store_type(location)
            if config.storage_backend == 'postgres' and args.action in ('reset-db','backup-db'):
                raise ValueError('Use RDS snapshots and an explicit recovery procedure for PostgreSQL')
            if args.action == 'reset-db':
                if args.previous_uidvalidity is None: parser.error('--previous-uidvalidity is required')
                print(f'New UIDVALIDITY generation: {store.reset_generation(args.previous_uidvalidity)}')
            elif args.action == 'backup-db':
                if not args.destination: parser.error('--destination is required')
                store.backup(args.destination)
                print('Consistent UID backup created')
            else:
                with run_directory(config.temp_dir) as private_dir:
                    asyncio.run(serve(dataclasses.replace(config,temp_dir=private_dir),store))
    except (UIDStoreError,ValueError,OSError) as exc:
        print('Startup failed: configuration, database, or listener unavailable',file=sys.stderr)
        return 1
    except Exception:
        print('Startup failed: database or service unavailable',file=sys.stderr)
        return 1
    finally:
        if store: store.close()
    return 0

if __name__ == '__main__':
    sys.exit(main())
