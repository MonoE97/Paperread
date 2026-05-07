#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const args = process.argv.slice(2);
const url = args[0];
const outputIndex = args.indexOf("--output");
const output = outputIndex >= 0 ? args[outputIndex + 1] : null;
const timeoutMs = readPositiveIntOption("--timeout-ms", 60000);
const pollMs = readPositiveIntOption("--poll-ms", 500);
const cdpBaseUrl = (process.env.ZOTERO_PAPERREAD_CDP_BASE_URL || "http://localhost:3456").replace(/\/$/, "");

if (!url || !output) {
  console.error("usage: capture-secondary-url.mjs <url> --output <path> [--timeout-ms <ms>] [--poll-ms <ms>]");
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

async function request(requestPath, options = {}) {
  const response = await fetch(`${cdpBaseUrl}${requestPath}`, options);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return await response.json();
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

  while (Date.now() <= deadline) {
    lastData = await capturePage(targetId);
    if (hasLoadedText(lastData)) {
      return { status: "captured", data: lastData };
    }
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      break;
    }
    await sleep(Math.min(pollMs, remaining));
  }

  return { status: "navigation_timeout", data: lastData };
}

function renderSecondaryContext({ sourceStatus, warning, data, capturedAt }) {
  return `# Secondary Context

- source_url: ${url}
- final_url: ${data.finalUrl}
- title: ${markdownEscape(data.title)}
- captured_at: ${capturedAt}
- capture_method: chrome_cdp
- source_status: ${sourceStatus}
${warning ? `- capture_warning: ${warning}\n` : ""}- usage_boundary: cross-check only; must not be cited in evidence_summary

## Description

${markdownEscape(data.description) || "_No description._"}

## Text

${markdownEscape(data.text) || "_No text captured._"}
`;
}

const target = await request(`/new?url=${encodeURIComponent(url)}`);
const targetId = target.targetId;

try {
  const capture = await waitForLoadedText(targetId);
  const data = capture.data;
  const capturedAt = new Date().toISOString();
  const sourceStatus = capture.status === "captured" ? "secondary_context" : "secondary_context_unavailable";
  const warning = capture.status === "captured" ? "" : capture.status;
  const body = renderSecondaryContext({ sourceStatus, warning, data, capturedAt });
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
    })
  );
  if (capture.status !== "captured") {
    process.exitCode = 1;
  }
} finally {
  await request(`/close?target=${encodeURIComponent(targetId)}`).catch(() => null);
}
