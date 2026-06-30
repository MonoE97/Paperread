#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const args = process.argv.slice(2);
const url = args[0];
const outputIndex = args.indexOf("--output");
const output = outputIndex >= 0 ? args[outputIndex + 1] : null;
const timeoutMs = readPositiveIntOption("--timeout-ms", 60000);
const pollMs = readPositiveIntOption("--poll-ms", 500);
const requestRetries = readPositiveIntOption("--request-retries", 2);
const requestRetryMs = readPositiveIntOption("--request-retry-ms", 500);
const cdpBaseUrl = (process.env.ZOTERO_PAPERREAD_CDP_BASE_URL || "http://localhost:3456").replace(/\/$/, "");

if (!url || !output) {
  console.error(
    "usage: capture-secondary-url.mjs <url> --output <path> [--timeout-ms <ms>] [--poll-ms <ms>] [--request-retries <n>] [--request-retry-ms <ms>]"
  );
  process.exit(2);
}

function readPositiveIntOption(name, fallback) {
  const index = args.indexOf(name);
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

async function request(requestPath, options = {}) {
  let lastError = null;
  const retryWarnings = [];
  for (let attempt = 0; attempt <= requestRetries; attempt += 1) {
    const response = await fetch(`${cdpBaseUrl}${requestPath}`, options);
    if (response.ok) {
      if (retryWarnings.length) {
        recordRecoveredRequestWarnings(retryWarnings);
      }
      return await response.json();
    }
    const bodyText = await response.text().catch(() => "");
    lastError = new CdpRequestError(response.status, response.statusText, bodyText);
    if (attempt < requestRetries && isRetryableStatus(response.status)) {
      retryWarnings.push(warningFromError(lastError, "cdp_request_failed"));
      await sleep(requestRetryMs);
      continue;
    }
    throw lastError;
  }
  throw lastError || new Error("cdp_request_failed");
}

function markdownEscape(text) {
  return String(text || "").replace(/\r\n/g, "\n").trim();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const expression = `(() => {
  const pick = (selector) => document.querySelector(selector);
  const meta = (name) => document.querySelector(\`meta[property="\${name}"], meta[name="\${name}"]\`)?.content || "";
  const article = pick("#js_content") || pick(".rich_media_content") || pick("article") || document.body;
  const clone = article.cloneNode(true);
  clone.querySelectorAll("script,style,noscript,iframe,svg").forEach((node) => node.remove());
  const text = clone.innerText || "";
  return JSON.stringify({
    title: document.title || meta("og:title"),
    description: meta("og:description") || meta("description"),
    finalUrl: location.href,
    readyState: document.readyState,
    text
  });
})()`;

async function capturePage(targetId) {
  const result = await request(`/eval?target=${encodeURIComponent(targetId)}`, {
    method: "POST",
    body: expression,
  });
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

async function waitForLoadedText(targetId) {
  const deadline = Date.now() + timeoutMs;
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
      lastData = await capturePage(targetId);
      requestWarnings.push(...drainRecoveredRequestWarnings());
      if (hasLoadedText(lastData)) {
        return {
          status: "captured",
          data: lastData,
          warnings: uniqueWarnings(requestWarnings),
        };
      }
    } catch (error) {
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

- source_url: ${url}
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

let targetId = "";
try {
  const target = await request(`/new?url=${encodeURIComponent(url)}`);
  targetId = target.targetId;
  const capture = await waitForLoadedText(targetId);
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
      textLength: data.text.length,
      warnings,
    })
  );
  if (capture.status !== "captured") {
    process.exitCode = 1;
  }
} catch (error) {
  const data = { title: "", description: "", finalUrl: "about:blank", readyState: "", text: "" };
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
    await request(`/close?target=${encodeURIComponent(targetId)}`).catch(() => null);
  }
}
