# Local AgentMail API sandbox

This directory contains a deterministic fake AgentMail API with synthetic messages and drafts. It requires Node.js 20 or newer and has no third-party dependencies.

Start it with:

```bash
npm --prefix test-harness run api
```

Configure your implementation with:

```text
AGENTMAIL_API_URL=http://127.0.0.1:3210/v0
AGENTMAIL_INBOX_ID=candidate@imap.test
AGENTMAIL_API_KEY=test_agentmail_key
```

Check the sandbox itself with:

```bash
npm --prefix test-harness test
```

Use the public AgentMail documentation to decide which endpoints and fields your design needs. Your own protocol tests are part of the submission.
