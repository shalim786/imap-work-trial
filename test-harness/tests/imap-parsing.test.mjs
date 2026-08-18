import assert from "node:assert/strict";
import test from "node:test";

import {
  extractFirstLiteral,
  parseFetchMetadata,
  parseSelectResponse,
  quoteImap,
} from "../lib/imap-client.mjs";

test("parses mailbox selection metadata", () => {
  const selected = parseSelectResponse(
    "* 4 EXISTS\r\n* OK [UIDVALIDITY 1234] stable\r\n* OK [UIDNEXT 9] next\r\nH1 OK done\r\n",
  );
  assert.deepEqual(selected, { exists: 4, uidValidity: 1234, uidNext: 9 });
});

test("parses FETCH metadata in varying attribute order", () => {
  const records = parseFetchMetadata(
    "* 1 FETCH (FLAGS (\\Seen \\Flagged) RFC822.SIZE 321 UID 7)\r\n" +
      "* 2 FETCH (UID 9 RFC822.SIZE 99 FLAGS ())\r\n" +
      "H1 OK complete\r\n",
  );
  assert.deepEqual(records, [
    { sequence: 1, uid: 7, size: 321, flags: ["\\Seen", "\\Flagged"] },
    { sequence: 2, uid: 9, size: 99, flags: [] },
  ]);
});

test("extracts literals by byte length without UTF-8 string conversion", () => {
  const raw = Buffer.from("café 東京 🚀", "utf8");
  const response = Buffer.concat([
    Buffer.from(`* 1 FETCH (BODY[] {${raw.length}}\r\n`, "ascii"),
    raw,
    Buffer.from(")\r\nH1 OK complete\r\n", "ascii"),
  ]);
  const literal = extractFirstLiteral(response);
  assert.equal(literal.declaredLength, raw.length);
  assert.ok(literal.bytes.equals(raw));
});

test("quotes IMAP credentials safely", () => {
  assert.equal(quoteImap('a"b\\c'), '"a\\"b\\\\c"');
});
