# Local AgentMail API sandbox

This directory contains a deterministic fake AgentMail API with synthetic messages. It requires Node.js 20 or newer and has no third-party dependencies.

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

Private test control: `POST /_test/remove-message` with `{"message_id":"..."}` hard-deletes a synthetic fixture for removal/reappearance tests. This route is used only by isolated tests; the normal IMAP application never calls it.

The localhost-only load runner uses `POST /_test/add-messages` with `{ "count": 1000 }` to add uniquely identified synthetic fixtures. Counts outside 1..10000 are rejected. These private harness controls are not AgentMail public API endpoints and must never be exposed as the hosted service.
