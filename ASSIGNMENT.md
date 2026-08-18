# Build an IMAP interface for AgentMail

## Goal

Build a small IMAP4rev1 server that lets an IMAP client authenticate, open an AgentMail inbox, and fetch real messages through the AgentMail API.

You have two working days. We value a reliable vertical slice over broad command coverage.

This is a time-bounded evaluation using synthetic data. The submitted implementation will not be shipped.

## Provided

- This empty implementation repository and local validation harness.
- [AgentMail API documentation and official SDK links](./API_RESOURCES.md).
- A local synthetic inbox and non-production test key.
- A hosted synthetic inbox and inbox-scoped key from the facilitator.
- A [raw TCP example](./MANUAL_SMOKE_TEST.md) and optional [standard-client setup](./STANDARD_IMAP_CLIENT.md).
- Internet access for public RFCs and general technical research.

## Required user path

1. Start the server with one documented command.
2. Connect over TCP and receive an IMAP greeting.
3. Authenticate with an inbox ID and API key.
4. List and select `INBOX`.
5. Fetch metadata and raw RFC 822 messages.
6. Disconnect cleanly.

## Mailboxes and flags

AgentMail labels map to fixed IMAP mailboxes:

| IMAP mailbox | AgentMail label |
| ------------ | --------------- |
| `INBOX`      | `received`      |
| `Sent`       | `sent`          |
| `Trash`      | `trash`         |
| `Spam`       | `spam`          |

A message may appear in multiple mailboxes. Each mailbox has its own UID space.

| IMAP flag  | AgentMail label |
| ---------- | --------------- |
| `\Seen`    | `read`          |
| `\Flagged` | `starred`       |
| `\Deleted` | `trash`         |

## Required scope

Implement:

- `CAPABILITY`
- `LOGIN`
- `LIST`
- `SELECT`
- `UID FETCH`
- `NOOP`
- `LOGOUT`

For `UID FETCH`, support `1:*` and:

- `UID`
- `FLAGS`
- `RFC822.SIZE`
- `BODY.PEEK[]` or `RFC822`

Your server must also:

- listen on a configurable local TCP port;
- use CRLF framing and buffer split or coalesced TCP commands;
- reject malformed, unsupported, or out-of-state commands cleanly;
- validate that the API key can access the requested inbox;
- follow message-list pagination;
- return exact raw bytes with byte-based literal lengths;
- keep per-mailbox UIDs stable across reconnects and process restarts;
- avoid renumbering or reusing UIDs when messages appear.

Local SQLite or another lightweight persistent store is sufficient. TLS and deployment are out of scope.

## Target after the core works

Implement `UID STORE` for `+FLAGS`, `-FLAGS`, and `.SILENT` forms of `\Seen`. Translate changes to AgentMail `read` and `unread` labels.

Optional extensions include richer sequence sets, `FETCH`, `STATUS`, `EXAMINE`, `SEARCH`, `MOVE`, or `IDLE`. Extensions do not compensate for a broken core path.

## Rules

- Use any language you can explain and debug.
- HTTP, database, and test libraries are allowed.
- Do not use a library that implements the IMAP server or protocol core.
- Use only the public AgentMail API or official SDK.
- Do not access production systems or internal AgentMail storage.
- Public RFCs, web research, and approved AI tools are allowed.
- Disclose meaningful generated or copied code in your README.
- Never commit credentials or raw authorization headers.

## Deliverables

By the Day 2 code freeze, commit:

- the runnable server;
- automated tests for the highest-risk behavior;
- setup, start, and smoke-test commands;
- implemented and unsupported commands;
- a short architecture and UID-persistence explanation;
- known limitations and next steps.

## Schedule

### Day 1

| Time  | Session                                              |
| ----- | ---------------------------------------------------- |
| 9:00  | Welcome and environment check                        |
| 10:00 | Plan sync: architecture, milestones, risks, and cuts |
| 1:30  | Working-slice checkpoint                             |
| 4:30  | Demo, blockers, and Day 2 plan                       |
| 5:00  | Stop work                                            |

### Day 2

| Time | Session                           |
| ---- | --------------------------------- |
| 9:00 | Plan sync and explicit scope cuts |
| 1:30 | Acceptance-readiness checkpoint   |
| 3:30 | Code freeze                       |
| 4:00 | Final presentation and live demo  |
| 4:45 | Candidate questions and wrap-up   |

Lunch is 12:30 to 1:30 and is not evaluated. Do not work overnight; overnight changes are not considered.

## Evaluation

We evaluate the working user path, protocol and byte correctness, API integration, UID design, testing, reliability, security, prioritization, collaboration, and candid technical communication. We do not evaluate typing speed, memorized IMAP trivia, presentation polish, or use of approved tools.

## Start here

1. Read [API resources](./API_RESOURCES.md).
2. Start the [local test harness](./test-harness/README.md).
3. Use the [manual TCP smoke test](./MANUAL_SMOKE_TEST.md) while building.
4. Optionally try a [standard IMAP client](./STANDARD_IMAP_CLIENT.md).
