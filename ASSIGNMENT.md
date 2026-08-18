# Build an IMAP interface for AgentMail

## Goal

Build a small IMAP4rev1 server over the public AgentMail API. A client should be able to read inbox messages and save a plain-text draft.

You have two working days. Build a reliable vertical slice and explain your tradeoffs. The exercise uses synthetic data and will not be shipped.

## Required outcome

A user must be able to:

1. start your server with one documented command;
2. connect over local TCP and authenticate with an inbox ID and API key;
3. discover and open `INBOX` and `Drafts`;
4. fetch message metadata and complete RFC 822 content;
5. inspect existing AgentMail drafts;
6. append a plain-text draft without losing its recipients, subject, or body;
7. disconnect cleanly.

At minimum, support the parts of `CAPABILITY`, `LOGIN`, `LIST`, `SELECT`, `UID FETCH`, `APPEND`, `NOOP`, and `LOGOUT` needed for that path.

## Correctness expectations

- `INBOX` represents received AgentMail messages. `Drafts` uses the AgentMail Drafts API.
- Reflect read state as `\Seen` and identify drafts with `\Draft`. Decide and document any other mailbox or flag mappings.
- Preserve raw message bytes and use byte lengths for IMAP literals and sizes.
- Do not silently truncate paginated API results.
- Keep UIDs stable across reconnects and process restarts. Adding an item must not renumber or reuse existing UIDs.
- Handle malformed commands, invalid credentials, unsupported behavior, and API failures without crashing, hanging, or exposing secrets.
- TLS, deployment, attachments in new drafts, draft sending, and production compatibility are out of scope.

## Decisions you own

Choose and be prepared to defend:

- language, dependencies, and project structure;
- parser and session-state design;
- mailbox behavior beyond `INBOX` and `Drafts`;
- draft-to-RFC-822 projection;
- persistence, synchronization, and caching strategy;
- test approach and one additional IMAP capability if the core is reliable.

Document the syntax and behavior you support. You may deliberately reject valid IMAP forms outside your chosen slice.

## Rules

- Use only the public AgentMail API or official SDK.
- Do not use a library that implements the IMAP server or protocol core.
- Do not access production systems or internal AgentMail storage.
- Public RFCs, web research, and approved AI tools are allowed.
- Disclose meaningful generated or copied code in your README.
- Never commit credentials or raw authorization headers.

## Deliverables

By the Day 2 code freeze, commit:

- the runnable server and focused automated tests;
- setup, start, and smoke-test commands;
- implemented and unsupported behavior;
- a short architecture and persistence explanation;
- known limitations and next steps.

## Schedule

### Day 1

| Time  | Session                                              |
| ----- | ---------------------------------------------------- |
| 9:00  | Welcome and environment check                        |
| 10:00 | Plan sync: architecture, milestones, risks, and cuts |
| 1:30  | Working-slice checkpoint                             |
| 4:30  | Demo, blockers, and Day 2 plan                       |

### Day 2

| Time | Session                           |
| ---- | --------------------------------- |
| 9:00 | Plan sync and explicit scope cuts |
| 1:30 | Acceptance-readiness checkpoint   |
| 3:30 | Code freeze                       |
| 4:00 | Final presentation and live demo  |
| 4:45 | Candidate questions and wrap-up   |

## Evaluation

We evaluate the working path, protocol correctness, API integration, state design, testing, reliability, security, prioritization, collaboration, and candid technical communication. We do not reward command count or presentation polish over a sound core.

## Start here

1. Review [API and protocol resources](./API_RESOURCES.md).
2. Start the [local AgentMail API sandbox](./test-harness/README.md).
3. Use the [small TCP example](./MANUAL_SMOKE_TEST.md) to begin.
