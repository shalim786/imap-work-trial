import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  ADDED_MESSAGE,
  API_KEY,
  BASE_MESSAGES,
  INBOX_ID,
} from "./lib/fixtures.mjs";
import {
  ImapTestClient,
  extractFirstLiteral,
  parseFetchMetadata,
  parseSelectResponse,
  quoteImap,
} from "./lib/imap-client.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_SNAPSHOT = path.join(HERE, ".state", "uid-snapshot.json");

function parseOptions(argv) {
  const flags = new Set(argv);
  if (flags.has("--help")) {
    process.stdout.write(`Usage: node imap-acceptance.mjs [options]\n\n`);
    process.stdout.write(
      `  --store        also validate the optional UID STORE \\Seen target\n`,
    );
    process.stdout.write(
      `  --record-uids  save a UID snapshot for a later process-restart check\n`,
    );
    process.stdout.write(
      `  --verify-uids  compare UIDs with the previously recorded snapshot\n`,
    );
    process.stdout.write(
      `  --no-reset     preserve current fake-API state before the run\n`,
    );
    process.exit(0);
  }
  return {
    store: flags.has("--store"),
    recordUids: flags.has("--record-uids"),
    verifyUids: flags.has("--verify-uids"),
    reset: !flags.has("--no-reset"),
  };
}

function redact(value) {
  return String(value).split(API_KEY).join("<redacted>");
}

function transcript(result) {
  return redact(
    result?.buffer?.toString("utf8") || result?.text || "<no response>",
  ).slice(0, 4_000);
}

function requireStatus(result, expected, label) {
  assert.equal(
    result.status,
    expected,
    `${label}: expected ${expected}, received ${result.status}\n${transcript(result)}`,
  );
}

function requirePositiveInteger(value, label) {
  assert.ok(
    Number.isSafeInteger(value) && value > 0,
    `${label} must be a positive integer`,
  );
}

async function controlRequest(
  controlRoot,
  route,
  { method = "GET", body } = {},
) {
  const response = await fetch(`${controlRoot}${route}`, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await response.text();
  let value;
  try {
    value = text ? JSON.parse(text) : {};
  } catch {
    value = { text };
  }
  if (!response.ok) {
    throw new Error(
      `fake API ${method} ${route} returned ${response.status}: ${redact(text)}`,
    );
  }
  return value;
}

function fixtureForRaw(raw) {
  return [...BASE_MESSAGES, ADDED_MESSAGE].find((item) => item.raw.equals(raw));
}

function assertFlags(record, fixture) {
  const lower = new Set(record.flags.map((flag) => flag.toLowerCase()));
  assert.equal(
    lower.has("\\seen"),
    fixture.labels.includes("read"),
    `${fixture.id}: \\Seen does not match AgentMail labels`,
  );
  assert.equal(
    lower.has("\\flagged"),
    fixture.labels.includes("starred"),
    `${fixture.id}: \\Flagged does not match AgentMail labels`,
  );
  assert.equal(
    lower.has("\\deleted"),
    fixture.labels.includes("trash"),
    `${fixture.id}: \\Deleted does not match AgentMail labels`,
  );
}

async function connect(host, port) {
  const client = new ImapTestClient({ host, port });
  const greeting = await client.connect();
  const greetingText = greeting.toString("utf8");
  assert.match(
    greetingText,
    /^\* +(OK|PREAUTH)\b/i,
    `invalid IMAP greeting: ${greetingText}`,
  );
  return client;
}

async function loginAndSelect(host, port, expectedCount) {
  const client = await connect(host, port);
  const login = await client.command(
    `LOGIN ${quoteImap(INBOX_ID)} ${quoteImap(API_KEY)}`,
  );
  requireStatus(login, "OK", "LOGIN");
  const select = await client.command("SELECT INBOX");
  requireStatus(select, "OK", "SELECT INBOX");
  const selected = parseSelectResponse(select.text);
  assert.equal(
    selected.exists,
    expectedCount,
    "SELECT EXISTS does not match received fixtures",
  );
  requirePositiveInteger(selected.uidValidity, "UIDVALIDITY");
  requirePositiveInteger(selected.uidNext, "UIDNEXT");
  return { client, selected };
}

async function fetchSnapshot(client, expectedFixtures) {
  const metadataResponse = await client.command(
    "UID FETCH 1:* (UID FLAGS RFC822.SIZE)",
  );
  requireStatus(metadataResponse, "OK", "UID FETCH metadata");
  const metadata = parseFetchMetadata(metadataResponse.text);
  assert.equal(
    metadata.length,
    expectedFixtures.length,
    `expected ${expectedFixtures.length} FETCH records, received ${metadata.length}\n${transcript(metadataResponse)}`,
  );
  assert.equal(
    new Set(metadata.map((item) => item.uid)).size,
    metadata.length,
    "UIDs are not unique",
  );
  assert.equal(
    new Set(metadata.map((item) => item.sequence)).size,
    metadata.length,
    "sequence numbers are not unique",
  );
  for (const item of metadata) {
    requirePositiveInteger(item.uid, "message UID");
    requirePositiveInteger(item.sequence, "message sequence number");
    requirePositiveInteger(item.size, "RFC822.SIZE");
  }

  const byMessageId = new Map();
  const seenFixtureIds = new Set();
  for (const item of metadata) {
    const bodyResponse = await client.command(
      `UID FETCH ${item.uid} (UID BODY.PEEK[])`,
    );
    requireStatus(bodyResponse, "OK", `UID FETCH ${item.uid} body`);
    const literal = extractFirstLiteral(bodyResponse.buffer);
    assert.ok(
      literal,
      `UID ${item.uid}: response did not contain an IMAP literal\n${transcript(bodyResponse)}`,
    );
    assert.equal(
      literal.declaredLength,
      literal.bytes.length,
      `UID ${item.uid}: literal length mismatch`,
    );
    const fixture = fixtureForRaw(literal.bytes);
    assert.ok(
      fixture,
      `UID ${item.uid}: raw bytes do not exactly match any disclosed fixture`,
    );
    assert.ok(
      expectedFixtures.some((item) => item.id === fixture.id),
      `UID ${item.uid}: ${fixture.id} does not belong in selected INBOX`,
    );
    assert.ok(
      !seenFixtureIds.has(fixture.id),
      `${fixture.id} was returned more than once`,
    );
    seenFixtureIds.add(fixture.id);
    assert.equal(
      item.size,
      fixture.raw.length,
      `${fixture.id}: RFC822.SIZE is not the raw byte length`,
    );
    assertFlags(item, fixture);
    byMessageId.set(fixture.id, item.uid);
  }

  assert.deepEqual(
    [...seenFixtureIds].sort(),
    expectedFixtures.map((item) => item.id).sort(),
    "fetched fixture set does not match selected mailbox",
  );
  return { metadata, byMessageId };
}

async function logout(client) {
  try {
    const result = await client.command("LOGOUT");
    requireStatus(result, "OK", "LOGOUT");
  } finally {
    client.destroy();
  }
}

function compareUidMaps(before, after, label) {
  for (const [messageId, uid] of before.entries()) {
    assert.equal(
      after.get(messageId),
      uid,
      `${label}: ${messageId} changed from UID ${uid}`,
    );
  }
}

async function run() {
  const options = parseOptions(process.argv.slice(2));
  const imapHost = process.env.IMAP_HOST || "127.0.0.1";
  const imapPort = Number(process.env.IMAP_PORT || 1143);
  const controlRoot = (
    process.env.HARNESS_CONTROL_URL || "http://127.0.0.1:3210"
  ).replace(/\/$/, "");
  let passed = 0;
  const check = async (name, action) => {
    process.stdout.write(`- ${name} ... `);
    await action();
    passed += 1;
    process.stdout.write("ok\n");
  };

  process.stdout.write(`IMAP acceptance target: ${imapHost}:${imapPort}\n`);
  process.stdout.write(`Fake API controls: ${controlRoot}\n\n`);

  await check("fake AgentMail API is reachable", async () => {
    await controlRequest(controlRoot, "/health");
    if (options.reset)
      await controlRequest(controlRoot, "/_test/reset", { method: "POST" });
  });

  const initialState = await controlRequest(controlRoot, "/_test/state");
  const initialFixtures = BASE_MESSAGES.filter((item) =>
    item.labels.includes("received"),
  );
  assert.equal(
    initialState.messages.filter((item) => item.labels.includes("received"))
      .length,
    initialFixtures.length,
    "fake API fixture state is not at its baseline; rerun without --no-reset",
  );

  await check("pre-authentication mailbox access is rejected", async () => {
    const client = await connect(imapHost, imapPort);
    try {
      const response = await client.command("SELECT INBOX");
      assert.notEqual(
        response.status,
        "OK",
        `SELECT before LOGIN unexpectedly succeeded\n${transcript(response)}`,
      );
      assert.doesNotMatch(
        response.text,
        /^\* +\d+ +EXISTS/im,
        "pre-auth response exposed mailbox data",
      );
    } finally {
      client.destroy();
    }
  });

  await check("invalid credentials are rejected", async () => {
    const client = await connect(imapHost, imapPort);
    try {
      const response = await client.command(
        `LOGIN ${quoteImap(INBOX_ID)} "wrong_test_key"`,
      );
      assert.notEqual(
        response.status,
        "OK",
        `invalid LOGIN unexpectedly succeeded\n${transcript(response)}`,
      );
    } finally {
      client.destroy();
    }
  });

  let firstSelected;
  let firstSnapshot;
  let mainClient;
  await check("CAPABILITY, LOGIN, LIST, and SELECT complete", async () => {
    mainClient = await connect(imapHost, imapPort);
    const capability = await mainClient.command("CAPABILITY");
    requireStatus(capability, "OK", "CAPABILITY");
    assert.match(
      capability.text,
      /\bIMAP4rev1\b/i,
      "CAPABILITY did not advertise IMAP4rev1",
    );

    const login = await mainClient.command(
      `LOGIN ${quoteImap(INBOX_ID)} ${quoteImap(API_KEY)}`,
    );
    requireStatus(login, "OK", "LOGIN");

    const list = await mainClient.command('LIST "" "*"');
    requireStatus(list, "OK", "LIST");
    assert.match(list.text, /\bINBOX\b/i, "LIST did not expose INBOX");
    assert.match(
      list.text,
      /(?:\bSent\b|\bTrash\b|\bSpam\b)/i,
      "LIST did not expose an additional label-backed mailbox",
    );

    const select = await mainClient.command("SELECT INBOX");
    requireStatus(select, "OK", "SELECT INBOX");
    firstSelected = parseSelectResponse(select.text);
    assert.equal(
      firstSelected.exists,
      initialFixtures.length,
      "SELECT EXISTS is incorrect",
    );
    requirePositiveInteger(firstSelected.uidValidity, "UIDVALIDITY");
    requirePositiveInteger(firstSelected.uidNext, "UIDNEXT");
  });

  await check(
    "UID FETCH returns exact raw bytes, sizes, and flags",
    async () => {
      firstSnapshot = await fetchSnapshot(mainClient, initialFixtures);
      const maxUid = Math.max(
        ...firstSnapshot.metadata.map((item) => item.uid),
      );
      assert.ok(
        firstSelected.uidNext > maxUid,
        "UIDNEXT must be greater than every allocated UID",
      );
    },
  );

  await check("split and coalesced TCP commands are buffered", async () => {
    const splitTag = mainClient.nextTag();
    const splitWait = mainClient.waitForTagged(splitTag);
    await mainClient.writeChunks([`${splitTag} NO`, "OP\r", "\n"]);
    requireStatus(await splitWait, "OK", "split NOOP");

    const firstTag = mainClient.nextTag();
    const secondTag = mainClient.nextTag();
    mainClient.write(`${firstTag} NOOP\r\n${secondTag} NOOP\r\n`);
    requireStatus(
      await mainClient.waitForTagged(firstTag),
      "OK",
      "first coalesced NOOP",
    );
    requireStatus(
      await mainClient.waitForTagged(secondTag),
      "OK",
      "second coalesced NOOP",
    );
  });

  await check(
    "malformed or unsupported commands receive a bounded error",
    async () => {
      const response = await mainClient.command("XYZZY");
      assert.ok(
        response.status === "BAD" || response.status === "NO",
        `unsupported command should return BAD or NO\n${transcript(response)}`,
      );
      const noop = await mainClient.command("NOOP");
      requireStatus(noop, "OK", "NOOP after unsupported command");
    },
  );

  if (options.store) {
    await check(
      "optional UID STORE maps \\Seen to AgentMail labels",
      async () => {
        const messageId = "msg_received_ascii";
        const uid = firstSnapshot.byMessageId.get(messageId);
        requirePositiveInteger(uid, `${messageId} UID`);

        const add = await mainClient.command(
          `UID STORE ${uid} +FLAGS.SILENT (\\Seen)`,
        );
        requireStatus(add, "OK", "UID STORE +FLAGS.SILENT");
        assert.doesNotMatch(
          add.text,
          /^\* +\d+ +FETCH/im,
          ".SILENT emitted an unsolicited FETCH",
        );
        const afterAdd = await mainClient.command(
          `UID FETCH ${uid} (UID FLAGS)`,
        );
        requireStatus(afterAdd, "OK", "UID FETCH after adding \\Seen");
        const addedMetadata = parseFetchMetadata(afterAdd.text);
        assert.equal(
          addedMetadata.length,
          1,
          "expected one FETCH record after STORE",
        );
        assert.ok(
          addedMetadata[0].flags.some(
            (flag) => flag.toLowerCase() === "\\seen",
          ),
          "\\Seen was not visible after STORE",
        );
        const addedState = await controlRequest(controlRoot, "/_test/state");
        const addedMessage = addedState.messages.find(
          (item) => item.message_id === messageId,
        );
        assert.ok(
          addedMessage.labels.includes("read"),
          "STORE did not add AgentMail read label",
        );
        assert.ok(
          !addedMessage.labels.includes("unread"),
          "STORE did not remove AgentMail unread label",
        );

        const remove = await mainClient.command(
          `UID STORE ${uid} -FLAGS.SILENT (\\Seen)`,
        );
        requireStatus(remove, "OK", "UID STORE -FLAGS.SILENT");
        const afterRemove = await mainClient.command(
          `UID FETCH ${uid} (UID FLAGS)`,
        );
        requireStatus(afterRemove, "OK", "UID FETCH after removing \\Seen");
        const removedMetadata = parseFetchMetadata(afterRemove.text);
        assert.equal(
          removedMetadata.length,
          1,
          "expected one FETCH record after STORE removal",
        );
        assert.ok(
          !removedMetadata[0].flags.some(
            (flag) => flag.toLowerCase() === "\\seen",
          ),
          "\\Seen remained after STORE removal",
        );
        const removedState = await controlRequest(controlRoot, "/_test/state");
        const removedMessage = removedState.messages.find(
          (item) => item.message_id === messageId,
        );
        assert.ok(
          !removedMessage.labels.includes("read"),
          "STORE did not remove AgentMail read label",
        );
        assert.ok(
          removedMessage.labels.includes("unread"),
          "STORE did not restore AgentMail unread label",
        );
      },
    );
  }

  await check("LOGOUT completes cleanly", async () => {
    await logout(mainClient);
  });

  let reconnectSelected;
  let reconnectSnapshot;
  await check("UIDs and UIDVALIDITY survive a reconnect", async () => {
    const session = await loginAndSelect(
      imapHost,
      imapPort,
      initialFixtures.length,
    );
    reconnectSelected = session.selected;
    reconnectSnapshot = await fetchSnapshot(session.client, initialFixtures);
    compareUidMaps(
      firstSnapshot.byMessageId,
      reconnectSnapshot.byMessageId,
      "reconnect",
    );
    assert.equal(
      reconnectSelected.uidValidity,
      firstSelected.uidValidity,
      "UIDVALIDITY changed across reconnect",
    );
    await logout(session.client);
  });

  let finalSelected;
  let finalSnapshot;
  await check(
    "a new message receives a new UID without renumbering existing messages",
    async () => {
      await controlRequest(controlRoot, "/_test/add-message", {
        method: "POST",
      });
      const expected = [...initialFixtures, ADDED_MESSAGE];
      const session = await loginAndSelect(imapHost, imapPort, expected.length);
      finalSelected = session.selected;
      finalSnapshot = await fetchSnapshot(session.client, expected);
      compareUidMaps(
        firstSnapshot.byMessageId,
        finalSnapshot.byMessageId,
        "new-message sync",
      );
      const newUid = finalSnapshot.byMessageId.get(ADDED_MESSAGE.id);
      requirePositiveInteger(newUid, "new message UID");
      assert.ok(
        !new Set(firstSnapshot.byMessageId.values()).has(newUid),
        "new message reused an existing UID",
      );
      assert.ok(
        newUid > Math.max(...firstSnapshot.byMessageId.values()),
        "new message UID is not monotonic",
      );
      assert.ok(
        finalSelected.uidNext > newUid,
        "UIDNEXT did not advance past the new UID",
      );
      assert.equal(
        finalSelected.uidValidity,
        firstSelected.uidValidity,
        "UIDVALIDITY changed after sync",
      );
      await logout(session.client);
    },
  );

  const serializableSnapshot = {
    uid_validity: finalSelected.uidValidity,
    messages: Object.fromEntries(
      [...finalSnapshot.byMessageId.entries()].sort(),
    ),
  };

  if (options.verifyUids) {
    await check("UIDs survive a candidate-server process restart", async () => {
      const previous = JSON.parse(await readFile(DEFAULT_SNAPSHOT, "utf8"));
      assert.equal(
        serializableSnapshot.uid_validity,
        previous.uid_validity,
        "UIDVALIDITY changed after process restart",
      );
      for (const [messageId, uid] of Object.entries(previous.messages)) {
        assert.equal(
          serializableSnapshot.messages[messageId],
          uid,
          `${messageId} changed UID after process restart`,
        );
      }
    });
  }

  if (options.recordUids) {
    await check("UID restart snapshot is recorded", async () => {
      await mkdir(path.dirname(DEFAULT_SNAPSHOT), { recursive: true });
      await writeFile(
        DEFAULT_SNAPSHOT,
        `${JSON.stringify(serializableSnapshot, null, 2)}\n`,
        "utf8",
      );
    });
  }

  process.stdout.write(`\nPASS: ${passed} acceptance checkpoints completed.\n`);
  if (!options.recordUids && !options.verifyUids) {
    process.stdout.write(
      "Run the UID record/verify commands around a candidate-server restart to validate persistence.\n",
    );
  }
}

run().catch((error) => {
  process.stderr.write(`\nFAIL: ${redact(error.stack || error)}\n`);
  process.exitCode = 1;
});
