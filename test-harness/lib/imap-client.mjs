import net from "node:net";

const CRLF = Buffer.from("\r\n", "ascii");

function escapeRegex(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export class ImapTestClient {
  constructor({ host, port, timeoutMs = 5_000 }) {
    this.host = host;
    this.port = port;
    this.timeoutMs = timeoutMs;
    this.buffer = Buffer.alloc(0);
    this.socket = null;
    this.waiters = new Set();
    this.counter = 0;
    this.closed = false;
    this.closeError = null;
  }

  async connect() {
    this.socket = net.createConnection({ host: this.host, port: this.port });
    this.socket.on("data", (chunk) => {
      this.buffer = Buffer.concat([this.buffer, chunk]);
      this.#flushWaiters();
    });
    this.socket.on("error", (error) => {
      this.closeError = error;
      this.#flushWaiters();
    });
    this.socket.on("close", () => {
      this.closed = true;
      this.#flushWaiters();
    });

    await new Promise((resolve, reject) => {
      const onConnect = () => {
        cleanup();
        resolve();
      };
      const onError = (error) => {
        cleanup();
        reject(error);
      };
      const cleanup = () => {
        this.socket.off("connect", onConnect);
        this.socket.off("error", onError);
      };
      this.socket.once("connect", onConnect);
      this.socket.once("error", onError);
    });

    return this.takeLine("server greeting");
  }

  nextTag() {
    this.counter += 1;
    return `H${String(this.counter).padStart(6, "0")}`;
  }

  write(value) {
    if (!this.socket || this.closed) throw new Error("IMAP socket is not open");
    this.socket.write(value);
  }

  async writeChunks(chunks, delayMs = 15) {
    for (const chunk of chunks) {
      this.write(chunk);
      if (delayMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, delayMs));
      }
    }
  }

  async command(command) {
    const tag = this.nextTag();
    this.write(`${tag} ${command}\r\n`);
    return this.waitForTagged(tag);
  }

  async takeLine(description = "line") {
    return this.#take((buffer) => {
      const index = buffer.indexOf(CRLF);
      if (index < 0) return null;
      const end = index + CRLF.length;
      return { value: buffer.subarray(0, end), consumed: end };
    }, description);
  }

  async waitForTagged(tag) {
    const terminal = new RegExp(
      `(?:^|\\r\\n)${escapeRegex(tag)} +(OK|NO|BAD)(?: +[^\\r\\n]*)?\\r\\n`,
      "i",
    );
    const response = await this.#take((buffer) => {
      const text = buffer.toString("latin1");
      const match = terminal.exec(text);
      if (!match) return null;
      const end = match.index + match[0].length;
      return { value: buffer.subarray(0, end), consumed: end };
    }, `tagged response for ${tag}`);
    const text = response.toString("latin1");
    const statusMatch = new RegExp(
      `${escapeRegex(tag)} +(OK|NO|BAD)`,
      "i",
    ).exec(text);
    return {
      tag,
      status: statusMatch?.[1]?.toUpperCase() || "UNKNOWN",
      buffer: response,
      text,
    };
  }

  destroy() {
    this.socket?.destroy();
    this.closed = true;
    this.#flushWaiters();
  }

  async #take(extractor, description) {
    const immediate = extractor(this.buffer);
    if (immediate) {
      this.buffer = this.buffer.subarray(immediate.consumed);
      return immediate.value;
    }
    if (this.closeError) throw this.closeError;
    if (this.closed)
      throw new Error(`connection closed while waiting for ${description}`);

    return new Promise((resolve, reject) => {
      const waiter = { extractor, description, resolve, reject, timer: null };
      waiter.timer = setTimeout(() => {
        this.waiters.delete(waiter);
        reject(
          new Error(
            `timed out after ${this.timeoutMs}ms waiting for ${description}`,
          ),
        );
      }, this.timeoutMs);
      this.waiters.add(waiter);
      this.#flushWaiters();
    });
  }

  #flushWaiters() {
    for (const waiter of [...this.waiters]) {
      const result = waiter.extractor(this.buffer);
      if (result) {
        this.waiters.delete(waiter);
        clearTimeout(waiter.timer);
        this.buffer = this.buffer.subarray(result.consumed);
        waiter.resolve(result.value);
        continue;
      }
      if (this.closeError || this.closed) {
        this.waiters.delete(waiter);
        clearTimeout(waiter.timer);
        waiter.reject(
          this.closeError ||
            new Error(
              `connection closed while waiting for ${waiter.description}`,
            ),
        );
      }
    }
  }
}

export function parseSelectResponse(text) {
  const exists = /^\* +(\d+) +EXISTS\r?$/im.exec(text);
  const uidValidity = /\[UIDVALIDITY +(\d+)\]/i.exec(text);
  const uidNext = /\[UIDNEXT +(\d+)\]/i.exec(text);
  return {
    exists: exists ? Number(exists[1]) : null,
    uidValidity: uidValidity ? Number(uidValidity[1]) : null,
    uidNext: uidNext ? Number(uidNext[1]) : null,
  };
}

export function parseFetchMetadata(text) {
  const records = [];
  const pattern = /^\* +(\d+) +FETCH +\((.*?)\)\r?$/gims;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    const attributes = match[2];
    const uid = /(?:^|\s)UID +(\d+)(?:\s|$)/i.exec(attributes);
    const size = /(?:^|\s)RFC822\.SIZE +(\d+)(?:\s|$)/i.exec(attributes);
    const flags = /(?:^|\s)FLAGS +\(([^)]*)\)/i.exec(attributes);
    if (!uid) continue;
    records.push({
      sequence: Number(match[1]),
      uid: Number(uid[1]),
      size: size ? Number(size[1]) : null,
      flags: flags ? flags[1].trim().split(/\s+/).filter(Boolean) : [],
    });
  }
  return records;
}

export function extractFirstLiteral(buffer) {
  const text = buffer.toString("latin1");
  const marker = /\{(\d+)\+?\}\r\n/.exec(text);
  if (!marker) return null;
  const declaredLength = Number(marker[1]);
  const start = marker.index + marker[0].length;
  const end = start + declaredLength;
  if (end > buffer.length) {
    throw new Error(
      `literal declares ${declaredLength} bytes but only ${buffer.length - start} were returned`,
    );
  }
  return {
    declaredLength,
    bytes: buffer.subarray(start, end),
    start,
    end,
  };
}

export function quoteImap(value) {
  return `"${String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}
