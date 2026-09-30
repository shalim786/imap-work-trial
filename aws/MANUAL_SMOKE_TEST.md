# Small TCP example

Start the local API sandbox and your IMAP server. Then connect:

```bash
nc 127.0.0.1 1143
```

An IMAP exchange uses client-chosen tags and CRLF-terminated commands. For example:

```text
A1 CAPABILITY
A2 LOGIN "candidate@imap.test" "test_agentmail_key"
```

Continue with the commands and forms your implementation supports. Include a repeatable end-to-end smoke test in your final README.
