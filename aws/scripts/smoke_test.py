"""Isolated real-process checks; all mail and credentials are synthetic."""
from __future__ import annotations

import base64
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
INBOX = 'candidate@imap.test'
KEY = 'test_agentmail_key'


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def ok(result):
    expect(result[0] == 'OK', f'IMAP command failed: {result[0]} {result[1]}')
    return result[1]


class Sandbox:
    def __init__(self, refresh=0.3, overrides=None):
        self.overrides = overrides or {}
        self.refresh = refresh
        self.children = []
        self.logs = []
        self.client = None

    def __enter__(self):
        self.workspace = tempfile.TemporaryDirectory(prefix='agentmail-imap-smoke-')
        self.path = Path(self.workspace.name)
        self.env = dict(os.environ, HARNESS_API_HOST='127.0.0.1', HARNESS_API_PORT='0',
                        IMAP_HOST='127.0.0.1', IMAP_PORT='0', IMAP_STORAGE_BACKEND='sqlite',
                        IMAP_TLS_MODE='local', IMAP_HEALTH_PORT='0',
                        IMAP_UID_DB=str(self.path / 'uids.sqlite3'),
                        IMAP_TEMP_DIR=str(self.path / 'raw'),
                        IMAP_API_REQUESTS_PER_SECOND='1000',
                        IMAP_RETRY_INITIAL='0.01', IMAP_RETRY_MAX='0.05',
                        IMAP_REFRESH_INTERVAL_SECONDS=str(self.refresh))
        self.env.update(self.overrides)
        try:
            api = self.spawn(['node', str(ROOT / 'test-harness/fake-agentmail-api.mjs')], 'api')
            self.api = self.wait_log(api, 'api', r'AGENTMAIL_API_URL=(http://[^\s]+)')
            self.env['AGENTMAIL_API_URL'] = self.api
            self.control('health')
            subprocess.run([sys.executable, '-m', 'agentmail_imap', 'init-db'], cwd=ROOT,
                           env=self.env, check=True, capture_output=True, timeout=15)
            self.start_server()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def spawn(self, command, name):
        log = (self.path / f'{name}.log').open('w+')
        self.logs.append(log)
        process = subprocess.Popen(command, cwd=ROOT, env=self.env, stdout=log, stderr=log)
        self.children.append(process)
        return process

    def wait_log(self, process, name, pattern, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            contents = (self.path / f'{name}.log').read_text()
            match = re.search(pattern, contents)
            if match:
                return match.group(1)
            if process.poll() is not None:
                raise RuntimeError(f'{name} exited ({process.returncode}): {contents.replace(KEY, "[redacted]")}')
            time.sleep(.03)
        raise TimeoutError(f'{name} readiness timed out: {contents.replace(KEY, "[redacted]")}')

    def start_server(self):
        self.server = self.spawn([sys.executable, '-u', '-m', 'agentmail_imap'], 'server')
        self.port = int(self.wait_log(self.server, 'server', r'(?:127\.0\.0\.1:|IMAP_PORT=)(\d+)'))

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def connect(self, readonly=False):
        self.client = imaplib.IMAP4('127.0.0.1', self.port, timeout=10)
        ok(self.client.capability())
        ok(self.client.login(INBOX, KEY))
        listing = ok(self.client.list())
        expect(any(b'INBOX' in item for item in listing), 'INBOX missing from LIST')
        selected = ok(self.client.select('INBOX', readonly=readonly))
        return self.client, int(selected[0])

    def control(self, route, payload=None):
        url = self.api.removesuffix('/v0') + ('/health' if route == 'health' else '/_test/' + route)
        request = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def restart(self):
        if self.client:
            self.client.logout()
            self.client = None
        self.stop(self.server)
        self.start_server()

    def __exit__(self, *args):
        if self.client:
            try:
                self.client.shutdown()
            except OSError:
                pass
        for child in reversed(self.children):
            self.stop(child)
        for log in self.logs:
            log.close()
        self.workspace.cleanup()


def fixtures():
    """Import independent harness buffers, never the IMAP serializer."""
    source = "import {BASE_MESSAGES, ADDED_MESSAGE} from './test-harness/lib/fixtures.mjs'; console.log(JSON.stringify([...BASE_MESSAGES, ADDED_MESSAGE].map(m=>({id:m.id,labels:m.labels,raw:m.raw.toString('base64')}))));"
    output = subprocess.check_output(['node', '--input-type=module', '-e', source], cwd=ROOT)
    return {row['id']: base64.b64decode(row['raw']) for row in json.loads(output)
            if 'received' in row['labels'] and 'trash' not in row['labels']}


def bodies(client, expected):
    rows = ok(client.uid('FETCH', '1:*', '(UID BODY.PEEK[])'))
    by_hash = {hashlib.sha256(raw).hexdigest(): mid for mid, raw in expected.items()}
    mapping = {}
    for row in rows:
        if not isinstance(row, tuple):
            continue
        header, raw = row
        uid = int(re.search(rb'\bUID (\d+)', header)[1])
        declared = int(re.search(rb'\{(\d+)\}$', header)[1])
        expect(declared == len(raw), 'literal byte count mismatch')
        digest = hashlib.sha256(raw).hexdigest()
        expect(digest in by_hash, 'unexpected or altered raw message')
        mid = by_hash[digest]
        expect(raw == expected[mid], 'raw bytes differ')
        expect(mid not in mapping, 'duplicate message')
        mapping[mid] = uid
    expect(set(mapping) == set(expected), 'incomplete received membership')
    return mapping


def run_smoke():
    expected = fixtures()
    baseline = {mid: raw for mid, raw in expected.items() if mid != 'msg_new_arrival'}
    with Sandbox() as box:
        client, count = box.connect()
        expect(count == 3, 'SELECT did not include all three non-trash received messages')
        validity = client.response('UIDVALIDITY')[1]
        metadata = ok(client.uid('FETCH', '1:*', '(UID FLAGS INTERNALDATE RFC822.SIZE)'))
        expect(len(metadata) == 3, 'metadata pagination incomplete')
        before = box.control('state')
        original = bodies(client, baseline)
        sizes = {int(re.search(rb'UID (\d+)', row)[1]): int(re.search(rb'RFC822.SIZE (\d+)', row)[1]) for row in metadata}
        expect(all(sizes[uid] == len(baseline[mid]) for mid, uid in original.items()), 'metadata byte sizes differ from independent fixtures')
        expect(box.control('state') == before, 'PEEK changed upstream state')
        box.control('add-message', {})
        ok(client.noop())
        arrived = bodies(client, expected)
        expect(all(arrived[mid] == uid for mid, uid in original.items()), 'arrival renumbered UIDs')
        box.restart()
        client, count = box.connect()
        expect(count == 4, 'restart membership changed')
        expect(client.response('UIDVALIDITY')[1] == validity, 'restart UIDVALIDITY changed')
        expect(bodies(client, expected) == arrived, 'restart UID mapping changed')
        uid = arrived['msg_received_ascii']
        ok(client.uid('STORE', str(uid), '+FLAGS.SILENT', '(\\Seen)'))
        labels = {m['message_id']: m['labels'] for m in box.control('state')['messages']}
        expect('read' in labels['msg_received_ascii'] and 'unread' not in labels['msg_received_ascii'], 'STORE read labels incorrect')
        ok(client.uid('STORE', str(uid), '-FLAGS.SILENT', '(\\Seen)'))
        ok(client.uid('FETCH', str(uid), '(BODY[])'))
        labels = {m['message_id']: m['labels'] for m in box.control('state')['messages']}
        expect('read' in labels['msg_received_ascii'], 'non-PEEK did not mark read')
        expect(client.uid('STORE', str(uid), '+FLAGS', '(\\Deleted)')[0] == 'NO', 'deletion flag accepted')
        client.logout()
        box.client = None
    # Separate mailbox run proves an idle notification without a NOOP.
    with Sandbox() as box:
        client, count = box.connect()
        box.control('add-message', {})
        client.sock.settimeout(5)
        line = client.readline()
        expect(re.fullmatch(rb'\* 4 EXISTS\r\n', line) is not None, f'periodic arrival notification incorrect: {line!r}')
        expect(len(bodies(client, expected)) == 4, 'periodic arrival missing')
        client.logout()
        box.client = None
    print('Smoke passed: exact raw bytes, pagination, Seen updates, NOOP/periodic arrivals, restart UIDs, owned-process cleanup.')


if __name__ == '__main__':
    try:
        run_smoke()
    except Exception as error:
        print(f'Smoke failed: {type(error).__name__}: {str(error).replace(KEY, "[redacted]")}', file=sys.stderr)
        sys.exit(1)
