#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const args = process.argv.slice(2);
const url = args[0];
const outputIndex = args.indexOf("--output");
const output = outputIndex >= 0 ? args[outputIndex + 1] : null;

if (!url || !output) {
  console.error("usage: capture-secondary-url.mjs <url> --output <path>");
  process.exit(2);
}

async function request(requestPath, options = {}) {
  const response = await fetch(`http://localhost:3456${requestPath}`, options);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return await response.json();
}

function markdownEscape(text) {
  return String(text || "").replace(/\r\n/g, "\n").trim();
}

const target = await request(`/new?url=${encodeURIComponent(url)}`);
const targetId = target.targetId;

try {
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
      text
    });
  })()`;
  const result = await request(`/eval?target=${encodeURIComponent(targetId)}`, {
    method: "POST",
    body: expression,
  });
  const data = JSON.parse(result.value);
  const capturedAt = new Date().toISOString();
  const body = `# Secondary Context

- source_url: ${url}
- final_url: ${data.finalUrl}
- title: ${markdownEscape(data.title)}
- captured_at: ${capturedAt}
- capture_method: chrome_cdp
- source_status: secondary_context
- usage_boundary: cross-check only; must not be cited in evidence_summary

## Description

${markdownEscape(data.description) || "_No description._"}

## Text

${markdownEscape(data.text) || "_No text captured._"}
`;
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, body, "utf8");
  console.log(JSON.stringify({ output, targetId, title: data.title, textLength: data.text.length }));
} finally {
  await request(`/close?target=${encodeURIComponent(targetId)}`).catch(() => null);
}
