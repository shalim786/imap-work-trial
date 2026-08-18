# Manual TCP smoke test

The automated harness is authoritative. This shorter dialogue is useful while building.

## Start services

In terminal 1:

```bash
npm --prefix test-harness run api
```

Start your IMAP server on `127.0.0.1:1143` in terminal 2.

## Run the dialogue

In terminal 3:

```bash
printf '%s\r\n' \
  'A1 CAPABILITY' \
  'A2 LOGIN "candidate@imap.test" "test_agentmail_key"' \
  'A3 LIST "" "*"' \
  'A4 SELECT INBOX' \
  'A5 UID FETCH 1:* (UID FLAGS RFC822.SIZE)' \
  'A6 SELECT Drafts' \
  'A7 UID FETCH 1:* (UID FLAGS RFC822.SIZE)' \
  'A8 NOOP' \
  'A9 LOGOUT' | nc 127.0.0.1 1143
```

Use a UID returned by `A5` for a second run with `UID FETCH <uid> (UID BODY.PEEK[])`.

Check that:

- every command receives the same tag in its terminal response;
- `LIST` includes `INBOX`, `Drafts`, and another mapped mailbox;
- `SELECT` includes `EXISTS`, `UIDVALIDITY`, and `UIDNEXT`;
- `FETCH` returns flags, byte sizes, and a `{byte-count}` raw-message literal;
- `LOGOUT` closes the connection.

The full harness performs the literal-based `APPEND Drafts` check.

Run the full validation when this works:

```bash
npm --prefix test-harness run check
```
