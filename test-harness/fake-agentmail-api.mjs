import http from "node:http";
import { pathToFileURL } from "node:url";

import {
  ADDED_MESSAGE,
  API_KEY,
  BASE_MESSAGES,
  INBOX_ID,
  cloneFixture,
  rawSha256,
} from "./lib/fixtures.mjs";

const DEFAULT_HOST = process.env.HARNESS_API_HOST || "127.0.0.1";
const DEFAULT_PORT = Number(process.env.HARNESS_API_PORT || 3210);
const MAX_BODY_BYTES = 1_000_000;

function json(res, status, value) {
  const body = Buffer.from(JSON.stringify(value, null, 2));
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": body.length,
    "cache-control": "no-store",
  });
  res.end(body);
}

function apiError(res, status, message) {
  json(res, status, { error: { message } });
}

async function readJson(req) {
  const chunks = [];
  let length = 0;
  for await (const chunk of req) {
    length += chunk.length;
    if (length > MAX_BODY_BYTES) {
      throw new Error("request body is too large");
    }
    chunks.push(chunk);
  }
  if (length === 0) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function listValue(searchParams, key) {
  return searchParams
    .getAll(key)
    .flatMap((value) => value.split(","))
    .map((value) => value.trim())
    .filter(Boolean);
}

function decodeOffset(token) {
  if (!token) return 0;
  try {
    const decoded = Buffer.from(token, "base64url").toString("utf8");
    const match = /^offset:(\d+)$/.exec(decoded);
    return match ? Number(match[1]) : NaN;
  } catch {
    return NaN;
  }
}

function encodeOffset(offset) {
  return Buffer.from(`offset:${offset}`, "utf8").toString("base64url");
}

function messageJson(message, includeBody = false) {
  const rawMessageId = /^Message-ID:\s*(.+)\r$/im.exec(
    message.raw.toString("utf8"),
  )?.[1];
  const value = {
    inbox_id: INBOX_ID,
    thread_id: message.threadId,
    message_id: message.id,
    labels: [...message.labels],
    timestamp: message.timestamp,
    from: message.from,
    to: [...message.to],
    subject: message.subject,
    preview: message.preview,
    headers: {
      "Message-ID": rawMessageId || `<${message.id}@api.fixture>`,
    },
    size: message.raw.length,
    updated_at: message.timestamp,
    created_at: message.timestamp,
  };
  if (includeBody) value.text = message.text;
  return value;
}

function normalizePath(pathname) {
  return pathname.startsWith("/v0/") ? pathname.slice(3) : pathname;
}

function hasValidAuth(req) {
  const authorization = req.headers.authorization || "";
  const apiKeyHeader = req.headers["x-api-key"] || "";
  return authorization === `Bearer ${API_KEY}` || apiKeyHeader === API_KEY;
}

function boolParam(searchParams, key) {
  return searchParams.get(key)?.toLowerCase() === "true";
}

export function createFakeAgentMailServer({
  host = DEFAULT_HOST,
  port = DEFAULT_PORT,
  quiet = process.env.HARNESS_QUIET === "1",
} = {}) {
  let messages = new Map();
  let failures = [];
  let requests = [];

  const reset = () => {
    messages = new Map(
      BASE_MESSAGES.map((item) => [item.id, cloneFixture(item)]),
    );
    failures = [];
    requests = [];
  };
  reset();

  const server = http.createServer(async (req, res) => {
    const requestUrl = new URL(
      req.url || "/",
      `http://${req.headers.host || `${host}:${port}`}`,
    );
    const path = normalizePath(requestUrl.pathname);
    requests.push({
      method: req.method,
      path: requestUrl.pathname,
      at: new Date().toISOString(),
      authorization_present: Boolean(
        req.headers.authorization || req.headers["x-api-key"],
      ),
    });
    requests = requests.slice(-100);

    if (!quiet) {
      process.stdout.write(`[fake-api] ${req.method} ${requestUrl.pathname}\n`);
    }

    try {
      if (path === "/health" && req.method === "GET") {
        return json(res, 200, { ok: true });
      }

      if (path === "/_test/reset" && req.method === "POST") {
        reset();
        return json(res, 200, { ok: true, message_count: messages.size });
      }

      if (path === "/_test/add-message" && req.method === "POST") {
        messages.set(ADDED_MESSAGE.id, cloneFixture(ADDED_MESSAGE));
        return json(res, 201, {
          ok: true,
          message: messageJson(messages.get(ADDED_MESSAGE.id), true),
        });
      }

      if (path === "/_test/fail-next" && req.method === "POST") {
        const body = await readJson(req);
        const failure = {
          method: String(body.method || "GET").toUpperCase(),
          path_prefix: normalizePath(String(body.path_prefix || "/")),
          status: Number(body.status || 503),
          delay_ms: Math.max(0, Number(body.delay_ms || 0)),
          times: Math.max(1, Number(body.times || 1)),
        };
        failures.push(failure);
        return json(res, 201, { ok: true, failure });
      }

      if (path === "/_test/requests" && req.method === "GET") {
        return json(res, 200, { requests });
      }

      if (path === "/_test/state" && req.method === "GET") {
        return json(res, 200, {
          inbox_id: INBOX_ID,
          messages: [...messages.values()].map((message) => ({
            ...messageJson(message, true),
            raw_sha256: rawSha256(message.raw),
          })),
        });
      }

      const failure = failures.find(
        (item) =>
          item.method === req.method && path.startsWith(item.path_prefix),
      );
      if (failure) {
        failure.times -= 1;
        if (failure.times <= 0)
          failures = failures.filter((item) => item !== failure);
        if (failure.delay_ms > 0) {
          await new Promise((resolve) => setTimeout(resolve, failure.delay_ms));
        }
        return apiError(res, failure.status, "Injected test failure");
      }

      const rawMatch = /^\/raw\/([^/]+)$/.exec(path);
      if (rawMatch && req.method === "GET") {
        const message = messages.get(decodeURIComponent(rawMatch[1]));
        if (!message) return apiError(res, 404, "Raw message not found");
        res.writeHead(200, {
          "content-type": "message/rfc822",
          "content-length": message.raw.length,
          "cache-control": "no-store",
        });
        return res.end(message.raw);
      }

      if (!hasValidAuth(req)) {
        return apiError(res, 401, "Invalid AgentMail API key");
      }

      if (path === "/auth/me" && req.method === "GET") {
        return json(res, 200, {
          scope_type: "inbox",
          scope_id: INBOX_ID,
          organization_id: "org_interview",
          pod_id: "pod_interview",
          inbox_id: INBOX_ID,
          api_key_id: "api_key_interview",
        });
      }

      if (
        (path === "/inboxes" || path === "/inboxes/") &&
        req.method === "GET"
      ) {
        return json(res, 200, {
          count: 1,
          limit: 1,
          inboxes: [
            {
              pod_id: "pod_interview",
              inbox_id: INBOX_ID,
              email: INBOX_ID,
              display_name: "IMAP Candidate",
              updated_at: "2026-08-18T00:00:00.000Z",
              created_at: "2026-08-01T00:00:00.000Z",
            },
          ],
        });
      }

      const inboxMatch = /^\/inboxes\/([^/]+)$/.exec(path);
      if (inboxMatch && req.method === "GET") {
        if (decodeURIComponent(inboxMatch[1]) !== INBOX_ID) {
          return apiError(res, 404, "Inbox not found");
        }
        return json(res, 200, {
          pod_id: "pod_interview",
          inbox_id: INBOX_ID,
          email: INBOX_ID,
          display_name: "IMAP Candidate",
          updated_at: "2026-08-18T00:00:00.000Z",
          created_at: "2026-08-01T00:00:00.000Z",
        });
      }

      const listMatch = /^\/inboxes\/([^/]+)\/messages\/?$/.exec(path);
      if (listMatch && req.method === "GET") {
        if (decodeURIComponent(listMatch[1]) !== INBOX_ID) {
          return apiError(res, 404, "Inbox not found");
        }

        const requestedLabels = listValue(requestUrl.searchParams, "labels");
        const includeTrash = boolParam(
          requestUrl.searchParams,
          "include_trash",
        );
        const includeSpam = boolParam(requestUrl.searchParams, "include_spam");
        const ascending = boolParam(requestUrl.searchParams, "ascending");
        let items = [...messages.values()].filter((message) => {
          if (requestedLabels.length > 0) {
            return requestedLabels.every((label) =>
              message.labels.includes(label),
            );
          }
          if (!includeTrash && message.labels.includes("trash")) return false;
          if (!includeSpam && message.labels.includes("spam")) return false;
          return true;
        });
        items.sort((a, b) => a.timestamp.localeCompare(b.timestamp));
        if (!ascending) items.reverse();

        const offset = decodeOffset(requestUrl.searchParams.get("page_token"));
        if (!Number.isInteger(offset) || offset < 0) {
          return apiError(res, 400, "Invalid page_token");
        }
        const requestedLimit = Number(
          requestUrl.searchParams.get("limit") || 50,
        );
        if (!Number.isFinite(requestedLimit) || requestedLimit < 1) {
          return apiError(res, 400, "Invalid limit");
        }

        // Deliberately return short pages. Clients must follow next_page_token
        // instead of assuming the server always fills the requested limit.
        const pageSize = Math.min(Math.floor(requestedLimit), 2);
        const page = items.slice(offset, offset + pageSize);
        const nextOffset = offset + page.length;
        return json(res, 200, {
          count: page.length,
          limit: pageSize,
          ...(nextOffset < items.length
            ? { next_page_token: encodeOffset(nextOffset) }
            : {}),
          messages: page.map((message) => messageJson(message)),
        });
      }

      const rawApiMatch = /^\/inboxes\/([^/]+)\/messages\/([^/]+)\/raw$/.exec(
        path,
      );
      if (rawApiMatch && req.method === "GET") {
        if (decodeURIComponent(rawApiMatch[1]) !== INBOX_ID) {
          return apiError(res, 404, "Inbox not found");
        }
        const message = messages.get(decodeURIComponent(rawApiMatch[2]));
        if (!message) return apiError(res, 404, "Message not found");
        const address = server.address();
        const actualPort =
          typeof address === "object" && address ? address.port : port;
        return json(res, 200, {
          message_id: message.id,
          size: message.raw.length,
          download_url: `http://${host}:${actualPort}/raw/${encodeURIComponent(message.id)}`,
          expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
        });
      }

      const messageMatch = /^\/inboxes\/([^/]+)\/messages\/([^/]+)$/.exec(path);
      if (messageMatch) {
        if (decodeURIComponent(messageMatch[1]) !== INBOX_ID) {
          return apiError(res, 404, "Inbox not found");
        }
        const message = messages.get(decodeURIComponent(messageMatch[2]));
        if (!message) return apiError(res, 404, "Message not found");

        if (req.method === "GET") {
          return json(res, 200, messageJson(message, true));
        }

        if (req.method === "PATCH") {
          const body = await readJson(req);
          const asList = (value) => {
            if (value === undefined) return [];
            return Array.isArray(value) ? value.map(String) : [String(value)];
          };
          const addLabels = asList(body.add_labels);
          const removeLabels = new Set(asList(body.remove_labels));
          const nextLabels = message.labels.filter(
            (label) => !removeLabels.has(label),
          );
          for (const label of addLabels) {
            if (!nextLabels.includes(label)) nextLabels.push(label);
          }
          message.labels = nextLabels;
          return json(res, 200, {
            message_id: message.id,
            labels: [...message.labels],
          });
        }
      }

      return apiError(res, 404, `No fake endpoint for ${req.method} ${path}`);
    } catch (error) {
      return apiError(
        res,
        400,
        error instanceof Error ? error.message : "Bad request",
      );
    }
  });

  return {
    server,
    async start() {
      await new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(port, host, () => {
          server.off("error", reject);
          resolve();
        });
      });
      const address = server.address();
      const actualPort =
        typeof address === "object" && address ? address.port : port;
      return {
        host,
        port: actualPort,
        baseUrl: `http://${host}:${actualPort}/v0`,
      };
    },
    async stop() {
      if (!server.listening) return;
      await new Promise((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
      });
    },
  };
}

async function main() {
  const fake = createFakeAgentMailServer();
  const address = await fake.start();
  process.stdout.write(`\nFake AgentMail API ready at ${address.baseUrl}\n`);
  process.stdout.write(`AGENTMAIL_API_URL=${address.baseUrl}\n`);
  process.stdout.write(`AGENTMAIL_INBOX_ID=${INBOX_ID}\n`);
  process.stdout.write(`AGENTMAIL_API_KEY=${API_KEY}\n\n`);

  const shutdown = async () => {
    await fake.stop();
    process.exit(0);
  };
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
}

if (import.meta.url === pathToFileURL(process.argv[1] || "").href) {
  main().catch((error) => {
    process.stderr.write(`${error.stack || error}\n`);
    process.exit(1);
  });
}
