# AgentMail API resources

Checked against the public API on 2026-08-18. The official documentation is the source of truth.

## Official links

- [API reference](https://docs.agentmail.to/api-reference)
- [Who Am I](https://docs.agentmail.to/api-reference/auth/me)
- [List Messages](https://docs.agentmail.to/api-reference/inboxes/messages/list)
- [Get Raw Message](https://docs.agentmail.to/api-reference/inboxes/messages/get-raw)
- [Update Message](https://docs.agentmail.to/api-reference/inboxes/messages/update)
- [List Drafts](https://docs.agentmail.to/api-reference/inboxes/drafts/list)
- [Get Draft](https://docs.agentmail.to/api-reference/inboxes/drafts/get)
- [Create Draft](https://docs.agentmail.to/api-reference/inboxes/drafts/create)
- [Drafts guide](https://docs.agentmail.to/drafts)
- [Messages guide](https://docs.agentmail.to/messages)
- [Inboxes guide](https://docs.agentmail.to/inboxes)

Production REST base URL: `https://api.agentmail.to/v0`.

## Local harness

| Value                                  | Local setting              |
| -------------------------------------- | -------------------------- |
| REST base URL                          | `http://127.0.0.1:3210/v0` |
| SDK origin, when the SDK appends `/v0` | `http://127.0.0.1:3210`    |
| Inbox ID                               | `candidate@imap.test`      |
| API key                                | `test_agentmail_key`       |

Authenticate REST requests with `Authorization: Bearer <api-key>`. The local fake also accepts `X-API-Key`.

## Minimal API contract

| Method  | Path                                          | Needed behavior                            |
| ------- | --------------------------------------------- | ------------------------------------------ |
| `GET`   | `/auth/me`                                    | Validate credential scope and inbox access |
| `GET`   | `/inboxes/:inbox_id`                          | Validate access to one inbox               |
| `GET`   | `/inboxes/:inbox_id/messages`                 | List messages; follow `next_page_token`    |
| `GET`   | `/inboxes/:inbox_id/messages/:message_id`     | Read metadata and labels                   |
| `GET`   | `/inboxes/:inbox_id/messages/:message_id/raw` | Receive `download_url`, `size`, and expiry |
| `PATCH` | `/inboxes/:inbox_id/messages/:message_id`     | Add or remove labels for optional `STORE`  |
| `GET`   | `/inboxes/:inbox_id/drafts`                   | List drafts; follow `next_page_token`      |
| `GET`   | `/inboxes/:inbox_id/drafts/:draft_id`         | Read the full draft body                   |
| `POST`  | `/inboxes/:inbox_id/drafts`                   | Create a draft from `APPEND`               |

Message-list items include `message_id`, `labels`, `timestamp`, addressing fields, `subject`, `headers`, and `size`. The raw endpoint returns a URL; download that URL as bytes without converting it through a text string.

Message updates use:

```json
{
  "add_labels": ["read"],
  "remove_labels": ["unread"]
}
```

Draft list items omit the full body. Fetch each draft, then use its structured address fields, `subject`, and `text` to build RFC 822 bytes. Draft creation accepts those same fields; this exercise does not send drafts.

## Official SDKs

Using an SDK is optional.

| Language   | Verified version | Install                        | Package                                        |
| ---------- | ---------------: | ------------------------------ | ---------------------------------------------- |
| TypeScript |         `0.5.19` | `npm install agentmail@0.5.19` | [npm](https://www.npmjs.com/package/agentmail) |
| Python     |          `0.5.8` | `pip install agentmail==0.5.8` | [PyPI](https://pypi.org/project/agentmail/)    |

TypeScript client: `AgentMailClient` from `agentmail`.

Python client: `AgentMail` from `agentmail`.

## Protocol reference

- [RFC 3501: IMAP4rev1](https://www.rfc-editor.org/rfc/rfc3501)
- [RFC 2177: IMAP IDLE](https://www.rfc-editor.org/rfc/rfc2177), optional
