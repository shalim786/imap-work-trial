# Optional standard IMAP client

Use a desktop client only after your own protocol tests work. For Thunderbird, choose manual configuration:

| Setting             | Value                 |
| ------------------- | --------------------- |
| Protocol            | IMAP                  |
| Hostname            | `127.0.0.1`           |
| Port                | `1143`                |
| Connection security | None                  |
| Authentication      | Normal password       |
| Username            | `candidate@imap.test` |
| Password            | `test_agentmail_key`  |

SMTP is not provided. A full desktop client may request IMAP behavior beyond your chosen scope.
