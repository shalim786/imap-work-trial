# Thunderbird IMAP capture

This capture records Thunderbird 156.0.1 discovering four synthetic messages for `candidate@imap.test`. It demonstrates how Thunderbird learns UIDs and then requests headers. It does not verify the planned AgentMail adapter or authentication.

## Conditions

- Captured September 29, 2026, at approximately 3:39 p.m. PDT.
- An isolated Thunderbird profile was launched with `--headless --new-instance` and IMAP logging enabled.
- A disposable Python listener served the harness fixtures directly on `127.0.0.1:2143`. It made no AgentMail API calls.
- The listener greeted Thunderbird with `PREAUTH`, meaning already authenticated. No Thunderbird `LOGIN` or `AUTHENTICATE` exchange was captured.
- Advertised capabilities were `IMAP4rev1 AUTH=PLAIN`; only `INBOX` existed.
- The temporary Thunderbird instance and both test listeners have been stopped. The existing Thunderbird instance was left running.

## Actual requests

Thunderbird opened two connections. Tags below are the actual client tags, not message identifiers.

Folder discovery on connection 1:

```text
24 capability
25 list "" "*"
26 lsub "" "*"
27 list "" "INBOX"
28 list "" "Trash"
29 create "Trash"
```

The listener rejected `CREATE` with a tagged `NO`. Header retrieval on the other connection continued.

Mailbox discovery on connection 2:

```text
79 capability
80 select "INBOX"
81 UID fetch 1:* (FLAGS)
82 UID fetch 12:22 (UID RFC822.SIZE FLAGS BODY.PEEK[HEADER.FIELDS (From To Cc Bcc Subject Date Message-ID Priority X-Priority References Newsgroups In-Reply-To Content-Type Reply-To Received)])
83 UID fetch 12 (UID BODY.PEEK[HEADER.FIELDS (Content-Type Content-Transfer-Encoding)] BODY.PEEK[TEXT]<0.2048>)
84 noop
85 UID fetch 23:* (FLAGS)
```

## How Thunderbird learned the UIDs

`SELECT` returned `4 EXISTS`, `UIDVALIDITY 20260929`, and `UIDNEXT 23`, together with flags and other mailbox status. It did not return a message mapping.

The response to command 81, also verified in Thunderbird's own log, was:

```text
* 1 FETCH (UID 12 FLAGS ())
* 2 FETCH (UID 15 FLAGS (\Seen \Flagged))
* 3 FETCH (UID 19 FLAGS ())
* 4 FETCH (UID 22 FLAGS ())
81 OK FETCH completed
```

The first number in each response is the sequence number. The number after `UID` is the stable identifier. Thunderbird then requested headers for UID range `12:22`.

The listener deliberately assigned these fixture UIDs:

| Sequence | UID | Fixture message ID |
| --- | --- | --- |
| 1 | 12 | msg_received_ascii |
| 2 | 15 | msg_received_utf8 |
| 3 | 19 | msg_received_attachment |
| 4 | 22 | msg_multi_label |

Command 83 requested a body preview of at most 2048 bytes. No full-message fetch or user opening a message was captured.

## Evidence files

- `requests.log`: all 13 observed client commands, with timestamps and connection numbers.
- `server-transcript.log`: listener transcript. Its FETCH entries summarize responses and omit literal contents; those entries are not exact wire syntax.
- `thunderbird-imap.log`: Thunderbird's own IMAP log, including parsed response lines and synthetic message content.
- `client-wire-extract.log`: sent and received lines extracted from the Thunderbird log. Some late entries were buffered, so use `requests.log` for the complete client command list.

## Limits

This is one observed trace under the stated capabilities and profile settings. Other settings, capabilities, mailbox contents, and user actions can produce different commands. It is not a complete protocol test or a repeatable end-to-end test of the planned server.

Normal GUI account setup stalled. Automatic approval review rejected terminating the existing Thunderbird process because doing so could discard unsaved application state. The isolated profile allowed this capture to proceed separately.

The disposable listener, fixture export, and profile remain under `/private/tmp/imap-thunderbird-capture-rnl91sys` for inspection while temporary files persist. A fresh profile would be needed to repeat initial synchronization without Thunderbird's cached headers.
