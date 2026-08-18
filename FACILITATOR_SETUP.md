# Facilitator setup

Complete this before the candidate arrives.

## Environment

- Install Node.js 20 or newer, Git, `nc`, curl, and the candidate's chosen language/editor.
- Optionally install Thunderbird.
- Confirm access to AgentMail docs, package registries, RFC Editor, and approved research/AI tools.
- Run `npm --prefix test-harness test`.
- Run the acceptance path against a known-good disposable implementation.

## Hosted sandbox

Provide a synthetic inbox and inbox-scoped key out of band. Never commit them.

The inbox should include:

- unread and read received messages;
- UTF-8 and multipart content;
- sent and trash messages;
- one message with multiple mailbox labels.

Verify list pagination, metadata retrieval, raw download, and label updates. Revoke the key after the interview.

## Candidate handoff

- Give the candidate the repository, hosted sandbox values, schedule, and internet access.
- Explain that the checked-in key is local-only and the hosted key belongs in an ignored `.env` file.
- Keep lunch and breaks non-evaluative.
- Do not expect or accept overnight work.
- Record environment failures so they do not affect scoring.

The confidential rubric remains outside this repository.
