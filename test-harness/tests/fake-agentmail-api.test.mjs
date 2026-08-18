import assert from "node:assert/strict";
import test from "node:test";

import { createFakeAgentMailServer } from "../fake-agentmail-api.mjs";
import {
  API_KEY,
  BASE_DRAFTS,
  BASE_MESSAGES,
  INBOX_ID,
} from "../lib/fixtures.mjs";

async function jsonRequest(
  url,
  { method = "GET", body, authenticated = true } = {},
) {
  const response = await fetch(url, {
    method,
    headers: {
      ...(authenticated ? { authorization: `Bearer ${API_KEY}` } : {}),
      ...(body ? { "content-type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const value = await response.json();
  return { response, value };
}

async function withServer(t) {
  const fake = createFakeAgentMailServer({
    host: "127.0.0.1",
    port: 0,
    quiet: true,
  });
  const address = await fake.start();
  t.after(() => fake.stop());
  return { ...address, controlRoot: address.baseUrl.replace(/\/v0$/, "") };
}

test("requires the deterministic test credential", async (t) => {
  const { baseUrl } = await withServer(t);
  const denied = await jsonRequest(`${baseUrl}/auth/me`, {
    authenticated: false,
  });
  assert.equal(denied.response.status, 401);

  const allowed = await jsonRequest(`${baseUrl}/auth/me`);
  assert.equal(allowed.response.status, 200);
  assert.equal(allowed.value.inbox_id, INBOX_ID);
});

test("returns short paginated label-filtered pages", async (t) => {
  const { baseUrl } = await withServer(t);
  const first = await jsonRequest(
    `${baseUrl}/inboxes/${encodeURIComponent(INBOX_ID)}/messages?labels=received&limit=100`,
  );
  assert.equal(first.response.status, 200);
  assert.equal(first.value.messages.length, 2);
  assert.ok(first.value.next_page_token);
  assert.ok(
    first.value.messages.every((message) =>
      message.labels.includes("received"),
    ),
  );

  const second = await jsonRequest(
    `${baseUrl}/inboxes/${encodeURIComponent(INBOX_ID)}/messages?labels=received&limit=100&page_token=${encodeURIComponent(first.value.next_page_token)}`,
  );
  assert.equal(second.response.status, 200);
  assert.equal(second.value.messages.length, 2);
  assert.equal(second.value.next_page_token, undefined);
});

test("serves exact raw bytes through the documented two-step flow", async (t) => {
  const { baseUrl } = await withServer(t);
  const fixture = BASE_MESSAGES[1];
  const metadata = await jsonRequest(
    `${baseUrl}/inboxes/${encodeURIComponent(INBOX_ID)}/messages/${fixture.id}/raw`,
  );
  assert.equal(metadata.response.status, 200);
  assert.equal(metadata.value.size, fixture.raw.length);

  const rawResponse = await fetch(metadata.value.download_url);
  const raw = Buffer.from(await rawResponse.arrayBuffer());
  assert.equal(rawResponse.status, 200);
  assert.ok(raw.equals(fixture.raw));
});

test("lists paginated drafts and returns full draft bodies", async (t) => {
  const { baseUrl } = await withServer(t);
  const first = await jsonRequest(
    `${baseUrl}/inboxes/${encodeURIComponent(INBOX_ID)}/drafts?limit=100`,
  );
  assert.equal(first.response.status, 200);
  assert.equal(first.value.drafts.length, 1);
  assert.ok(first.value.next_page_token);
  assert.equal("text" in first.value.drafts[0], false);

  const second = await jsonRequest(
    `${baseUrl}/inboxes/${encodeURIComponent(INBOX_ID)}/drafts?limit=100&page_token=${encodeURIComponent(first.value.next_page_token)}`,
  );
  assert.equal(second.response.status, 200);
  assert.equal(second.value.drafts.length, 1);
  assert.equal(second.value.next_page_token, undefined);

  const fixture = BASE_DRAFTS[0];
  const get = await jsonRequest(
    `${baseUrl}/inboxes/${encodeURIComponent(INBOX_ID)}/drafts/${fixture.id}`,
  );
  assert.equal(get.response.status, 200);
  assert.equal(get.value.text, fixture.text);
});

test("creates a plain-text draft and exposes it in test state", async (t) => {
  const { baseUrl, controlRoot } = await withServer(t);
  const created = await jsonRequest(
    `${baseUrl}/inboxes/${encodeURIComponent(INBOX_ID)}/drafts`,
    {
      method: "POST",
      body: {
        to: ["append@example.com"],
        cc: ["copy@example.com"],
        subject: "Created in test",
        text: "Draft body",
      },
    },
  );
  assert.equal(created.response.status, 200);
  assert.match(created.value.draft_id, /^draft_created_/);
  assert.equal(created.value.text, "Draft body");

  const state = await jsonRequest(`${controlRoot}/_test/state`, {
    authenticated: false,
  });
  const draft = state.value.drafts.find(
    (item) => item.draft_id === created.value.draft_id,
  );
  assert.deepEqual(draft.to, ["append@example.com"]);
  assert.equal(draft.subject, "Created in test");
});

test("updates labels and exposes state without exposing raw bodies", async (t) => {
  const { baseUrl, controlRoot } = await withServer(t);
  const fixture = BASE_MESSAGES.find(
    (message) => message.id === "msg_received_ascii",
  );
  const update = await jsonRequest(
    `${baseUrl}/inboxes/${encodeURIComponent(INBOX_ID)}/messages/${fixture.id}`,
    {
      method: "PATCH",
      body: { add_labels: ["read"], remove_labels: ["unread"] },
    },
  );
  assert.equal(update.response.status, 200);
  assert.ok(update.value.labels.includes("read"));
  assert.ok(!update.value.labels.includes("unread"));

  const state = await jsonRequest(`${controlRoot}/_test/state`, {
    authenticated: false,
  });
  const message = state.value.messages.find(
    (item) => item.message_id === fixture.id,
  );
  assert.ok(message.labels.includes("read"));
  assert.equal("raw" in message, false);
});

test("injects one bounded failure for a matching API path", async (t) => {
  const { baseUrl, controlRoot } = await withServer(t);
  const injection = await jsonRequest(`${controlRoot}/_test/fail-next`, {
    authenticated: false,
    method: "POST",
    body: { method: "GET", path_prefix: "/auth/me", status: 503 },
  });
  assert.equal(injection.response.status, 201);

  const failed = await jsonRequest(`${baseUrl}/auth/me`);
  assert.equal(failed.response.status, 503);
  const recovered = await jsonRequest(`${baseUrl}/auth/me`);
  assert.equal(recovered.response.status, 200);
});
