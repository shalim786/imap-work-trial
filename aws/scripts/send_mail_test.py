"""Send a numbered, idempotent batch and verify the recipient's actual inbox."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import time
from urllib.parse import quote
import uuid

from agentmail_imap.adapter import Adapter, ApiError
from agentmail_imap.request_policy import RequestPolicy, persistent_retries


def load_settings(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            name, value = line.split('=',1)
            values[name.strip()] = value.strip().strip('\"\'')
    return {**values, **os.environ}


def subject_for(run, number):
    return f"[IMAP Test {run['run_id']}] {number:03d}/{run['count']:03d}"


def save_report(path, run):
    pending = path.with_suffix('.pending')
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor,'w') as output:
        json.dump(run,output,indent=2)
        output.flush()
        os.fsync(output.fileno())
    pending.replace(path)


async def send_batch(sender, run, checkpoint):
    for number in range(1,run['count']+1):
        if str(number) in run['sent']:
            continue
        subject = subject_for(run,number)
        # Reusing a run/number always reuses both payload and idempotency key.
        # No date or random value is regenerated inside the request body.
        key = str(uuid.uuid5(uuid.NAMESPACE_URL,f"imap-test:{run['run_id']}:{number}"))
        with persistent_retries():
            result = await sender._call(sender._client.inboxes.messages.send,
                quote(run['sender'],safe=''), mutation=True,
                to=[run['recipient']], subject=subject,
                text=f"AgentMail to Thunderbird delivery test.\nRun: {run['run_id']}\nMessage: {number}/{run['count']}\nSender: {run['sender']}\nRecipient: {run['recipient']}\n",
                idempotency_key=key)
        run['sent'][str(number)] = {'subject':subject,'message_id':result.message_id,
                                  'submitted_at':datetime.now(timezone.utc).isoformat()}
        checkpoint()
        print(f"Sent {number}/{run['count']}",flush=True)


async def received_batch(recipient, run):
    expected = {subject_for(run,n):n for n in range(1,run['count']+1)}
    found = {}
    token = None
    tokens = set()
    while True:
        page = await recipient._call(recipient._client.inboxes.messages.list,
            quote(run['recipient'],safe=''),limit=100,page_token=token,labels=['received'],
            include_trash=True,include_spam=True,include_blocked=True,include_unauthenticated=True)
        for message in page.messages:
            number = expected.get(message.subject)
            if number and 'received' in message.labels and 'trash' not in message.labels:
                found.setdefault(str(number),set()).add(message.message_id)
        token = page.next_page_token
        if not token:
            return {number:sorted(ids) for number,ids in found.items()}
        if token in tokens or len(tokens)>=10000:
            raise ApiError('Invalid verification pagination')
        tokens.add(token)


async def run_test(settings, run, checkpoint, verify_seconds):
    api_url = settings.get('AGENTMAIL_API_URL','https://api.agentmail.to/v0')
    # One send per second; verification uses its recipient key independently.
    sender = Adapter(api_url,settings['MAIL_TEST_SENDER_API_KEY'],policy=RequestPolicy(1,1,1,60))
    recipient = Adapter(api_url,settings['MAIL_TEST_RECIPIENT_API_KEY'],policy=RequestPolicy(2,1,1,60))
    started = time.monotonic()
    try:
        await sender.authorize(run['sender'])
        await recipient.authorize(run['recipient'])
        await send_batch(sender,run,checkpoint)
        run['send_elapsed_seconds'] = round(time.monotonic()-started,2)
        checkpoint()
        deadline = time.monotonic()+verify_seconds
        while True:
            run['received'] = await received_batch(recipient,run)
            duplicate_numbers = [n for n,ids in run['received'].items() if len(ids)>1]
            run['duplicate_numbers'] = duplicate_numbers
            checkpoint()
            print(f"Recipient verified {len(run['received'])}/{run['count']}",flush=True)
            if len(run['received'])==run['count']:
                run['completed_at']=datetime.now(timezone.utc).isoformat()
                run['elapsed_seconds']=round(time.monotonic()-started,2)
                checkpoint()
                if duplicate_numbers:
                    raise ApiError('Duplicate testcase deliveries detected')
                return
            if time.monotonic()>=deadline:
                raise ApiError('Delivery verification deadline reached; report preserves missing numbers')
            await asyncio.sleep(5)
    finally:
        await sender.close()
        await recipient.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-file',default='.env.mail_test')
    parser.add_argument('--count',type=int,default=100)
    parser.add_argument('--run-id',help='Resume an existing run within 24 hours; omit for a new batch')
    parser.add_argument('--verify-seconds',type=float,default=180)
    args=parser.parse_args()
    if not 1<=args.count<=100 or args.verify_seconds<=0:
        parser.error('count must be 1..100 and verify-seconds must be positive')
    logging.basicConfig(level=logging.WARNING,format='%(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try:
        settings=load_settings(args.credentials_file)
        run_id=args.run_id or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',run_id):
            raise ValueError('Invalid run ID')
        root=Path('var/mail-tests');root.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=root/f'{run_id}.json'
        descriptor=os.open(root/f'{run_id}.lock',os.O_WRONLY|os.O_CREAT,0o600)
        with os.fdopen(descriptor,'w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if path.exists():
                run=json.loads(path.read_text())
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(run['started_at'])).total_seconds()
                if age>=23*3600:
                    raise ValueError('Resume window expired; create a new run')
                if (run['sender'],run['recipient'],run['count'])!=(settings['MAIL_TEST_SENDER_INBOX'],settings['MAIL_TEST_RECIPIENT_INBOX'],args.count):
                    raise ValueError('Existing run parameters differ')
            else:
                run={'run_id':run_id,'sender':settings['MAIL_TEST_SENDER_INBOX'],
                     'recipient':settings['MAIL_TEST_RECIPIENT_INBOX'],'count':args.count,
                     'started_at':datetime.now(timezone.utc).isoformat(),'sent':{},'received':{}}
                save_report(path,run)
            print(f"Run {run_id}: {run['sender']} -> {run['recipient']} ({run['count']} messages)",flush=True)
            asyncio.run(run_test(settings,run,lambda:save_report(path,run),args.verify_seconds))
            print(f"Complete. Report: {path.resolve()}",flush=True)
        return 0
    except ApiError as exc:
        print(f'Test failed: {exc}; status={exc.status} code={exc.code}',flush=True)
    except (KeyError,OSError,ValueError):
        print('Test configuration/report error; check the private settings and report',flush=True)
    return 1


if __name__=='__main__':
    raise SystemExit(main())
