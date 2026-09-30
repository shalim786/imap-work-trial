"""Repeatable synthetic failure/load evidence; never contacts real AgentMail."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import imaplib
import json
from pathlib import Path
import platform
import socket
import statistics
import subprocess
import time
from scripts.smoke_test import Sandbox, INBOX, KEY, ok, expect, bodies, fixtures


def connect(box):
    client = imaplib.IMAP4('127.0.0.1', box.port, timeout=60)
    ok(client.login(INBOX, KEY))
    ok(client.select('INBOX', readonly=True))
    return client


def timed_worker(box, barrier, rounds, expected_count):
    """Measure one client including login, paginated selection, and mixed FETCH."""
    started = time.monotonic()
    barrier.wait(timeout=60)
    client = connect(box)
    try:
        for _ in range(rounds):
            metadata = ok(client.uid('FETCH', '1:*', '(UID FLAGS INTERNALDATE RFC822.SIZE)'))
            expect(len(metadata) == expected_count, 'load metadata incomplete')
            rows = ok(client.uid('FETCH', '1:3', '(UID BODY.PEEK[])'))
            expect(sum(isinstance(row, tuple) for row in rows) == 3, 'load raw result incomplete')
            ok(client.noop())
            client.response('FETCH')  # Consume unsolicited flag updates before next FETCH.
        return time.monotonic() - started
    finally:
        client.logout()


def failure_evidence():
    results = {}
    with Sandbox(refresh=60) as box:
        client, _ = box.connect(readonly=True)
        before = bodies(client, {k:v for k,v in fixtures().items() if k != 'msg_new_arrival'})
        # Exhaust bounded non-FETCH retries, then recover without changing identities.
        box.control('fail-next', {'path_prefix':'/', 'status':503, 'times':3})
        started = time.monotonic()
        ok(client.noop())
        expect(client.response('ALERT')[1] != [None], 'outage did not report retained view')
        ok(client.noop())
        after = bodies(client, {k:v for k,v in fixtures().items() if k != 'msg_new_arrival'})
        expect(before == after, 'outage changed UIDs')
        results['outage_recovery_uid_stable'] = True
        results['outage_recovery_seconds'] = round(time.monotonic()-started, 3)
        # Bypass warm raw cache in a separate process while preserving the UID DB.
        box.restart()
        client, _ = box.connect(readonly=True)
        box.control('fail-next', {'path_prefix':'/raw/', 'status':429, 'times':6})
        started = time.monotonic()
        rows = ok(client.uid('FETCH', '1:3', '(UID BODY.PEEK[])'))
        expect(sum(isinstance(row,tuple) for row in rows)==3, 'retry FETCH missing literals')
        results['six_rate_limits_then_fetch_success'] = True
        results['rate_limit_recovery_seconds'] = round(time.monotonic()-started, 3)
        results['restart_uid_stable'] = bodies(client, {k:v for k,v in fixtures().items() if k != 'msg_new_arrival'}) == before
        invalid = imaplib.IMAP4('127.0.0.1', box.port, timeout=10)
        try:
            try:
                invalid.login(INBOX, 'synthetic-invalid')
                raise AssertionError('invalid credentials accepted')
            except imaplib.IMAP4.error:
                results['invalid_credentials_rejected'] = True
        finally:
            invalid.shutdown()
        ok(client.noop())
        results['service_survived_failures'] = True
    # A disconnected long-retry FETCH releases command/download resources.
    with Sandbox(refresh=60) as box:
        client, _ = box.connect(readonly=True)
        box.control('fail-next', {'path_prefix':'/raw/', 'status':503, 'times':100, 'delay_ms':50})
        client.send(b'ZZ UID FETCH 1:3 (UID BODY.PEEK[])\r\n')
        time.sleep(.1)
        client.shutdown()
        box.client=None
        time.sleep(.4)  # Disconnect monitor polls every 250 ms.
        survivor=connect(box)
        ok(survivor.noop())
        survivor.logout()
        results['disconnected_retry_does_not_block_refresh'] = True
    # Saturation rejects surplus clients without consuming existing slots forever.
    with Sandbox(refresh=60, overrides={'IMAP_MAX_CONNECTIONS':'4'}) as box:
        holders = [imaplib.IMAP4('127.0.0.1',box.port,timeout=10) for _ in range(4)]
        try:
            with socket.create_connection(('127.0.0.1',box.port),timeout=10) as surplus:
                expect(surplus.recv(1024).startswith(b'* BYE Connection limit'), 'connection cap not enforced')
            results['connection_limit_rejection'] = True
        finally:
            for client in holders: client.shutdown()
        time.sleep(.1)
        client=connect(box)
        client.logout()
        results['slots_recovered_after_disconnect'] = True
    return results


def run(clients, messages, rounds):
    import threading
    failures = failure_evidence()
    with Sandbox(refresh=60) as box:
        box.control('add-messages', {'count':messages})
        barrier=threading.Barrier(clients)
        started=time.monotonic()
        with ThreadPoolExecutor(max_workers=clients) as pool:
            futures=[pool.submit(timed_worker,box,barrier,rounds,messages+3) for _ in range(clients)]
            durations=[future.result(timeout=180) for future in futures]
        elapsed=time.monotonic()-started
        rss_kib=int(subprocess.check_output(['ps','-o','rss=','-p',str(box.server.pid)]).strip())
        samples=sorted(durations)
        return {'environment':{'os':platform.system(),'python':platform.python_version(),
                    'storage':'sqlite','upstream':'synthetic local HTTP','api_rps':1000,
                    'scope':'single process; not AWS or PostgreSQL capacity proof'},
                'failures':failures,
                'load':{'clients':clients,'mailbox_messages':messages+3,'rounds_per_client':rounds,
                    'mixed_commands':clients*rounds*3,'elapsed_seconds':round(elapsed,3),
                    'commands_per_second':round(clients*rounds*3/elapsed,2),
                    'client_p50_seconds':round(statistics.median(samples),3),
                    'client_p95_seconds':round(samples[min(len(samples)-1,int(len(samples)*.95))],3),
                    'client_max_seconds':round(max(samples),3),'server_rss_kib_after_load':rss_kib,
                    'errors':0,'metadata_complete':True,'raw_literals_complete':True}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clients',type=int,default=20)
    parser.add_argument('--messages',type=int,default=100)
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if not 1 <= args.clients <= 100 or not 1 <= args.messages <= 10000 or not 1 <= args.rounds <= 100:
        parser.error('clients 1..100, messages 1..10000, rounds 1..100')
    report=run(args.clients,args.messages,args.rounds)
    encoded=json.dumps(report,indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(encoded)
    print(encoded,end='')


if __name__=='__main__': main()
