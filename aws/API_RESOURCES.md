# API and protocol resources

The official documentation is the source of truth.

## AgentMail

- [API reference](https://docs.agentmail.to/api-reference)
- [Messages guide](https://docs.agentmail.to/messages)
- [Inboxes guide](https://docs.agentmail.to/inboxes)

Production REST base URL: `https://api.agentmail.to/v0`.

Official SDKs are optional: [TypeScript](https://www.npmjs.com/package/agentmail) or [Python](https://pypi.org/project/agentmail/).

## Optional starter template

- [Bundled AgentMail IMAP template](website-template/README.md): optional project scaffolding. Run its setup commands from `aws/website-template/`. Deployment is not required for the work trial.
- Source: [agentmail-to/imap-template](https://github.com/agentmail-to/imap-template), copied from [revision `1eb3417`](https://github.com/agentmail-to/imap-template/commit/1eb3417eeef3100a8aa812f28ee7a7eb91e861f6).

## Local AgentMail sandbox

| Setting       | Value                             |
| ------------- | --------------------------------- |
| REST base URL | `http://127.0.0.1:3210/v0`        |
| Inbox ID      | `candidate@imap.test`             |
| API key       | `test_agentmail_key`              |

The local service follows the relevant public API shapes and accepts bearer authentication. A separate hosted sandbox may be provided during the interview.

## IMAP

- [RFC 3501: IMAP4rev1](https://www.rfc-editor.org/rfc/rfc3501)
