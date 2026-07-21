import fs from "node:fs";
import os from "node:os";
import path from "node:path";


const CDP_MESSAGE_MAX_BYTES = 2 * 1024 * 1024;
const CDP_VERSION_MAX_BYTES = 64 * 1024;
const DEFAULT_SINGLE_RESPONSE_MAX_BYTES = 8 * 1024 * 1024;
const DEFAULT_AGGREGATE_RESPONSE_MAX_BYTES = 32 * 1024 * 1024;
const RESPONSE_BUDGET_HARD_MAX_BYTES = 64 * 1024 * 1024;
const MIN_CAPTURE_TEXT_CODE_POINTS = 200;
const PASSIVE_BINARY_RESOURCE_TYPES = new Set([
  "Font",
  "Image",
  "Media",
  "Prefetch",
  "TextTrack",
]);
const TRANSIENT_NAVIGATION_ERRORS = new Set([
  "net::ERR_CONNECTION_CLOSED",
  "net::ERR_CONNECTION_RESET",
  "net::ERR_HTTP2_PROTOCOL_ERROR",
  "net::ERR_NETWORK_CHANGED",
  "net::ERR_QUIC_PROTOCOL_ERROR",
  "net::ERR_TEMPORARILY_THROTTLED",
  "net::ERR_TIMED_OUT",
]);
const BLOCKED_NON_HTTP_URL_PATTERNS = [
  "blob:*",
  "data:*",
  "file://*",
  "filesystem:*",
  "ftp://*",
  "intent:*",
  "javascript:*",
  "mailto:*",
  "sms:*",
  "stun://*",
  "tel:*",
  "turn://*",
  "turns://*",
  "webcal:*",
  "ws://*",
  "wss://*",
];
const NETWORK_API_HARDENING_SOURCE = `(() => {
  "use strict";
  const deny = function PaperReaderBlockedNetworkAPI() {
    throw new DOMException("Blocked by Paper Reader strict capture", "SecurityError");
  };
  Object.freeze(deny);
  const replace = (root, name, value) => {
    let cursor = root;
    while (cursor) {
      if (Object.prototype.hasOwnProperty.call(cursor, name)) {
        Object.defineProperty(cursor, name, {
          value,
          writable: false,
          configurable: false,
          enumerable: false,
        });
      }
      cursor = Object.getPrototypeOf(cursor);
    }
    if (name in root && root[name] !== value) {
      Object.defineProperty(root, name, {
        value,
        writable: false,
        configurable: false,
        enumerable: false,
      });
    }
  };
  for (const name of [
    "EventSource",
    "RTCPeerConnection",
    "SharedWorker",
    "WebSocket",
    "WebTransport",
    "Worker",
    "webkitRTCPeerConnection",
  ]) {
    if (name in globalThis) replace(globalThis, name, deny);
  }
  if ("open" in globalThis) replace(globalThis, "open", () => null);
  if (globalThis.navigator && "sendBeacon" in globalThis.navigator) {
    replace(globalThis.navigator, "sendBeacon", () => false);
  }
  Object.defineProperty(globalThis, "__paperReaderStrictNetworkGuard", {
    value: true,
    writable: false,
    configurable: false,
    enumerable: false,
  });
  return globalThis.__paperReaderStrictNetworkGuard === true;
})()`;


function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}


function blankPageData() {
  return {
    title: "",
    description: "",
    publisher: "",
    publishedAt: "",
    finalUrl: "about:blank",
    readyState: "",
    text: "",
  };
}


function hasUsablePageData(data) {
  return (
    data &&
    typeof data.finalUrl === "string" &&
    data.finalUrl !== "about:blank" &&
    ["interactive", "complete"].includes(data.readyState) &&
    typeof data.title === "string" &&
    data.title.trim().length > 0 &&
    typeof data.text === "string" &&
    Array.from(data.text.trim()).length >= MIN_CAPTURE_TEXT_CODE_POINTS
  );
}


function isAllowedFrameNavigationUrl(value) {
  if (value === "about:blank") {
    return true;
  }
  try {
    return ["http:", "https:"].includes(new URL(value).protocol);
  } catch {
    return false;
  }
}


function requireCdpIdentity(value, errorCode) {
  if (typeof value !== "string" || value.length === 0 || value.length > 512) {
    throw new Error(errorCode);
  }
  return value;
}


const CDP_RESOURCE_CLASSES = new Map([
  ["document", "document"],
  ["stylesheet", "stylesheet"],
  ["image", "image"],
  ["media", "media"],
  ["font", "font"],
  ["script", "script"],
  ["texttrack", "texttrack"],
  ["xhr", "xhr"],
  ["fetch", "fetch"],
  ["prefetch", "prefetch"],
  ["eventsource", "eventsource"],
  ["websocket", "websocket"],
  ["manifest", "manifest"],
  ["signedexchange", "signedexchange"],
  ["ping", "ping"],
  ["cspviolationreport", "cspviolationreport"],
  ["preflight", "preflight"],
]);
const INVALID_URL_CHECKPOINTS = new Set([
  "fetch_request_paused",
  "network_request_will_be_sent",
]);


class InvalidNetworkRequestUrlError extends Error {
  constructor(diagnostic) {
    super("invalid_network_request_url");
    this.name = "InvalidNetworkRequestUrlError";
    this.diagnostic = diagnostic;
  }
}


function classifyCdpResource(value) {
  if (typeof value !== "string" || value.length === 0) {
    return "none";
  }
  if (value.length > 64) {
    return "other";
  }
  return CDP_RESOURCE_CLASSES.get(value.toLowerCase()) || "other";
}


function classifyInvalidUrlValue(value) {
  let valueType;
  if (value === null) {
    valueType = "null";
  } else if (Array.isArray(value)) {
    valueType = "array";
  } else {
    valueType = typeof value;
  }
  if (
    ![
      "string",
      "number",
      "boolean",
      "object",
      "null",
      "array",
      "undefined",
    ].includes(valueType)
  ) {
    valueType = "other";
  }
  if (valueType === "undefined") {
    valueType = "missing";
  }

  let lengthBucket = "not_applicable";
  let parseability = "not_attempted";
  let schemeClass = "none";
  if (typeof value === "string") {
    if (value.length === 0) {
      lengthBucket = "empty";
    } else if (value.length > 4096) {
      lengthBucket = "over_limit";
    } else {
      lengthBucket = "bounded";
      try {
        new URL(value);
        parseability = "valid";
      } catch {
        parseability = "invalid";
      }
    }
    const schemeMatch = /^([A-Za-z][A-Za-z0-9+.-]*):/.exec(value.slice(0, 64));
    if (schemeMatch) {
      const scheme = schemeMatch[1].toLowerCase();
      schemeClass = scheme === "http" || scheme === "https" ? scheme : "other";
    }
  }
  return {valueType, lengthBucket, parseability, schemeClass};
}


function invalidNetworkRequestUrlError(value, {checkpoint, resourceType}) {
  const {valueType, lengthBucket, parseability, schemeClass} =
    classifyInvalidUrlValue(value);
  const resourceClass = classifyCdpResource(resourceType);
  const checkpointClass = INVALID_URL_CHECKPOINTS.has(checkpoint)
    ? checkpoint
    : "invalid";
  return new InvalidNetworkRequestUrlError(
    "invalid_network_request_url:" +
      `checkpoint=${checkpointClass};resource=${resourceClass};value=${valueType};` +
      `length=${lengthBucket};parse=${parseability};scheme=${schemeClass}`,
  );
}


function recordRequestAccountingError(warnings, error) {
  warnings.add(String(error?.message || "invalid_network_request_identity"));
  if (error instanceof InvalidNetworkRequestUrlError) {
    warnings.add(error.diagnostic);
  }
}


function isOversizedBlockedPassiveInlineDataUrl(value, resourceType) {
  // data: has no proxy hop; final accounting still requires a proven browser block.
  return (
    typeof value === "string" &&
    value.length > 4096 &&
    PASSIVE_BINARY_RESOURCE_TYPES.has(resourceType) &&
    value.slice(0, 5).toLowerCase() === "data:"
  );
}


function passiveDataProofKey(sessionId, requestId, resourceType) {
  if (
    typeof sessionId !== "string" ||
    sessionId.length === 0 ||
    sessionId.length > 512 ||
    typeof requestId !== "string" ||
    requestId.length === 0 ||
    requestId.length > 512 ||
    !PASSIVE_BINARY_RESOURCE_TYPES.has(resourceType)
  ) {
    throw new Error("invalid_network_request_identity");
  }
  return JSON.stringify([sessionId, requestId, resourceType]);
}


function passiveDataAccountingKey(sessionId, requestId, value, resourceType) {
  if (!isOversizedBlockedPassiveInlineDataUrl(value, resourceType)) {
    return null;
  }
  return passiveDataProofKey(sessionId, requestId, resourceType);
}


function requestAccountingKey(sessionId, requestId, value, context) {
  if (isOversizedBlockedPassiveInlineDataUrl(value, context?.resourceType)) {
    return null;
  }
  if (typeof value !== "string" || value.length === 0 || value.length > 4096) {
    throw invalidNetworkRequestUrlError(value, context);
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw invalidNetworkRequestUrlError(value, context);
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    return null;
  }
  if (
    typeof sessionId !== "string" ||
    sessionId.length === 0 ||
    sessionId.length > 512 ||
    typeof requestId !== "string" ||
    requestId.length === 0 ||
    requestId.length > 512
  ) {
    throw new Error("invalid_network_request_identity");
  }
  return JSON.stringify([sessionId, requestId, parsed.href]);
}


function responseAccountingKey(sessionId, requestId) {
  if (
    typeof sessionId !== "string" ||
    sessionId.length === 0 ||
    sessionId.length > 512 ||
    typeof requestId !== "string" ||
    requestId.length === 0 ||
    requestId.length > 512
  ) {
    throw new Error("invalid_response_accounting");
  }
  return JSON.stringify([sessionId, requestId]);
}


function requireResponseByteCount(value) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error("invalid_response_accounting");
  }
  return value;
}


function authorityAccountingKey(protocol, hostname, port) {
  let normalizedHostname = String(hostname || "").replace(/\.$/, "").toLowerCase();
  if (normalizedHostname.startsWith("[") && normalizedHostname.endsWith("]")) {
    normalizedHostname = normalizedHostname.slice(1, -1);
  }
  if (
    !["http:", "https:"].includes(protocol) ||
    !normalizedHostname ||
    !Number.isSafeInteger(port) ||
    port < 1 ||
    port > 65535
  ) {
    return null;
  }
  return JSON.stringify([protocol, normalizedHostname, port]);
}


function requestAuthorityKey(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    return null;
  }
  const port = parsed.port
    ? Number.parseInt(parsed.port, 10)
    : (parsed.protocol === "https:" ? 443 : 80);
  return authorityAccountingKey(parsed.protocol, parsed.hostname, port);
}


function incrementCount(counts, key) {
  counts.set(key, (counts.get(key) || 0) + 1);
}


function cdpRequestHasBody(request) {
  if (
    request?.hasPostData === true ||
    (typeof request?.postData === "string" && request.postData.length > 0) ||
    (Array.isArray(request?.postDataEntries) && request.postDataEntries.length > 0)
  ) {
    return true;
  }
  const headers = request?.headers;
  if (!headers || typeof headers !== "object" || Array.isArray(headers)) {
    return false;
  }
  for (const [name, value] of Object.entries(headers)) {
    const normalizedName = name.toLowerCase();
    if (normalizedName === "transfer-encoding") {
      return true;
    }
    if (normalizedName === "content-length") {
      const normalizedValue = String(value).trim();
      return !/^0+$/.test(normalizedValue);
    }
  }
  return false;
}


function recordUnsafeRequest(warnings, value, code = "unsafe_request_blocked") {
  warnings.add(code);
  let protocol = "invalid";
  try {
    const candidate = new URL(value).protocol.replace(/:$/, "").toLowerCase();
    if (/^[a-z][a-z0-9+.-]{0,31}$/.test(candidate)) {
      protocol = candidate;
    }
  } catch {
    // Keep the bounded invalid marker; never echo an untrusted URL.
  }
  warnings.add(`unsafe_request_scheme:${protocol}`);
}


function defaultActivePortPaths() {
  const home = os.homedir();
  if (os.platform() === "darwin") {
    return [
      path.join(home, "Library/Application Support/Google/Chrome/DevToolsActivePort"),
      path.join(home, "Library/Application Support/Google/Chrome Canary/DevToolsActivePort"),
      path.join(home, "Library/Application Support/Chromium/DevToolsActivePort"),
    ];
  }
  if (os.platform() === "linux") {
    return [
      path.join(home, ".config/google-chrome/DevToolsActivePort"),
      path.join(home, ".config/chromium/DevToolsActivePort"),
    ];
  }
  if (os.platform() === "win32") {
    const localAppData = process.env.LOCALAPPDATA || "";
    return [
      path.join(localAppData, "Google/Chrome/User Data/DevToolsActivePort"),
      path.join(localAppData, "Chromium/User Data/DevToolsActivePort"),
    ];
  }
  return [];
}


function sameFileStat(left, right) {
  return (
    left.dev === right.dev &&
    left.ino === right.ino &&
    left.mode === right.mode &&
    left.nlink === right.nlink &&
    left.size === right.size &&
    left.mtimeNs === right.mtimeNs &&
    left.ctimeNs === right.ctimeNs
  );
}


function readStableActivePortFile(requestedPath) {
  const absolutePath = path.resolve(requestedPath);
  if (fs.realpathSync(requestedPath) !== absolutePath) {
    throw new Error("unsafe_raw_cdp_endpoint");
  }
  const before = fs.lstatSync(absolutePath, {bigint: true});
  const descriptor = fs.openSync(
    absolutePath,
    fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0),
  );
  try {
    const openedBefore = fs.fstatSync(descriptor, {bigint: true});
    if (
      !openedBefore.isFile() ||
      openedBefore.nlink !== 1n ||
      openedBefore.size < 1n ||
      openedBefore.size > 4096n ||
      !sameFileStat(before, openedBefore)
    ) {
      throw new Error("unsafe_raw_cdp_endpoint");
    }
    const raw = fs.readFileSync(descriptor, "utf8");
    const openedAfter = fs.fstatSync(descriptor, {bigint: true});
    const after = fs.lstatSync(absolutePath, {bigint: true});
    if (!sameFileStat(openedBefore, openedAfter) || !sameFileStat(openedBefore, after)) {
      throw new Error("unsafe_raw_cdp_endpoint");
    }
    return raw;
  } finally {
    fs.closeSync(descriptor);
  }
}


function validateRawCdpWebSocket(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("unsafe_raw_cdp_endpoint");
  }
  let hostname = parsed.hostname.toLowerCase();
  if (hostname.startsWith("[") && hostname.endsWith("]")) {
    hostname = hostname.slice(1, -1);
  }
  const port = parsed.port ? Number.parseInt(parsed.port, 10) : 80;
  if (
    parsed.protocol !== "ws:" ||
    parsed.username ||
    parsed.password ||
    !["127.0.0.1", "::1", "localhost"].includes(hostname) ||
    !Number.isSafeInteger(port) ||
    port < 1 ||
    port > 65535 ||
    !/^\/devtools\/browser\/[A-Za-z0-9._-]{1,256}$/.test(parsed.pathname) ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error("unsafe_raw_cdp_endpoint");
  }
  if (hostname === "localhost") {
    parsed.hostname = "127.0.0.1";
  }
  return parsed.href;
}


function validateRawCdpHttpBase(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("unsafe_raw_cdp_endpoint");
  }
  let hostname = parsed.hostname.toLowerCase();
  if (hostname.startsWith("[") && hostname.endsWith("]")) {
    hostname = hostname.slice(1, -1);
  }
  if (
    parsed.protocol !== "http:" ||
    parsed.username ||
    parsed.password ||
    !["127.0.0.1", "::1", "localhost"].includes(hostname) ||
    !["", "/"].includes(parsed.pathname) ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error("unsafe_raw_cdp_endpoint");
  }
  if (hostname === "localhost") {
    parsed.hostname = "127.0.0.1";
  }
  return parsed.origin;
}


async function readBoundedVersionPayload(baseUrl, deadline) {
  const remaining = deadline - Date.now();
  if (remaining <= 0) {
    throw new Error("raw_cdp_endpoint_unavailable");
  }
  const response = await fetch(`${baseUrl}/json/version`, {
    redirect: "error",
    signal: AbortSignal.timeout(Math.max(1, Math.min(2000, remaining))),
  });
  if (!response.ok || !response.body) {
    throw new Error("raw_cdp_endpoint_unavailable");
  }
  const chunks = [];
  let total = 0;
  const reader = response.body.getReader();
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > CDP_VERSION_MAX_BYTES) {
      await reader.cancel().catch(() => null);
      throw new Error("unsafe_raw_cdp_endpoint");
    }
    chunks.push(Buffer.from(value));
  }
  let payload;
  try {
    payload = JSON.parse(Buffer.concat(chunks, total).toString("utf8"));
  } catch {
    throw new Error("unsafe_raw_cdp_endpoint");
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("unsafe_raw_cdp_endpoint");
  }
  return payload;
}


export async function discoverRawCdpWebSocket({
  explicitEndpoint = process.env.ZOTERO_PAPER_READER_CDP_WS_ENDPOINT || null,
  activePortPaths = defaultActivePortPaths(),
  httpBaseUrls = process.env.ZOTERO_PAPER_READER_CDP_HTTP_BASE_URL
    ? [process.env.ZOTERO_PAPER_READER_CDP_HTTP_BASE_URL]
    : [
        "http://127.0.0.1:9222",
        "http://127.0.0.1:9229",
        "http://127.0.0.1:9333",
      ],
  deadline = Date.now() + 6000,
} = {}) {
  if (explicitEndpoint) {
    return validateRawCdpWebSocket(explicitEndpoint);
  }
  for (const activePortPath of activePortPaths) {
    if (!fs.existsSync(activePortPath)) {
      continue;
    }
    const lines = readStableActivePortFile(activePortPath)
      .trim()
      .split(/\r?\n/);
    if (lines.length !== 2 || !/^\d{1,5}$/.test(lines[0])) {
      throw new Error("unsafe_raw_cdp_endpoint");
    }
    const port = Number.parseInt(lines[0], 10);
    if (port < 1 || port > 65535 || !/^\/devtools\/browser\/[A-Za-z0-9._-]{1,256}$/.test(lines[1])) {
      throw new Error("unsafe_raw_cdp_endpoint");
    }
    return validateRawCdpWebSocket(`ws://127.0.0.1:${port}${lines[1]}`);
  }
  for (const requestedBaseUrl of httpBaseUrls) {
    if (Date.now() >= deadline) {
      break;
    }
    const baseUrl = validateRawCdpHttpBase(requestedBaseUrl);
    try {
      const payload = await readBoundedVersionPayload(baseUrl, deadline);
      if (typeof payload.webSocketDebuggerUrl !== "string") {
        throw new Error("unsafe_raw_cdp_endpoint");
      }
      return validateRawCdpWebSocket(payload.webSocketDebuggerUrl);
    } catch (error) {
      if (error instanceof Error && error.message === "unsafe_raw_cdp_endpoint") {
        throw error;
      }
    }
  }
  throw new Error("raw_cdp_endpoint_unavailable");
}


class RawCdpConnection {
  constructor({wsEndpoint, websocketFactory, deadline}) {
    this.wsEndpoint = wsEndpoint;
    this.websocketFactory = websocketFactory;
    this.deadline = deadline;
    this.nextId = 0;
    this.pending = new Map();
    this.eventHandlers = new Set();
    this.socket = null;
    this.closed = false;
    this.failureReason = null;
  }

  onEvent(handler) {
    this.eventHandlers.add(handler);
    return () => this.eventHandlers.delete(handler);
  }

  async open() {
    if (this.socket) return;
    const socket = this.websocketFactory(this.wsEndpoint);
    this.socket = socket;
    await new Promise((resolve, reject) => {
      const remaining = Math.max(1, this.deadline - Date.now());
      const timer = setTimeout(() => {
        cleanup();
        socket.close();
        reject(new Error("raw_cdp_connect_timeout"));
      }, remaining);
      const onOpen = () => {
        cleanup();
        resolve();
      };
      const onError = () => {
        cleanup();
        reject(new Error("raw_cdp_connect_failed"));
      };
      const cleanup = () => {
        clearTimeout(timer);
        socket.removeEventListener?.("open", onOpen);
        socket.removeEventListener?.("error", onError);
      };
      socket.addEventListener("open", onOpen);
      socket.addEventListener("error", onError);
    });
    socket.addEventListener("message", (event) => this.#handleMessage(event));
    socket.addEventListener("close", () => this.#handleClose());
    socket.addEventListener("error", () => this.#handleClose());
  }

  async send(method, params = {}, sessionId = null) {
    if (!this.socket || this.closed || this.socket.readyState !== 1) {
      throw new Error("raw_cdp_not_connected");
    }
    const remaining = this.deadline - Date.now();
    if (remaining <= 0) {
      throw new Error("raw_cdp_command_timeout");
    }
    const id = ++this.nextId;
    const message = {id, method, params};
    if (sessionId) {
      message.sessionId = sessionId;
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`raw_cdp_command_timeout:${method}`));
      }, remaining);
      this.pending.set(id, {method, resolve, reject, timer});
      try {
        this.socket.send(JSON.stringify(message));
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(error);
      }
    });
  }

  close(failureReason = null) {
    if (failureReason && !this.failureReason) {
      this.failureReason = failureReason;
    }
    if (this.closed) return;
    this.closed = true;
    this.socket?.close();
    this.#rejectPending(new Error(this.failureReason || "raw_cdp_closed"));
  }

  #handleMessage(event) {
    const raw = typeof event.data === "string" ? event.data : String(event.data);
    if (Buffer.byteLength(raw, "utf8") > CDP_MESSAGE_MAX_BYTES) {
      this.close("cdp_message_resource_limit");
      return;
    }
    let message;
    try {
      message = JSON.parse(raw);
    } catch {
      this.close("invalid_cdp_message");
      return;
    }
    if (Number.isSafeInteger(message.id) && this.pending.has(message.id)) {
      const pending = this.pending.get(message.id);
      clearTimeout(pending.timer);
      this.pending.delete(message.id);
      if (message.error) {
        pending.reject(new Error(
          `raw_cdp_protocol_error:${pending.method}:${message.error.message || "unknown"}`,
        ));
      } else {
        pending.resolve(message.result || {});
      }
      return;
    }
    if (typeof message.method === "string") {
      for (const handler of this.eventHandlers) {
        handler(message);
      }
    }
  }

  #handleClose() {
    if (this.closed) return;
    this.failureReason = this.failureReason || "raw_cdp_connection_lost";
    this.closed = true;
    this.#rejectPending(new Error(this.failureReason));
  }

  #rejectPending(error) {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }
}


async function installTargetGuards(connection, sessionId, {pageLike = true} = {}) {
  await connection.send("Runtime.enable", {}, sessionId);
  if (pageLike) {
    await connection.send("Page.enable", {}, sessionId);
    await connection.send("Page.addScriptToEvaluateOnNewDocument", {
      source: NETWORK_API_HARDENING_SOURCE,
    }, sessionId);
  }
  const hardening = await connection.send("Runtime.evaluate", {
    expression: NETWORK_API_HARDENING_SOURCE,
    returnByValue: true,
    awaitPromise: false,
  }, sessionId);
  if (
    hardening?.exceptionDetails ||
    hardening?.result?.type !== "boolean" ||
    hardening?.result?.value !== true
  ) {
    throw new Error("network_api_hardening_failed");
  }
  await connection.send("Network.enable", {
    reportDirectSocketTraffic: true,
  }, sessionId);
  await connection.send("Network.setCacheDisabled", {cacheDisabled: true}, sessionId);
  await connection.send("Network.setBypassServiceWorker", {bypass: true}, sessionId);
  await connection.send("Network.setBlockedURLs", {
    urls: BLOCKED_NON_HTTP_URL_PATTERNS,
  }, sessionId);
  await connection.send("Fetch.enable", {
    patterns: [{urlPattern: "*", requestStage: "Request"}],
    handleAuthRequests: true,
  }, sessionId);
  await connection.send("Target.setAutoAttach", {
    autoAttach: true,
    waitForDebuggerOnStart: true,
    flatten: true,
  }, sessionId);
}


export async function capturePageWithRawCdp({
  wsEndpoint,
  sourceUrl,
  policy,
  proxy,
  expression,
  deadline,
  pollMs = 500,
  requestRetries = 2,
  requestRetryMs = 500,
  eventQuiescenceMs = 100,
  maxRequestEvents = 8192,
  maxPendingEventTasks = 256,
  maxGuardedSessions = 64,
  maxSingleResponseBytes = DEFAULT_SINGLE_RESPONSE_MAX_BYTES,
  maxAggregateResponseBytes = DEFAULT_AGGREGATE_RESPONSE_MAX_BYTES,
  websocketFactory = (endpoint) => new WebSocket(endpoint),
}) {
  if (
    !proxy ||
    typeof proxy.seal !== "function" ||
    !Array.isArray(proxy.violations) ||
    !Array.isArray(proxy.blockedConnects) ||
    !Array.isArray(proxy.blockedAfterSeal) ||
    !Number.isSafeInteger(eventQuiescenceMs) ||
    eventQuiescenceMs < 1 ||
    eventQuiescenceMs > 1000 ||
    !Number.isSafeInteger(maxRequestEvents) ||
    maxRequestEvents < 1 ||
    maxRequestEvents > 8192 ||
    !Number.isSafeInteger(maxPendingEventTasks) ||
    maxPendingEventTasks < 1 ||
    maxPendingEventTasks > 256 ||
    !Number.isSafeInteger(maxGuardedSessions) ||
    maxGuardedSessions < 1 ||
    maxGuardedSessions > 256 ||
    !Number.isSafeInteger(maxSingleResponseBytes) ||
    maxSingleResponseBytes < 1 ||
    maxSingleResponseBytes > RESPONSE_BUDGET_HARD_MAX_BYTES ||
    !Number.isSafeInteger(maxAggregateResponseBytes) ||
    maxAggregateResponseBytes < maxSingleResponseBytes ||
    maxAggregateResponseBytes > RESPONSE_BUDGET_HARD_MAX_BYTES
  ) {
    throw new TypeError("invalid_strict_capture_boundary");
  }
  const connection = new RawCdpConnection({wsEndpoint, websocketFactory, deadline});
  const warnings = new Set();
  const ownedFrameIds = new Set();
  const guardedSessions = new Set();
  const networkRequestCounts = new Map();
  const fetchRequestCounts = new Map();
  const passiveDataNetworkSeen = new Map();
  const passiveDataFetchBlocked = new Map();
  const passiveDataInspectorBlocked = new Map();
  const responseByteCounts = new Map();
  const observedNetworkAuthorities = new Set();
  const documentResponses = [];
  const eventTasks = new Set();
  const auditWarnings = new Set();
  let browserContextId = "";
  let targetId = "";
  let rootSessionId = "";
  let rootFrameId = "";
  let lastData = blankPageData();
  let requestEventCount = 0;
  let relevantEventGeneration = 0;
  let captureStopping = false;
  let consecutiveUsableObservations = 0;
  let aggregateDecodedResponseBytes = 0;
  let aggregateEncodedResponseBytes = 0;

  const stopForResponseLimit = (code) => {
    warnings.add(code);
    proxy.seal();
    connection.close(code);
  };
  const accountResponseBytes = ({
    sessionId,
    requestId,
    decodedDelta = 0,
    encodedDelta = 0,
    encodedTotal = null,
  }) => {
    let key;
    let decoded;
    let encoded;
    try {
      key = responseAccountingKey(sessionId, requestId);
      decoded = requireResponseByteCount(decodedDelta);
      encoded = requireResponseByteCount(encodedDelta);
      if (encodedTotal !== null) {
        encodedTotal = requireResponseByteCount(encodedTotal);
      }
    } catch {
      stopForResponseLimit("invalid_response_accounting");
      return;
    }
    const previous = responseByteCounts.get(key) || {decoded: 0, encoded: 0};
    const nextDecoded = previous.decoded + decoded;
    const incrementalEncoded = encodedTotal === null
      ? encoded
      : Math.max(0, encodedTotal - previous.encoded);
    const nextEncoded = previous.encoded + incrementalEncoded;
    if (!Number.isSafeInteger(nextDecoded) || !Number.isSafeInteger(nextEncoded)) {
      stopForResponseLimit("response_resource_limit");
      return;
    }
    responseByteCounts.set(key, {decoded: nextDecoded, encoded: nextEncoded});
    aggregateDecodedResponseBytes += decoded;
    aggregateEncodedResponseBytes += incrementalEncoded;
    if (
      nextDecoded > maxSingleResponseBytes ||
      nextEncoded > maxSingleResponseBytes ||
      aggregateDecodedResponseBytes > maxAggregateResponseBytes ||
      aggregateEncodedResponseBytes > maxAggregateResponseBytes
    ) {
      stopForResponseLimit("response_resource_limit");
    }
  };

  const failPausedRequest = async (requestId, sessionId) => {
    try {
      await connection.send(
        "Fetch.failRequest",
        {requestId, errorReason: "BlockedByClient"},
        sessionId,
      );
      return true;
    } catch {
      warnings.add("cdp_fail_request_failed");
      proxy.seal();
      connection.close("cdp_fail_request_failed");
      return false;
    }
  };

  const queueEventTask = (operation) => {
    if (eventTasks.size >= maxPendingEventTasks) {
      warnings.add("pending_event_task_limit");
      connection.close("pending_event_task_limit");
      return;
    }
    const task = Promise.resolve()
      .then(operation)
      .catch(() => warnings.add("cdp_event_handler_failed"))
      .finally(() => eventTasks.delete(task));
    eventTasks.add(task);
  };
  const drainEventTasks = async () => {
    while (eventTasks.size) {
      await Promise.allSettled([...eventTasks]);
    }
  };

  const removeEventHandler = connection.onEvent((event) => {
    const sessionId = event.sessionId || null;
    if (
      (guardedSessions.has(sessionId) && (
        event.method.startsWith("Fetch.") ||
        event.method.startsWith("Network.") ||
        event.method === "Page.frameNavigated" ||
        event.method === "Page.frameRequestedNavigation" ||
        event.method === "Target.attachedToTarget"
      )) ||
      event.method === "Browser.downloadWillBegin"
    ) {
      relevantEventGeneration += 1;
    }
    if (
      guardedSessions.has(sessionId) &&
      [
        "Fetch.requestPaused",
        "Network.requestWillBeSent",
        "Network.loadingFailed",
      ].includes(event.method)
    ) {
      requestEventCount += 1;
      if (requestEventCount > maxRequestEvents) {
        warnings.add("request_resource_limit");
        connection.close("request_resource_limit");
        return;
      }
    }
    if (
      guardedSessions.has(sessionId) &&
      ["Page.frameNavigated", "Page.frameRequestedNavigation"].includes(event.method)
    ) {
      const navigationUrl = event.method === "Page.frameNavigated"
        ? event.params?.frame?.url
        : event.params?.url;
      if (!isAllowedFrameNavigationUrl(navigationUrl)) {
        recordUnsafeRequest(warnings, navigationUrl, "unsafe_navigation_blocked");
        queueEventTask(async () => {
          await connection.send("Page.stopLoading", {}, sessionId).catch(() => {
            warnings.add("stop_loading_failed");
          });
        });
      }
      return;
    }
    if (event.method === "Fetch.requestPaused" && guardedSessions.has(sessionId)) {
      queueEventTask(async () => {
        const frameId = event.params?.frameId;
        if (typeof frameId === "string" && frameId) {
          ownedFrameIds.add(frameId);
        }
        const method = String(event.params?.request?.method || "").toUpperCase();
        const isDocumentRequest = event.params?.resourceType === "Document";
        let passiveDataKey = null;
        try {
          passiveDataKey = passiveDataAccountingKey(
            sessionId,
            event.params?.networkId,
            event.params?.request?.url,
            event.params?.resourceType,
          );
          if (passiveDataKey === null) {
            const key = requestAccountingKey(
              sessionId,
              event.params?.networkId,
              event.params?.request?.url,
              {
                checkpoint: "fetch_request_paused",
                resourceType: event.params?.resourceType,
              },
            );
            if (key) {
              incrementCount(fetchRequestCounts, key);
            }
          }
        } catch (error) {
          recordRequestAccountingError(warnings, error);
          await failPausedRequest(event.params.requestId, sessionId);
          return;
        }
        if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
          warnings.add("unsafe_method_blocked");
          await failPausedRequest(event.params.requestId, sessionId);
          return;
        }
        if (cdpRequestHasBody(event.params?.request)) {
          warnings.add("request_body_blocked");
          await failPausedRequest(event.params.requestId, sessionId);
          return;
        }
        if (PASSIVE_BINARY_RESOURCE_TYPES.has(event.params?.resourceType)) {
          if (passiveDataKey !== null) {
            auditWarnings.add("unsafe_subresource_blocked");
            auditWarnings.add("unsafe_request_scheme:data");
          } else {
            try {
              await policy.validateUrl(event.params?.request?.url);
            } catch {
              recordUnsafeRequest(
                auditWarnings,
                event.params?.request?.url,
                "unsafe_subresource_blocked",
              );
            }
          }
          const blocked = await failPausedRequest(event.params.requestId, sessionId);
          if (passiveDataKey !== null && blocked) {
            incrementCount(passiveDataFetchBlocked, passiveDataKey);
          }
          return;
        }
        try {
          await policy.authorizeUrl(event.params?.request?.url);
        } catch {
          recordUnsafeRequest(
            isDocumentRequest ? warnings : auditWarnings,
            event.params?.request?.url,
            isDocumentRequest ? "unsafe_request_blocked" : "unsafe_subresource_blocked",
          );
          await failPausedRequest(event.params.requestId, sessionId);
          return;
        }
        try {
          await connection.send(
            "Fetch.continueRequest",
            {requestId: event.params.requestId},
            sessionId,
          );
        } catch {
          (captureStopping ? auditWarnings : warnings).add(
            captureStopping ? "late_request_cancelled" : "cdp_continue_failed",
          );
          await failPausedRequest(event.params.requestId, sessionId);
        }
      });
      return;
    }
    if (event.method === "Fetch.authRequired" && guardedSessions.has(sessionId)) {
      queueEventTask(async () => {
        warnings.add("authentication_blocked");
        await connection.send("Fetch.continueWithAuth", {
          requestId: event.params.requestId,
          authChallengeResponse: {response: "CancelAuth"},
        }, sessionId);
      });
      return;
    }
    if (event.method === "Network.requestWillBeSent" && guardedSessions.has(sessionId)) {
      try {
        const passiveDataKey = passiveDataAccountingKey(
          sessionId,
          event.params?.requestId,
          event.params?.request?.url,
          event.params?.type,
        );
        if (passiveDataKey !== null) {
          incrementCount(passiveDataNetworkSeen, passiveDataKey);
        } else {
          const key = requestAccountingKey(
            sessionId,
            event.params?.requestId,
            event.params?.request?.url,
            {
              checkpoint: "network_request_will_be_sent",
              resourceType: event.params?.type,
            },
          );
          if (key) {
            incrementCount(networkRequestCounts, key);
          }
          const authority = requestAuthorityKey(event.params?.request?.url);
          if (authority) {
            observedNetworkAuthorities.add(authority);
          }
        }
      } catch (error) {
        recordRequestAccountingError(warnings, error);
      }
      return;
    }
    if (event.method === "Network.loadingFailed" && guardedSessions.has(sessionId)) {
      if (
        event.params?.blockedReason === "inspector" &&
        PASSIVE_BINARY_RESOURCE_TYPES.has(event.params?.type)
      ) {
        try {
          const key = passiveDataProofKey(
            sessionId,
            event.params?.requestId,
            event.params?.type,
          );
          incrementCount(passiveDataInspectorBlocked, key);
        } catch (error) {
          recordRequestAccountingError(warnings, error);
        }
      }
      return;
    }
    if (event.method === "Network.responseReceived" && guardedSessions.has(sessionId)) {
      if (event.params?.type !== "Document") {
        return;
      }
      const frameId = event.params?.frameId;
      const status = event.params?.response?.status;
      const responseUrl = event.params?.response?.url;
      let parsed;
      try {
        parsed = new URL(responseUrl);
      } catch {
        warnings.add("invalid_document_response");
        return;
      }
      if (
        typeof frameId !== "string" ||
        frameId.length === 0 ||
        frameId.length > 512 ||
        !["http:", "https:"].includes(parsed.protocol) ||
        !Number.isSafeInteger(status) ||
        status < 100 ||
        status > 599
      ) {
        warnings.add("invalid_document_response");
        return;
      }
      if (documentResponses.length >= maxRequestEvents) {
        warnings.add("request_resource_limit");
        connection.close("request_resource_limit");
        return;
      }
      documentResponses.push({sessionId, frameId, status});
      return;
    }
    if (event.method === "Network.dataReceived" && guardedSessions.has(sessionId)) {
      accountResponseBytes({
        sessionId,
        requestId: event.params?.requestId,
        decodedDelta: event.params?.dataLength,
        encodedDelta: event.params?.encodedDataLength,
      });
      return;
    }
    if (event.method === "Network.loadingFinished" && guardedSessions.has(sessionId)) {
      accountResponseBytes({
        sessionId,
        requestId: event.params?.requestId,
        encodedTotal: event.params?.encodedDataLength,
      });
      return;
    }
    if (event.method === "Browser.downloadWillBegin") {
      const frameId = event.params?.frameId;
      if (typeof frameId === "string" && ownedFrameIds.has(frameId)) {
        queueEventTask(async () => {
          warnings.add("download_blocked");
          await connection.send("Browser.cancelDownload", {
            guid: event.params.guid,
            browserContextId,
          });
        });
      }
      return;
    }
    if (event.method === "Network.webSocketCreated" && guardedSessions.has(sessionId)) {
      warnings.add("websocket_blocked");
      return;
    }
    if (
      [
        "Network.webTransportCreated",
        "Network.directTCPSocketCreated",
        "Network.directUDPSocketCreated",
      ].includes(event.method) &&
      guardedSessions.has(sessionId)
    ) {
      warnings.add("unsupported_network_transport");
      return;
    }
    if (event.method === "Target.attachedToTarget" && guardedSessions.has(sessionId)) {
      queueEventTask(async () => {
        const childSessionId = event.params?.sessionId;
        const targetInfo = event.params?.targetInfo || {};
        if (!childSessionId || targetInfo.browserContextId !== browserContextId) {
          warnings.add("unowned_child_target");
          return;
        }
        if (targetInfo.type === "page") {
          warnings.add("popup_blocked");
          await connection.send("Target.closeTarget", {targetId: targetInfo.targetId});
          return;
        }
        if (guardedSessions.size >= maxGuardedSessions) {
          warnings.add("target_resource_limit");
          connection.close("target_resource_limit");
          return;
        }
        guardedSessions.add(childSessionId);
        await installTargetGuards(connection, childSessionId, {
          pageLike: targetInfo.type === "iframe",
        });
        await connection.send("Runtime.runIfWaitingForDebugger", {}, childSessionId);
      });
    }
  });

  try {
    await policy.authorizePlannedNavigation(sourceUrl);
    await connection.open();
    const context = await connection.send("Target.createBrowserContext", {
      disposeOnDetach: true,
      proxyServer: `http://${proxy.host}:${proxy.port}`,
      proxyBypassList: "<-loopback>",
    });
    browserContextId = requireCdpIdentity(
      context.browserContextId,
      "invalid_cdp_browser_context",
    );
    await connection.send("Browser.setDownloadBehavior", {
      behavior: "deny",
      browserContextId,
      eventsEnabled: true,
    });
    const target = await connection.send("Target.createTarget", {
      url: "about:blank",
      browserContextId,
      background: true,
    });
    targetId = requireCdpIdentity(target.targetId, "invalid_cdp_target");
    const attached = await connection.send("Target.attachToTarget", {
      targetId,
      flatten: true,
    });
    rootSessionId = requireCdpIdentity(attached.sessionId, "invalid_cdp_session");
    guardedSessions.add(rootSessionId);
    await installTargetGuards(connection, rootSessionId);
    for (let attempt = 0; attempt <= requestRetries; attempt += 1) {
      let navigation;
      try {
        navigation = await connection.send(
          "Page.navigate",
          {url: sourceUrl},
          rootSessionId,
        );
      } catch (error) {
        if (
          connection.closed &&
          [
            "cdp_fail_request_failed",
            "invalid_response_accounting",
            "response_resource_limit",
          ].includes(connection.failureReason)
        ) {
          warnings.add(connection.failureReason);
          break;
        }
        throw error;
      }
      const observedFrameId = requireCdpIdentity(
        navigation.frameId,
        "invalid_navigation_frame",
      );
      if (rootFrameId && rootFrameId !== observedFrameId) {
        warnings.add("navigation_frame_changed");
      } else {
        rootFrameId = observedFrameId;
      }
      await drainEventTasks();
      if (navigation.isDownload === true) {
        warnings.add("download_blocked");
        break;
      }
      const errorText = typeof navigation.errorText === "string"
        ? navigation.errorText
        : "";
      if (!errorText) {
        break;
      }
      let retryDelay = Math.min(
        requestRetryMs,
        Math.floor(Math.max(0, deadline - Date.now()) / 2),
      );
      if (
        warnings.size > 0 ||
        proxy.violations.length > 0 ||
        !TRANSIENT_NAVIGATION_ERRORS.has(errorText) ||
        attempt >= requestRetries ||
        retryDelay <= 0
      ) {
        warnings.add("navigation_failed");
        break;
      }
      await connection.send("Page.stopLoading", {}, rootSessionId).catch(() => {
        warnings.add("stop_loading_failed");
      });
      if (warnings.size > 0) {
        break;
      }
      retryDelay = Math.min(
        retryDelay,
        Math.floor(Math.max(0, deadline - Date.now()) / 2),
      );
      if (retryDelay <= 0) {
        warnings.add("navigation_failed");
        break;
      }
      await sleep(retryDelay);
    }

    while (Date.now() <= deadline && warnings.size === 0 && proxy.violations.length === 0) {
      let evaluated;
      try {
        evaluated = await connection.send("Runtime.evaluate", {
          expression,
          returnByValue: true,
          awaitPromise: true,
        }, rootSessionId);
      } catch (error) {
        if (connection.closed) {
          warnings.add(connection.failureReason || "raw_cdp_connection_lost");
          break;
        }
        if (
          Date.now() >= deadline &&
          String(error?.message || error).startsWith("raw_cdp_command_timeout")
        ) {
          break;
        }
        throw error;
      }
      const value = evaluated?.result?.value;
      if (typeof value === "string") {
        try {
          lastData = JSON.parse(value);
        } catch {
          warnings.add("invalid_page_capture");
          break;
        }
      }
      if (hasUsablePageData(lastData)) {
        consecutiveUsableObservations += 1;
        if (consecutiveUsableObservations >= 2) {
          break;
        }
      } else {
        consecutiveUsableObservations = 0;
      }
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      await sleep(Math.min(pollMs, remaining));
    }
    if (connection.closed) {
      warnings.add(connection.failureReason || "raw_cdp_connection_lost");
    }
    const navigationTimedOut =
      consecutiveUsableObservations < 2 &&
      warnings.size === 0 &&
      proxy.violations.length === 0 &&
      Date.now() >= deadline;
    if (!connection.closed && !navigationTimedOut) {
      captureStopping = true;
      await connection.send("Page.stopLoading", {}, rootSessionId).catch(() => {
        warnings.add("stop_loading_failed");
      });
    }
    proxy.seal();
    if (
      !navigationTimedOut &&
      !connection.closed &&
      warnings.size === 0 &&
      proxy.violations.length === 0
    ) {
      while (true) {
        await drainEventTasks();
        const observedGeneration = relevantEventGeneration;
        const observedProxyActivity =
          proxy.violations.length +
          proxy.blockedConnects.length +
          proxy.blockedAfterSeal.length;
        const remaining = deadline - Date.now();
        if (remaining < eventQuiescenceMs) {
          warnings.add("event_quiescence_timeout");
          break;
        }
        await sleep(eventQuiescenceMs);
        await drainEventTasks();
        if (
          observedGeneration === relevantEventGeneration &&
          observedProxyActivity ===
            proxy.violations.length +
              proxy.blockedConnects.length +
              proxy.blockedAfterSeal.length
        ) {
          break;
        }
        if (warnings.size > 0 || proxy.violations.length > 0) {
          break;
        }
      }
    } else {
      await drainEventTasks();
    }
    const requestKeys = new Set([
      ...networkRequestCounts.keys(),
      ...fetchRequestCounts.keys(),
    ]);
    if ([...requestKeys].some(
      (key) => networkRequestCounts.get(key) !== fetchRequestCounts.get(key),
    )) {
      warnings.add("unintercepted_network_request");
    }
    const passiveDataKeys = new Set([
      ...passiveDataNetworkSeen.keys(),
      ...passiveDataFetchBlocked.keys(),
    ]);
    if ([...passiveDataKeys].some((key) => {
      const networkSeen = passiveDataNetworkSeen.get(key) || 0;
      const fetchBlocked = passiveDataFetchBlocked.get(key) || 0;
      const inspectorBlocked = passiveDataInspectorBlocked.get(key) || 0;
      return (
        networkSeen > 1 ||
        fetchBlocked > 1 ||
        inspectorBlocked > 1 ||
        (networkSeen === 1 && fetchBlocked === 0 && inspectorBlocked === 0)
      );
    })) {
      warnings.add("unproven_passive_data_block");
    }
    for (const response of documentResponses) {
      if (
        response.sessionId === rootSessionId &&
        response.frameId === rootFrameId &&
        response.status >= 400
      ) {
        warnings.add(
          response.status === 401 || response.status === 403
            ? "http_403_unauthorized"
            : "http_error_response",
        );
      }
    }
    for (const blocked of proxy.blockedConnects) {
      const blockedKey = authorityAccountingKey(
        blocked?.protocol,
        blocked?.hostname,
        blocked?.port,
      );
      if (!blockedKey) {
        warnings.add("invalid_proxy_block_record");
      } else if (
        policy.isAuthorityAuthorized(
          blocked.protocol,
          blocked.hostname,
          blocked.port,
        )
      ) {
        auditWarnings.add("proxy_connect_race_recovered");
      } else if (observedNetworkAuthorities.has(blockedKey)) {
        warnings.add("proxy_unauthorized_connect");
      } else {
        auditWarnings.add("browser_background_connect_blocked");
      }
    }
    if (proxy.blockedAfterSeal.length > 0) {
      auditWarnings.add("proxy_activity_after_seal_blocked");
    }
    if (proxy.violations.length) {
      for (const violation of proxy.violations) {
        warnings.add(violation);
      }
      warnings.add("proxy_policy_violation");
    }
    if (connection.closed) {
      warnings.add(connection.failureReason || "raw_cdp_connection_lost");
    }
    if (warnings.size) {
      return {
        status: "unavailable",
        data: {...lastData, text: ""},
        warnings: [...warnings, ...auditWarnings],
      };
    }
    if (consecutiveUsableObservations < 2) {
      return {
        status: "navigation_timeout",
        data: lastData,
        warnings: [...auditWarnings],
      };
    }
    return {status: "captured", data: lastData, warnings: [...auditWarnings]};
  } finally {
    proxy.seal();
    await drainEventTasks();
    if (targetId && !connection.closed) {
      await connection.send("Target.closeTarget", {targetId}).catch(() => null);
    }
    if (browserContextId && !connection.closed) {
      await connection.send("Target.disposeBrowserContext", {browserContextId}).catch(() => null);
    }
    removeEventHandler();
    connection.close();
  }
}
