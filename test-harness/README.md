# Local validation harness

This directory contains a deterministic fake AgentMail API and a black-box IMAP acceptance client. It provides a stable development loop without implementing any part of the candidate's IMAP server.

The harness uses only Node.js built-ins and requires Node.js 20 or newer. It has no packages to install.

## What it validates

The default acceptance run checks only the required scope disclosed in the candidate brief:

- greeting, `CAPABILITY`, authentication, `LIST`, `SELECT`, `NOOP`, and `LOGOUT`;
- authentication and selected-mailbox session boundaries;
- `UID FETCH 1:*` metadata plus individual raw-message literals;
- exact raw bytes, UTF-8 byte lengths, sizes, and label-derived flags;
- commands split across TCP writes and multiple commands in one write;
- bounded unsupported-command errors;
- stable UIDs and `UIDVALIDITY` across reconnects;
- monotonic UID assignment when a new message appears.

`UID STORE` is tested only when explicitly requested. Process-restart persistence uses separate record and verify commands so the harness never needs to know how to start the candidate's application.

The checks validate protocol semantics rather than exact cosmetic response text. They are not a substitute for the candidate's own unit and integration tests.

## Quick start

### 1. Start the fake AgentMail API

From the repository root:

```bash
npm --prefix test-harness run api
```

It listens on `127.0.0.1:3210` by default and prints these non-production fixture values:

```text
AGENTMAIL_API_URL=http://127.0.0.1:3210/v0
AGENTMAIL_INBOX_ID=candidate@imap.test
AGENTMAIL_API_KEY=test_agentmail_key
```

Configure the candidate implementation with those values. The API accepts either `Authorization: Bearer test_agentmail_key` or `X-API-Key: test_agentmail_key`.

### 2. Start the candidate's IMAP server

Start it in another terminal. The acceptance client defaults to:

```text
IMAP_HOST=127.0.0.1
IMAP_PORT=1143
```

Override either value in the environment when needed.

### 3. Run the disclosed acceptance path

```bash
npm --prefix test-harness run check
```

To include the target `UID STORE` behavior:

```bash
npm --prefix test-harness run check:store
```

Each run resets the fake API to its baseline, then adds one deterministic message during the UID-stability check. Pass `--no-reset` directly when debugging state:

```bash
npm --prefix test-harness run check -- --no-reset
```

## Validate process-restart UID persistence

Keep the fake API running. With the candidate server running:

```bash
npm --prefix test-harness run uids:record
```

Stop and restart only the candidate server without deleting its UID storage, then run:

```bash
npm --prefix test-harness run uids:verify
```

The snapshot is written under `test-harness/.state/`, which is ignored by Git.

## Fake API behavior

The fake implements the documented surface needed by the exercise:

| Method  | Path                                             | Purpose                                         |
| ------- | ------------------------------------------------ | ----------------------------------------------- |
| `GET`   | `/v0/auth/me`                                    | Validate the inbox-scoped credential            |
| `GET`   | `/v0/inboxes`                                    | List the fixture inbox                          |
| `GET`   | `/v0/inboxes/:inbox_id`                          | Get the fixture inbox                           |
| `GET`   | `/v0/inboxes/:inbox_id/messages`                 | List and filter messages with opaque pagination |
| `GET`   | `/v0/inboxes/:inbox_id/messages/:message_id`     | Get message metadata and text                   |
| `GET`   | `/v0/inboxes/:inbox_id/messages/:message_id/raw` | Obtain a temporary raw-message URL              |
| `PATCH` | `/v0/inboxes/:inbox_id/messages/:message_id`     | Add and remove labels                           |
| `GET`   | `/raw/:message_id`                               | Download exact `message/rfc822` bytes           |

List responses deliberately contain at most two messages even when a larger `limit` is requested. This is valid short-page behavior and ensures clients follow `next_page_token` rather than silently truncating the mailbox.

The fixtures include unread, read, starred, sent, trash, multi-label, UTF-8, and multipart messages. Every raw message has CRLF line endings and a unique visible `Message-ID`.

## Test controls

Control endpoints bind to the same local-only HTTP server and do not require the fixture credential:

| Method | Path                 | Purpose                                                         |
| ------ | -------------------- | --------------------------------------------------------------- |
| `GET`  | `/_test/state`       | Inspect message metadata, labels, sizes, and raw SHA-256 hashes |
| `GET`  | `/_test/requests`    | Inspect the last 100 redacted request records                   |
| `POST` | `/_test/reset`       | Restore the baseline messages and labels                        |
| `POST` | `/_test/add-message` | Add the deterministic new-arrival fixture                       |
| `POST` | `/_test/fail-next`   | Delay or fail the next matching API request                     |

For example, make the next raw-metadata request return `503`:

```bash
curl -sS -X POST http://127.0.0.1:3210/_test/fail-next \
  -H 'content-type: application/json' \
  -d '{"method":"GET","path_prefix":"/inboxes/candidate@imap.test/messages/msg_received_ascii/raw","status":503}'
```

Or delay the next inbox lookup by two seconds:

```bash
curl -sS -X POST http://127.0.0.1:3210/_test/fail-next \
  -H 'content-type: application/json' \
  -d '{"method":"GET","path_prefix":"/inboxes/candidate@imap.test","delay_ms":2000,"status":504}'
```

These controls support manual failure testing without adding undisclosed acceptance requirements.

## Harness self-tests

```bash
npm --prefix test-harness test
```

The self-tests validate the fake API, pagination, raw-byte flow, label updates, failure injection, and the acceptance client's response parsing. They do not contain a reference IMAP implementation.
