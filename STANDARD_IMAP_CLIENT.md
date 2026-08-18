# Optional standard IMAP clients

Use these only after the raw TCP and automated checks work.

## curl

Many curl builds support IMAP:

```bash
curl --url imap://127.0.0.1:1143/INBOX \
  --user "candidate@imap.test:test_agentmail_key" \
  --request 'UID FETCH 1:* (UID FLAGS RFC822.SIZE)'
```

## Thunderbird

Choose manual configuration and use:

| Setting             | Value                 |
| ------------------- | --------------------- |
| Protocol            | IMAP                  |
| Hostname            | `127.0.0.1`           |
| Port                | `1143`                |
| Connection security | None                  |
| Authentication      | Normal password       |
| Username            | `candidate@imap.test` |
| Password            | `test_agentmail_key`  |

The exercise does not provide SMTP, so sending mail is expected to fail. Incoming mailbox behavior is the only relevant path.

See Mozilla's [manual account configuration](https://support.mozilla.org/en-US/kb/manual-account-configuration) guide if the setup screen differs.
