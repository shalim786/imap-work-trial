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
    config.temp_dir.mkdir(parents=True,exist_ok=True)
    server = Server(config,store)
    listener = await server.start()
    address = listener.sockets[0].getsockname()
    print(f'IMAP ready {address[0]}:{address[1]}',flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        loop.add_signal_handler(sig,stop.set)
    try:
        await stop.wait()
    finally:
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
        if args.action == 'init-db':
            store = UIDStore.initialize(config.db_path)
            print('UID database initialized')
        else:
            store = UIDStore(config.db_path)
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
        print(f'Startup failed: {exc}',file=sys.stderr)
        return 1
    finally:
        if store: store.close()
    return 0

if __name__ == '__main__':
    sys.exit(main())
