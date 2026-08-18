# AgentMail IMAP work trial

This repository intentionally begins without implementation code.

Read the candidate brief supplied by the interview facilitator, choose the language and tooling you work most effectively in, and document setup, execution, tests, architecture, and tradeoffs here as you build.

Never commit the sandbox API key or raw authorization headers.

## Local validation

The language-neutral [test harness](./test-harness/README.md) provides a deterministic fake AgentMail API and a black-box IMAP acceptance client. It contains no reference IMAP implementation and uses no third-party dependencies.
