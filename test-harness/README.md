# Local validation harness

This directory contains a fake AgentMail API and a black-box IMAP acceptance client. It uses Node.js 20 or newer, has no third-party dependencies, and contains no reference IMAP server.

## Run it

Start the fake API:

```bash
npm --prefix test-harness run api
```

Configure your server with:

```text
AGENTMAIL_API_URL=http://127.0.0.1:3210/v0
AGENTMAIL_INBOX_ID=candidate@imap.test
AGENTMAIL_API_KEY=test_agentmail_key
```

Start your IMAP server on `127.0.0.1:1143`, then run:

```bash
npm --prefix test-harness run check
```

Override `IMAP_HOST`, `IMAP_PORT`, or `HARNESS_CONTROL_URL` when needed.

## Additional checks

Optional `UID STORE`:

```bash
npm --prefix test-harness run check:store
```

Process-restart UID stability:

```bash
npm --prefix test-harness run uids:record
# restart only your IMAP server without deleting its local state
npm --prefix test-harness run uids:verify
```

Harness self-tests:

```bash
npm --prefix test-harness test
```

## What the default check covers

- greeting, authentication, `LIST`, `SELECT`, `UID FETCH`, `APPEND Drafts`, `NOOP`, and `LOGOUT`;
- session-state errors and unsupported commands;
- split and coalesced TCP commands;
- pagination, flags, raw bytes, UTF-8 byte lengths, and message sizes;
- paginated draft projection, `\Draft`, and plain-text draft creation;
- UID stability across reconnects, new-message arrival, and draft creation.

The harness resets its fake API before each run. It deliberately returns short message and draft pages to catch missing pagination.

## Failure controls

The fake API also exposes local-only controls:

| Method | Path                 | Purpose                                 |
| ------ | -------------------- | --------------------------------------- |
| `GET`  | `/_test/state`       | Inspect fixture metadata and labels     |
| `GET`  | `/_test/requests`    | Inspect redacted request records        |
| `POST` | `/_test/reset`       | Restore baseline fixtures               |
| `POST` | `/_test/add-message` | Add a deterministic new message         |
| `POST` | `/_test/fail-next`   | Delay or fail the next matching request |

Example:

```bash
curl -sS -X POST http://127.0.0.1:3210/_test/fail-next \
  -H 'content-type: application/json' \
  -d '{"method":"GET","path_prefix":"/inboxes/","status":503}'
```

The acceptance checks validate disclosed behavior, but you should still write focused tests for your own design.
