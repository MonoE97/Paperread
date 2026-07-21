#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { TextDecoder } from "node:util";

import {
  PublicNetworkPolicy,
  isPublicIp,
  parsePublicHttpUrl,
  resolveSystemHostname,
} from "./lib/secondary-network-policy.mjs";
import { createStrictEgressProxy } from "./lib/strict-egress-proxy.mjs";
import {
  capturePageWithRawCdp,
  discoverRawCdpWebSocket,
} from "./lib/raw-cdp-capture.mjs";

const args = process.argv.slice(2);
const strictMode = args.includes("--plan") || args.includes("--source-id");
let strictOptions = null;
if (strictMode) {
  try {
    strictOptions = parseStrictArguments(args);
  } catch (error) {
    fs.writeSync(2, `${String(error?.message || error)}\n`, null, "utf8");
    const result = JSON.stringify({
      output: null,
      sourceId: null,
      status: "error",
      finalUrl: "about:blank",
      title: "",
      textLength: 0,
      warnings: ["invalid_arguments"],
    });
    fs.writeSync(1, `${result}\n`, null, "utf8");
    process.exit(2);
  }
}
const diagnosticUrl = strictMode ? null : args[0];
const planIndex = args.indexOf("--plan");
const planPath = strictMode ? strictOptions.planPath : (planIndex >= 0 ? args[planIndex + 1] : null);
const sourceIdIndex = args.indexOf("--source-id");
const sourceId = strictMode ? strictOptions.sourceId : (sourceIdIndex >= 0 ? args[sourceIdIndex + 1] : null);
const outputIndex = args.indexOf("--output");
const output = strictMode ? strictOptions.output : (outputIndex >= 0 ? args[outputIndex + 1] : null);
const timeoutMs = strictMode ? strictOptions.timeoutMs : readPositiveIntOption("--timeout-ms", 60000);
const pollMs = strictMode ? strictOptions.pollMs : readPositiveIntOption("--poll-ms", 500);
const requestRetries = strictMode ? strictOptions.requestRetries : readPositiveIntOption("--request-retries", 2);
const requestRetryMs = strictMode ? strictOptions.requestRetryMs : readPositiveIntOption("--request-retry-ms", 500);
const publicDnsOverHttps = strictMode
  ? strictOptions.publicDnsOverHttps
  : args.includes("--public-dns-over-https");
const cdpBaseUrl = (process.env.ZOTERO_PAPER_READER_CDP_BASE_URL || "http://localhost:3456").replace(/\/$/, "");
// Use Cloudflare's public IP literal so the opt-in resolver does not depend on
// the same potentially synthetic or poisoned system DNS path it is meant to
// bypass. The source hostname answers are still independently public-checked.
const DOH_ENDPOINT = "https://1.1.1.1/dns-query";
const DOH_RESPONSE_MAX_BYTES = 64 * 1024;
const SECONDARY_PLAN_MAX_BYTES = 2 * 1024 * 1024;
const STRUCTURED_ARTIFACT_MAX_BYTES = 512 * 1024 * 1024;
const CONTRACT_IDENTIFIER_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;

if ((strictMode && (!planPath || !sourceId || !output)) || (!strictMode && (!diagnosticUrl || !output))) {
  console.error(
    "usage: capture-secondary-url.mjs <url> --output <path> OR capture-secondary-url.mjs --plan <path> --source-id <id> --output <path> [--public-dns-over-https]"
  );
  process.exit(2);
}

function parseStrictInteger(raw, { name, minimum, maximum }) {
  if (typeof raw !== "string" || !/^(0|[1-9][0-9]*)$/.test(raw)) {
    throw new Error(`${name} must be an exact decimal integer`);
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${name} must be between ${minimum} and ${maximum}`);
  }
  return value;
}

function parseStrictArguments(argv) {
  const valueOptions = new Set([
    "--plan",
    "--source-id",
    "--output",
    "--timeout-ms",
    "--poll-ms",
    "--request-retries",
    "--request-retry-ms",
  ]);
  const booleanOptions = new Set(["--public-dns-over-https"]);
  const observed = new Map();
  for (let index = 0; index < argv.length; index += 1) {
    const option = argv[index];
    if (!valueOptions.has(option) && !booleanOptions.has(option)) {
      throw new Error(`unknown strict capture option: ${option}`);
    }
    if (observed.has(option)) {
      throw new Error(`duplicate strict capture option: ${option}`);
    }
    if (booleanOptions.has(option)) {
      observed.set(option, true);
      continue;
    }
    const value = argv[index + 1];
    if (typeof value !== "string" || value.startsWith("--")) {
      throw new Error(`strict capture option requires one value: ${option}`);
    }
    observed.set(option, value);
    index += 1;
  }
  for (const required of ["--plan", "--source-id", "--output"]) {
    if (!observed.has(required) || !String(observed.get(required))) {
      throw new Error(`missing required strict capture option: ${required}`);
    }
  }
  return Object.freeze({
    planPath: observed.get("--plan"),
    sourceId: observed.get("--source-id"),
    output: observed.get("--output"),
    timeoutMs: parseStrictInteger(observed.get("--timeout-ms") ?? "60000", {
      name: "--timeout-ms",
      minimum: 1,
      maximum: 60000,
    }),
    pollMs: parseStrictInteger(observed.get("--poll-ms") ?? "500", {
      name: "--poll-ms",
      minimum: 1,
      maximum: 5000,
    }),
    requestRetries: parseStrictInteger(observed.get("--request-retries") ?? "2", {
      name: "--request-retries",
      minimum: 0,
      maximum: 2,
    }),
    requestRetryMs: parseStrictInteger(observed.get("--request-retry-ms") ?? "500", {
      name: "--request-retry-ms",
      minimum: 1,
      maximum: 5000,
    }),
    publicDnsOverHttps: observed.has("--public-dns-over-https"),
  });
}

function readPositiveIntOption(name, fallback) {
  const index = args.lastIndexOf(name);
  if (index < 0) {
    return fallback;
  }
  const raw = args[index + 1];
  const value = Number.parseInt(raw, 10);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

class CdpRequestError extends Error {
  constructor(status, statusText, bodyText) {
    super(`${status} ${statusText}`);
    this.status = status;
    this.statusText = statusText;
    this.bodyText = bodyText;
  }
}

function isRetryableStatus(status) {
  return status === 400 || status === 408 || status === 409 || status === 429 || status >= 500;
}

function warningFromError(error, prefix) {
  if (error instanceof CdpRequestError) {
    return `${prefix}:${error.status} ${error.statusText}`;
  }
  return `${prefix}:${String(error?.message || error || "unknown_error")}`;
}

function uniqueWarnings(warnings) {
  return [...new Set(warnings.filter(Boolean))];
}

function isContractIdentifier(value) {
  return typeof value === "string" && CONTRACT_IDENTIFIER_RE.test(value);
}

const recoveredRequestWarnings = [];

function recordRecoveredRequestWarnings(warnings) {
  recoveredRequestWarnings.push(
    ...warnings.map((warning) => warning.replace("cdp_request_failed:", "transient_cdp_request_recovered:"))
  );
}

function drainRecoveredRequestWarnings() {
  const warnings = [...recoveredRequestWarnings];
  recoveredRequestWarnings.length = 0;
  return warnings;
}

async function request(requestPath, options = {}, deadline = Date.now() + timeoutMs) {
  let lastError = null;
  const retryWarnings = [];
  for (let attempt = 0; attempt <= requestRetries; attempt += 1) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      throw new Error("cdp_request_timeout");
    }
    const controller = new AbortController();
    const abortTimer = setTimeout(() => controller.abort(), remaining);
    try {
      const response = await fetch(`${cdpBaseUrl}${requestPath}`, {
        ...options,
        signal: controller.signal,
      });
      if (response.ok) {
        const payload = await response.json();
        if (retryWarnings.length) {
          recordRecoveredRequestWarnings(retryWarnings);
        }
        return payload;
      }
      const bodyText = await response.text().catch(() => "");
      lastError = new CdpRequestError(response.status, response.statusText, bodyText);
      if (attempt < requestRetries && isRetryableStatus(response.status)) {
        retryWarnings.push(warningFromError(lastError, "cdp_request_failed"));
      } else {
        throw lastError;
      }
    } catch (error) {
      if (controller.signal.aborted) {
        throw new Error("cdp_request_timeout");
      }
      if (!(error instanceof CdpRequestError) || attempt >= requestRetries || !isRetryableStatus(error.status)) {
        throw error;
      }
    } finally {
      clearTimeout(abortTimer);
    }
    const retryDelay = Math.min(requestRetryMs, Math.max(0, deadline - Date.now()));
    if (retryDelay <= 0) {
      throw new Error("cdp_request_timeout");
    }
    await sleep(retryDelay);
  }
  throw lastError || new Error("cdp_request_failed");
}

function markdownEscape(text) {
  return String(text || "").replace(/\r\n/g, "\n").trim();
}

function toWellFormedUnicode(value) {
  const source = String(value || "");
  let result = "";
  for (let index = 0; index < source.length; index += 1) {
    const current = source.charCodeAt(index);
    if (current >= 0xD800 && current <= 0xDBFF) {
      const next = source.charCodeAt(index + 1);
      if (next >= 0xDC00 && next <= 0xDFFF) {
        result += source[index] + source[index + 1];
        index += 1;
      } else {
        result += "\uFFFD";
      }
    } else if (current >= 0xDC00 && current <= 0xDFFF) {
      result += "\uFFFD";
    } else {
      result += source[index];
    }
  }
  return result;
}

function normalizeVisibleText(text) {
  return toWellFormedUnicode(text)
    .replace(/[\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069]/g, "")
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F]/g, " ")
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((line) => line.trimEnd())
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function unicodeLength(text) {
  return Array.from(String(text || "")).length;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const expression = `(() => {
  const pick = (selector) => document.querySelector(selector);
  const meta = (name) => document.querySelector(\`meta[property="\${name}"], meta[name="\${name}"]\`)?.content || "";
  const boundedCodePoints = (value, limit) => Array.from(String(value || "")).slice(0, limit).join("");
  const article = pick("#js_content") || pick(".rich_media_content") || pick("article") || document.body;
  const clone = article.cloneNode(true);
  clone.querySelectorAll("script,style,noscript,iframe,svg").forEach((node) => node.remove());
  const text = boundedCodePoints(clone.innerText || "", 100001);
  return JSON.stringify({
    title: boundedCodePoints(
      pick("#activity-name")?.innerText ||
      pick(".rich_media_title")?.innerText ||
      meta("og:title") ||
      document.title,
      2000
    ),
    description: boundedCodePoints(meta("og:description") || meta("description"), 10000),
    publisher: boundedCodePoints(pick("#js_name")?.innerText || pick(".rich_media_meta_nickname")?.innerText || meta("author"), 2000),
    publishedAt: boundedCodePoints(pick("#publish_time")?.innerText || meta("article:published_time"), 500),
    finalUrl: location.href,
    readyState: document.readyState,
    text
  });
})()`;

async function capturePage(targetId, deadline) {
  const result = await request(`/eval?target=${encodeURIComponent(targetId)}`, {
    method: "POST",
    body: expression,
  }, deadline);
  return JSON.parse(result.value);
}

function hasLoadedText(data) {
  return (
    data &&
    data.finalUrl &&
    data.finalUrl !== "about:blank" &&
    ["interactive", "complete"].includes(data.readyState) &&
    markdownEscape(data.text).length > 0
  );
}

function unavailablePageWarnings(data) {
  const warnings = [];
  const finalUrl = String(data?.finalUrl || "");
  const text = markdownEscape(data?.text || "");
  if (finalUrl.startsWith("chrome-error://")) {
    warnings.push("chrome_error_page");
  }
  if (/HTTP ERROR 403/i.test(text) || /未获授权|请求遭到拒绝/.test(text)) {
    warnings.push("http_403_unauthorized");
  }
  return warnings;
}

async function waitForLoadedText(targetId, deadline = Date.now() + timeoutMs) {
  let lastData = {
    title: "",
    description: "",
    finalUrl: "about:blank",
    readyState: "",
    text: "",
  };
  const requestWarnings = drainRecoveredRequestWarnings();

  while (Date.now() <= deadline) {
    try {
      lastData = await capturePage(targetId, deadline);
      requestWarnings.push(...drainRecoveredRequestWarnings());
      const unavailableWarnings = unavailablePageWarnings(lastData);
      if (unavailableWarnings.length) {
        return {
          status: "page_unavailable",
          data: lastData,
          warnings: uniqueWarnings([...requestWarnings, ...unavailableWarnings]),
        };
      }
      if (hasLoadedText(lastData)) {
        return {
          status: "captured",
          data: lastData,
          warnings: uniqueWarnings(requestWarnings),
        };
      }
    } catch (error) {
      if (error instanceof Error && error.message === "cdp_request_timeout") {
        break;
      }
      requestWarnings.push(warningFromError(error, "cdp_request_failed"));
      requestWarnings.push(...drainRecoveredRequestWarnings());
    }
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      break;
    }
    await sleep(Math.min(pollMs, remaining));
  }

  return {
    status: requestWarnings.length ? "cdp_request_failed" : "navigation_timeout",
    data: lastData,
    warnings: uniqueWarnings(requestWarnings),
  };
}

function renderWarningLines(warnings) {
  return warnings.map((warning) => `- capture_warning: ${warning}\n`).join("");
}

function renderSecondaryContext({ sourceStatus, warnings = [], data, capturedAt }) {
  return `# Secondary Context

- source_url: ${diagnosticUrl}
- final_url: ${data.finalUrl}
- title: ${markdownEscape(data.title)}
- captured_at: ${capturedAt}
- capture_method: chrome_cdp
- source_status: ${sourceStatus}
${renderWarningLines(warnings)}- usage_boundary: cross-check only; must not be cited in evidence_summary

## Description

${markdownEscape(data.description) || "_No description._"}

## Text

${markdownEscape(data.text) || "_No text captured._"}
`;
}

function sameStat(left, right) {
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

function readStableRegularFile(requestedPath, { label, maxBytes }) {
  const absolutePath = path.resolve(requestedPath);
  if (fs.realpathSync(requestedPath) !== absolutePath) {
    throw new Error(`${label} path must not contain symlinks`);
  }
  const before = fs.lstatSync(absolutePath, { bigint: true });
  const flags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0);
  const descriptor = fs.openSync(absolutePath, flags);
  try {
    const openedBefore = fs.fstatSync(descriptor, { bigint: true });
    if (!openedBefore.isFile() || openedBefore.nlink !== 1n || openedBefore.size > BigInt(maxBytes)) {
      throw new Error(`${label} must be a bounded single-link regular file`);
    }
    if (!sameStat(before, openedBefore)) {
      throw new Error(`${label} pathname identity mismatch`);
    }
    const raw = fs.readFileSync(descriptor);
    const openedAfter = fs.fstatSync(descriptor, { bigint: true });
    const after = fs.lstatSync(absolutePath, { bigint: true });
    if (!sameStat(openedBefore, openedAfter) || !sameStat(openedBefore, after)) {
      throw new Error(`${label} changed while being read`);
    }
    return raw;
  } finally {
    fs.closeSync(descriptor);
  }
}

function readStableJson(requestedPath, { label, maxBytes }) {
  const rawBytes = readStableRegularFile(requestedPath, { label, maxBytes });
  return {
    payload: JSON.parse(rawBytes.toString("utf8")),
    rawBytes,
  };
}

function assertWellFormedJsonStrings(value) {
  const assertWellFormedString = (text) => {
    for (let index = 0; index < text.length; index += 1) {
      const codeUnit = text.charCodeAt(index);
      if (codeUnit >= 0xD800 && codeUnit <= 0xDBFF) {
        const next = text.charCodeAt(index + 1);
        if (!(next >= 0xDC00 && next <= 0xDFFF)) {
          throw new Error("secondary plan contains an unpaired surrogate");
        }
        index += 1;
      } else if (codeUnit >= 0xDC00 && codeUnit <= 0xDFFF) {
        throw new Error("secondary plan contains an unpaired surrogate");
      }
    }
  };

  if (typeof value === "string") {
    assertWellFormedString(value);
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      assertWellFormedJsonStrings(item);
    }
    return;
  }
  if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      assertWellFormedString(key);
      assertWellFormedJsonStrings(item);
    }
  }
}

function canonicalJsonValue(value) {
  if (Array.isArray(value)) {
    return value.map((item) => canonicalJsonValue(item));
  }
  if (value && typeof value === "object") {
    const canonical = Object.create(null);
    for (const key of Object.keys(value).sort()) {
      canonical[key] = canonicalJsonValue(value[key]);
    }
    return canonical;
  }
  return value;
}

function readStableCanonicalPlan(requestedPath) {
  const rawBytes = readStableRegularFile(requestedPath, {
    label: "secondary plan",
    maxBytes: SECONDARY_PLAN_MAX_BYTES,
  });
  let decoded;
  try {
    decoded = new TextDecoder("utf-8", {fatal: true}).decode(rawBytes);
  } catch {
    throw new Error("secondary plan must be valid UTF-8");
  }
  let payload;
  try {
    payload = JSON.parse(decoded);
  } catch {
    throw new Error("secondary plan must be valid JSON");
  }
  assertWellFormedJsonStrings(payload);
  const canonicalBytes = Buffer.from(
    JSON.stringify(canonicalJsonValue(payload)),
    "utf8",
  );
  if (!rawBytes.equals(canonicalBytes)) {
    throw new Error("secondary plan must use canonical JSON bytes");
  }
  return {payload, rawBytes};
}

function validateRunPlanBinding(requestedPlanPath, plan, planBytes) {
  const absolutePlanPath = path.resolve(requestedPlanPath);
  const sourceDirectory = path.dirname(absolutePlanPath);
  const runDirectory = path.dirname(sourceDirectory);
  const relativePlanPath = path.relative(runDirectory, absolutePlanPath).split(path.sep).join("/");
  if (
    path.basename(sourceDirectory) !== "source" ||
    relativePlanPath !== "source/secondary-plan.json"
  ) {
    throw new Error("secondary plan must use its run-owned source path");
  }
  const { payload: run } = readStableJson(path.join(runDirectory, "run.json"), {
    label: "run manifest",
    maxBytes: STRUCTURED_ARTIFACT_MAX_BYTES,
  });
  const source = run?.source;
  const normalizedRef = source?.normalized_source;
  const planRefs = Array.isArray(run?.artifacts)
    ? run.artifacts.filter((item) => item?.role === "secondary_source_plan")
    : [];
  if (
    run?.schema_version !== "paper_reader.run.v2" ||
    !isContractIdentifier(run?.run_id) ||
    source?.source_type !== "zotero" ||
    source?.item_key !== plan.item_key ||
    !normalizedRef ||
    normalizedRef.role !== "normalized_source" ||
    normalizedRef.path !== "source/source.json" ||
    normalizedRef.media_type !== "application/json" ||
    normalizedRef.sha256 !== plan.source_snapshot_sha256 ||
    !Number.isSafeInteger(normalizedRef.size_bytes) ||
    normalizedRef.size_bytes < 0 ||
    planRefs.length !== 1
  ) {
    throw new Error("secondary plan is not bound to one strict Zotero run");
  }
  const planRef = planRefs[0];
  if (
    planRef.path !== "source/secondary-plan.json" ||
    planRef.media_type !== "application/json" ||
    !Number.isSafeInteger(planRef.size_bytes) ||
    planRef.size_bytes !== planBytes.length ||
    planRef.sha256 !== crypto.createHash("sha256").update(planBytes).digest("hex")
  ) {
    throw new Error("secondary plan digest does not match the run manifest");
  }
  const normalizedBytes = readStableRegularFile(
    path.join(runDirectory, normalizedRef.path),
    { label: "normalized source snapshot", maxBytes: STRUCTURED_ARTIFACT_MAX_BYTES },
  );
  if (
    normalizedBytes.length !== normalizedRef.size_bytes ||
    crypto.createHash("sha256").update(normalizedBytes).digest("hex") !== normalizedRef.sha256
  ) {
    throw new Error("secondary plan source snapshot binding failed");
  }
  return Object.freeze({
    runId: run.run_id,
    itemKey: source.item_key,
    sourceSnapshotSha256: normalizedRef.sha256,
    secondaryPlanSha256: planRef.sha256,
  });
}

async function readBoundedResponseText(response, maxBytes) {
  if (!response.body) {
    return "";
  }
  const chunks = [];
  let total = 0;
  const reader = response.body.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    total += value.byteLength;
    if (total > maxBytes) {
      await reader.cancel().catch(() => null);
      throw new Error("unsafe_url");
    }
    chunks.push(Buffer.from(value));
  }
  return Buffer.concat(chunks, total).toString("utf8");
}

async function resolvePublicDnsOverHttps(hostname, deadline) {
  const query = async (recordType) => {
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      throw new Error("unsafe_url");
    }
    const endpoint = new URL(DOH_ENDPOINT);
    if (!isPublicIp(endpoint.hostname)) {
      throw new Error("unsafe_url");
    }
    endpoint.searchParams.set("name", hostname);
    endpoint.searchParams.set("type", recordType);
    const response = await fetch(endpoint, {
      headers: { accept: "application/dns-json" },
      redirect: "error",
      signal: AbortSignal.timeout(Math.max(1, Math.min(remaining, 10000))),
    });
    if (!response.ok) {
      throw new Error("unsafe_url");
    }
    let payload;
    try {
      payload = JSON.parse(await readBoundedResponseText(response, DOH_RESPONSE_MAX_BYTES));
    } catch {
      throw new Error("unsafe_url");
    }
    if (
      !payload ||
      typeof payload !== "object" ||
      Array.isArray(payload) ||
      payload.Status !== 0 ||
      (payload.Answer !== undefined && !Array.isArray(payload.Answer)) ||
      (Array.isArray(payload.Answer) && payload.Answer.length > 64)
    ) {
      throw new Error("unsafe_url");
    }
    return (payload.Answer || [])
      .filter((answer) => answer && (answer.type === 1 || answer.type === 28))
      .map((answer) => answer.data);
  };
  const addresses = (await Promise.all([query("A"), query("AAAA")])).flat();
  if (
    !addresses.length ||
    addresses.some((address) => typeof address !== "string" || !isPublicIp(address))
  ) {
    throw new Error("unsafe_url");
  }
  return addresses;
}

async function resolveNetworkPolicyHostname(hostname, deadline) {
  if (publicDnsOverHttps) {
    return resolvePublicDnsOverHttps(hostname, deadline);
  }
  return resolveSystemHostname(hostname, {deadline});
}

function validatePlanAndSelectSource(plan, requestedSourceId) {
  const hasExactKeys = (value, expected) => {
    const observed = Object.keys(value).sort();
    const wanted = [...expected].sort();
    return observed.length === wanted.length && observed.every((key, index) => key === wanted[index]);
  };
  const legacyPlanKeys = [
    "format",
    "item_key",
    "source_snapshot_sha256",
    "usage_boundary",
    "eligible_source_count",
    "sources",
    "warnings",
  ];
  const anchoredPlanKeys = [...legacyPlanKeys, "finding_anchor_policy"];
  const sourceKeys = [
    "source_id",
    "url",
    "source_field",
    "source_provenance",
    "eligibility",
    "rejection_reason",
  ];
  if (
    !plan ||
    typeof plan !== "object" ||
    Array.isArray(plan) ||
    !(
      hasExactKeys(plan, legacyPlanKeys) ||
      (
        hasExactKeys(plan, anchoredPlanKeys) &&
        plan.finding_anchor_policy === "codepoint_sha256_v1"
      )
    ) ||
    plan.format !== "paper_reader.secondary-plan.v2-internal" ||
    !isContractIdentifier(plan.item_key) ||
    !/^[0-9a-f]{64}$/.test(plan.source_snapshot_sha256) ||
    plan.usage_boundary !== "cross-check only; must not be cited in evidence_summary" ||
    !Number.isSafeInteger(plan.eligible_source_count) ||
    plan.eligible_source_count < 0 ||
    !Array.isArray(plan.sources) ||
    plan.sources.length > 256 ||
    !Array.isArray(plan.warnings) ||
    plan.warnings.some((warning) => typeof warning !== "string")
  ) {
    throw new Error("invalid secondary plan");
  }
  const seenSourceIds = new Set();
  const seenUrls = new Set();
  let eligibleCount = 0;
  for (const [index, source] of plan.sources.entries()) {
    const expectedSourceId = `secondary-${String(index + 1).padStart(3, "0")}`;
    if (
      !source ||
      typeof source !== "object" ||
      Array.isArray(source) ||
      !hasExactKeys(source, sourceKeys) ||
      source.source_id !== expectedSourceId ||
      seenSourceIds.has(source.source_id) ||
      typeof source.url !== "string" ||
      seenUrls.has(source.url) ||
      source.source_field !== "extra" ||
      typeof source.source_provenance !== "string" ||
      !source.source_provenance ||
      !["eligible", "rejected"].includes(source.eligibility) ||
      (source.eligibility === "eligible" && source.rejection_reason !== null) ||
      (source.eligibility === "rejected" && !["primary_source", "unsafe_url", "source_limit"].includes(source.rejection_reason))
    ) {
      throw new Error("invalid secondary plan source");
    }
    seenSourceIds.add(source.source_id);
    seenUrls.add(source.url);
    let unsafeUrl = false;
    try {
      parsePublicHttpUrl(source.url);
    } catch {
      unsafeUrl = true;
    }
    if (source.eligibility === "eligible") {
      if (unsafeUrl || eligibleCount >= 8) {
        throw new Error("invalid secondary plan source classification");
      }
      eligibleCount += 1;
    } else {
      if (unsafeUrl !== (source.rejection_reason === "unsafe_url")) {
        throw new Error("invalid secondary plan source classification");
      }
      if (source.rejection_reason === "source_limit" && eligibleCount < 8) {
        throw new Error("invalid secondary plan source limit");
      }
    }
  }
  if (eligibleCount !== plan.eligible_source_count || eligibleCount > 8) {
    throw new Error("invalid secondary plan eligible source count");
  }
  const matches = plan.sources.filter((item) => item && item.source_id === requestedSourceId);
  if (
    matches.length !== 1 ||
    matches[0].eligibility !== "eligible" ||
    matches[0].rejection_reason !== null ||
    typeof matches[0].url !== "string"
  ) {
    throw new Error("source-id is not one eligible plan source");
  }
  return matches[0];
}

function writeNoReplaceJson(destination, payload) {
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  const bytes = `${JSON.stringify(payload)}\n`;
  if (Buffer.byteLength(bytes, "utf8") > 1024 * 1024) {
    throw new Error("capture JSON exceeds 1 MiB");
  }
  fs.writeFileSync(destination, bytes, { encoding: "utf8", flag: "wx", mode: 0o600 });
}

function emitStrictMachineResult({
  status,
  finalUrl = "about:blank",
  title = "",
  textLength = 0,
  warnings = [],
} = {}) {
  console.log(JSON.stringify({
    output: typeof output === "string" ? output : null,
    sourceId: typeof sourceId === "string" ? sourceId : null,
    status,
    finalUrl,
    title,
    textLength,
    warnings: uniqueWarnings(warnings),
  }));
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

async function buildStrictCapture({ source, binding, capture, capturedAt, policy }) {
  const data = capture.data || blankPageData();
  const warnings = [...capture.warnings];
  if (
    capture.status !== "captured" &&
    capture.status !== "unavailable" &&
    !warnings.includes(capture.status)
  ) {
    warnings.push(capture.status);
  }
  const normalizedText = normalizeVisibleText(data.text);
  const normalizedTitle = normalizeVisibleText(data.title);
  const normalizedTextLength = unicodeLength(normalizedText);
  let status = capture.status === "captured" ? "captured" : "unavailable";
  if (status === "captured") {
    if (!policy.isUrlAuthorized(data.finalUrl)) {
      status = "unavailable";
      warnings.push("unsafe_final_url");
    }
  }
  if (status === "captured" && normalizedTextLength < 200) {
    status = "unavailable";
    warnings.push("insufficient_text");
  }
  if (status === "captured" && normalizedTextLength > 100_000) {
    status = "unavailable";
    warnings.push("text_resource_limit");
  }
  if (!normalizedTitle && normalizedTextLength >= 200) {
    status = "unavailable";
    warnings.push("missing_title");
  }
  const storedText = status === "captured" ? normalizedText : "";
  return {
    format: "paper_reader.secondary-capture.v2-internal",
    run_id: binding.runId,
    item_key: binding.itemKey,
    source_snapshot_sha256: binding.sourceSnapshotSha256,
    secondary_plan_sha256: binding.secondaryPlanSha256,
    source_id: source.source_id,
    requested_url: source.url,
    final_url: String(data.finalUrl || "about:blank"),
    captured_at: capturedAt,
    capture_method: "chrome_cdp",
    status,
    title: normalizedTitle,
    publisher: normalizeVisibleText(data.publisher),
    published_at: normalizeVisibleText(data.publishedAt),
    description: normalizeVisibleText(data.description),
    text: storedText,
    text_sha256: crypto.createHash("sha256").update(storedText, "utf8").digest("hex"),
    text_length: unicodeLength(storedText),
    warnings: uniqueWarnings(warnings),
  };
}

async function runStrictCapture() {
  const captureDeadline = Date.now() + timeoutMs;
  let source;
  let binding;
  let policy;
  try {
    if (fs.existsSync(output)) {
      throw new Error("capture output already exists");
    }
    const loadedPlan = readStableCanonicalPlan(planPath);
    source = validatePlanAndSelectSource(loadedPlan.payload, sourceId);
    binding = validateRunPlanBinding(planPath, loadedPlan.payload, loadedPlan.rawBytes);
    policy = new PublicNetworkPolicy({
      resolver: (hostname) => resolveNetworkPolicyHostname(hostname, captureDeadline),
    });
    await policy.validateUrl(source.url);
  } catch (error) {
    console.error(String(error?.message || error));
    emitStrictMachineResult({status: "error", warnings: ["capture_setup_failed"]});
    process.exitCode = 2;
    return;
  }

  let proxy = null;
  try {
    let capture;
    try {
      const wsEndpoint = await discoverRawCdpWebSocket({deadline: captureDeadline});
      proxy = await createStrictEgressProxy({
        policy,
        connectTimeoutMs: Math.min(timeoutMs, 10_000),
      });
      capture = await capturePageWithRawCdp({
        wsEndpoint,
        sourceUrl: source.url,
        policy,
        proxy,
        expression,
        deadline: captureDeadline,
        pollMs,
        requestRetries,
        requestRetryMs,
      });
    } catch (error) {
      capture = {
        status: "cdp_request_failed",
        data: blankPageData(),
        warnings: [warningFromError(error, "cdp_request_failed")],
      };
    }
    const artifact = await buildStrictCapture({
      source,
      binding,
      capture,
      capturedAt: new Date().toISOString(),
      policy,
    });
    writeNoReplaceJson(output, artifact);
    emitStrictMachineResult({
      status: artifact.status,
      finalUrl: artifact.final_url,
      title: artifact.title,
      textLength: artifact.text_length,
      warnings: artifact.warnings,
    });
  } catch (error) {
    console.error(String(error?.message || error));
    emitStrictMachineResult({status: "error", warnings: ["capture_setup_failed"]});
    process.exitCode = 2;
  } finally {
    if (proxy) {
      await proxy.close().catch(() => null);
    }
  }
}

async function runDiagnosticCapture() {
  let targetId = "";
  const captureDeadline = Date.now() + timeoutMs;
  try {
    const target = await request(
      `/new?url=${encodeURIComponent(diagnosticUrl)}`,
      {},
      captureDeadline,
    );
    targetId = target.targetId;
    const capture = await waitForLoadedText(targetId, captureDeadline);
    const data = capture.data;
    const capturedAt = new Date().toISOString();
    const sourceStatus = capture.status === "captured" ? "secondary_context" : "secondary_context_unavailable";
    const warnings = capture.status === "captured" ? capture.warnings : (capture.warnings.length ? capture.warnings : [capture.status]);
    const body = renderSecondaryContext({ sourceStatus, warnings, data, capturedAt });
    fs.mkdirSync(path.dirname(output), { recursive: true });
    fs.writeFileSync(output, body, "utf8");
    console.log(
      JSON.stringify({
        output,
        targetId,
        status: capture.status,
        finalUrl: data.finalUrl,
        title: data.title,
        textLength: unicodeLength(data.text),
        warnings,
      })
    );
    if (capture.status !== "captured") {
      process.exitCode = 1;
    }
  } catch (error) {
    const data = blankPageData();
    const warnings = [warningFromError(error, "cdp_request_failed")];
    const body = renderSecondaryContext({
      sourceStatus: "secondary_context_unavailable",
      warnings,
      data,
      capturedAt: new Date().toISOString(),
    });
    fs.mkdirSync(path.dirname(output), { recursive: true });
    fs.writeFileSync(output, body, "utf8");
    console.log(
      JSON.stringify({
        output,
        targetId,
        status: "cdp_request_failed",
        finalUrl: data.finalUrl,
        title: data.title,
        textLength: 0,
        warnings,
      })
    );
    process.exitCode = 1;
  } finally {
    if (targetId) {
      await request(
        `/close?target=${encodeURIComponent(targetId)}`,
        {},
        Date.now() + Math.min(timeoutMs, 5000),
      ).catch(() => null);
    }
  }
}

if (strictMode) {
  await runStrictCapture();
} else {
  await runDiagnosticCapture();
}
