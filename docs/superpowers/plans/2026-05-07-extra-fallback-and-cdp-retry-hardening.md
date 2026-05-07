# Extra Fallback and CDP Retry Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Zotero `Extra` fallback feel like a normal first-class path and make WeChat/secondary-source CDP capture resilient to transient HTTP 400/eval failures.

**Architecture:** Keep MCP-first item details unchanged. When MCP omits `extra`, `save-item-details` still uses read-only Zotero SQLite, but successful immutable reads become provenance diagnostics instead of user-facing workflow warnings. Secondary URL capture remains in the existing Node CDP script, with bounded retries and deterministic unavailable-output files instead of raw stack traces.

**Tech Stack:** Python 3 via `uv`, stdlib `sqlite3`, pytest, Node.js CDP capture script, existing `zotero-paperread` CLI.

---

## File Structure

- Modify `src/zotero_paperread/zotero_sqlite.py`: add short read-only retry before immutable fallback; move successful immutable use into provenance diagnostics.
- Modify `src/zotero_paperread/zotero_item_io.py`: propagate fallback diagnostics into `_paperread.enrichment.extra`, keep `_paperread.warnings` for actual failure/missing states only.
- Modify `src/zotero_paperread/secondary_sources.py`: ensure `secondary_sources.json.warnings` only includes actionable warnings, not successful immutable fallback diagnostics.
- Modify `skills/zotero-paper-summary/scripts/capture-secondary-url.mjs`: add retry options and graceful unavailable output for persistent CDP request failures.
- Modify tests:
  - `tests/test_zotero_sqlite.py`
  - `tests/test_zotero_item_io.py`
  - `tests/test_secondary_sources.py`
  - `tests/test_capture_secondary_url.py`
- Modify docs:
  - `README.md`
  - `skills/zotero-paper-summary/SKILL.md`
  - `tests/test_default_workflow_docs.py`

---

### Task 1: Make Successful SQLite Immutable Fallback Non-Noisy

**Files:**
- Modify: `src/zotero_paperread/zotero_sqlite.py`
- Modify: `src/zotero_paperread/zotero_item_io.py`
- Test: `tests/test_zotero_sqlite.py`
- Test: `tests/test_zotero_item_io.py`

- [ ] **Step 1: Write failing tests for retry and diagnostics**

Add to `tests/test_zotero_sqlite.py`:

```python
def test_lookup_extra_retries_read_only_before_immutable(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "zotero.sqlite"
    make_zotero_db(db_path)
    calls: list[bool] = []

    def fake_lookup(item_key: str, sqlite_path: Path, *, immutable: bool) -> tuple[str, bool]:
        calls.append(immutable)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return "https://mp.weixin.qq.com/s/retry-success", True

    monkeypatch.setattr("zotero_paperread.zotero_sqlite._lookup_with_mode", fake_lookup)

    result = lookup_extra_by_item_key(
        "ABC123",
        sqlite_path=db_path,
        ro_retries=1,
        retry_sleep_seconds=0,
    )

    assert result["extra"] == "https://mp.weixin.qq.com/s/retry-success"
    assert result["warnings"] == []
    assert result["provenance"]["sqlite_mode"] == "ro"
    assert result["provenance"]["diagnostics"] == ["sqlite_ro_retry_after_locked"]
    assert calls == [False, False]
```

Add to `tests/test_zotero_sqlite.py`:

```python
def test_lookup_extra_records_successful_immutable_as_diagnostic(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "zotero.sqlite"
    make_zotero_db(db_path)
    calls: list[bool] = []

    def fake_lookup(item_key: str, sqlite_path: Path, *, immutable: bool) -> tuple[str, bool]:
        calls.append(immutable)
        if not immutable:
            raise sqlite3.OperationalError("database is locked")
        return "https://mp.weixin.qq.com/s/immutable-success", True

    monkeypatch.setattr("zotero_paperread.zotero_sqlite._lookup_with_mode", fake_lookup)

    result = lookup_extra_by_item_key(
        "ABC123",
        sqlite_path=db_path,
        ro_retries=0,
        retry_sleep_seconds=0,
    )

    assert result["extra"] == "https://mp.weixin.qq.com/s/immutable-success"
    assert result["warnings"] == []
    assert result["provenance"]["sqlite_mode"] == "immutable"
    assert result["provenance"]["diagnostics"] == ["sqlite_immutable_snapshot_used"]
    assert calls == [False, True]
```

Update `tests/test_zotero_item_io.py::test_write_item_details_files_enriches_missing_extra_from_sqlite` so the fake lookup returns diagnostics in provenance and no warnings:

```python
def fake_lookup(item_key: str, **kwargs):
    assert item_key == "ABC123"
    return {
        "extra": "https://mp.weixin.qq.com/s/example",
        "warnings": [],
        "provenance": {
            "source": "zotero_sqlite",
            "item_key": "ABC123",
            "sqlite_mode": "immutable",
            "diagnostics": ["sqlite_immutable_snapshot_used"],
        },
    }
```

Then assert:

```python
assert normalized["_paperread"]["warnings"] == []
assert normalized["_paperread"]["enrichment"]["extra"]["diagnostics"] == ["sqlite_immutable_snapshot_used"]
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```bash
uv run pytest tests/test_zotero_sqlite.py tests/test_zotero_item_io.py -q
```

Expected: fails because `lookup_extra_by_item_key()` does not accept `ro_retries` / `retry_sleep_seconds`, and successful immutable fallback currently appears in `warnings`.

- [ ] **Step 3: Implement retry and diagnostics in SQLite lookup**

In `src/zotero_paperread/zotero_sqlite.py`:

```python
import time
```

Change the signature:

```python
def lookup_extra_by_item_key(
    item_key: str,
    *,
    sqlite_path: Path = DEFAULT_ZOTERO_SQLITE_PATH,
    allow_immutable: bool = True,
    ro_retries: int = 2,
    retry_sleep_seconds: float = 0.05,
) -> dict[str, Any]:
```

Replace the current locked-error block with this behavior:

```python
warnings: list[str] = []
diagnostics: list[str] = []
mode = "ro"
attempt = 0
while True:
    try:
        extra, exists = _lookup_with_mode(key, db_path, immutable=False)
        if attempt > 0:
            diagnostics.append("sqlite_ro_retry_after_locked")
        break
    except sqlite3.OperationalError as error:
        if not _is_locked_error(error):
            return {"extra": "", "warnings": ["sqlite_extra_unavailable"], "provenance": {}}
        if attempt < max(0, ro_retries):
            attempt += 1
            if retry_sleep_seconds > 0:
                time.sleep(retry_sleep_seconds)
            continue
        if not allow_immutable:
            return {"extra": "", "warnings": ["sqlite_extra_unavailable"], "provenance": {}}
        mode = "immutable"
        diagnostics.append("sqlite_immutable_snapshot_used")
        try:
            extra, exists = _lookup_with_mode(key, db_path, immutable=True)
            break
        except sqlite3.Error:
            return {"extra": "", "warnings": ["sqlite_extra_unavailable"], "provenance": {}}
    except sqlite3.Error:
        return {"extra": "", "warnings": ["sqlite_extra_unavailable"], "provenance": {}}
```

Return provenance with diagnostics:

```python
"provenance": {
    "source": "zotero_sqlite",
    "item_key": key,
    "sqlite_path": str(db_path),
    "sqlite_mode": mode,
    "diagnostics": diagnostics,
},
```

Keep actual failure warnings unchanged:

```python
if not exists:
    return {"extra": "", "warnings": warnings + ["sqlite_extra_item_not_found"], "provenance": {}}
```

- [ ] **Step 4: Ensure item IO stores diagnostics without warning noise**

In `src/zotero_paperread/zotero_item_io.py`, keep warning propagation exactly as-is for `lookup["warnings"]`, but because successful immutable fallback now returns no warning, `_paperread.warnings` stays empty. No extra code path is needed beyond tests unless current implementation strips provenance diagnostics.

If diagnostics are accidentally stripped, replace provenance assignment with:

```python
provenance = dict(lookup.get("provenance", {}))
meta.setdefault("enrichment", {})["extra"] = provenance
```

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run:

```bash
uv run pytest tests/test_zotero_sqlite.py tests/test_zotero_item_io.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/zotero_paperread/zotero_sqlite.py src/zotero_paperread/zotero_item_io.py tests/test_zotero_sqlite.py tests/test_zotero_item_io.py
git commit -m "fix: make Zotero extra fallback diagnostics non-noisy"
```

---

### Task 2: Keep Secondary Source Warnings Actionable

**Files:**
- Modify: `src/zotero_paperread/secondary_sources.py`
- Test: `tests/test_secondary_sources.py`

- [ ] **Step 1: Update the existing warning test and add a regression test**

Replace `tests/test_secondary_sources.py::test_build_secondary_sources_records_cross_check_boundary` with:

```python
def test_build_secondary_sources_records_cross_check_boundary() -> None:
    details = {
        "key": "ABC123",
        "title": "Example Paper",
        "extra": "Related: https://mp.weixin.qq.com/s/example?scene=334",
        "_paperread": {
            "enrichment": {
                "extra": {
                    "source": "zotero_sqlite",
                    "sqlite_mode": "immutable",
                    "diagnostics": ["sqlite_immutable_snapshot_used"],
                }
            },
            "warnings": [],
        },
    }

    payload = build_secondary_sources(details)

    assert payload["item_key"] == "ABC123"
    assert payload["usage_boundary"] == "cross-check only; must not be cited in evidence_summary"
    assert payload["warnings"] == []
    assert payload["sources"] == [
        {
            "source_id": "secondary-001",
            "url": "https://mp.weixin.qq.com/s/example?scene=334",
            "source_field": "extra",
            "source_provenance": "zotero_sqlite",
            "capture_status": "pending_capture",
        }
    ]
```

Add a regression test for legacy normalized files that still contain the old noisy warning:

```python
def test_build_secondary_sources_does_not_promote_successful_sqlite_diagnostics_to_warnings() -> None:
    payload = build_secondary_sources(
        {
            "key": "ABC123",
            "title": "Example",
            "extra": "https://mp.weixin.qq.com/s/example",
            "_paperread": {
                "warnings": ["sqlite_immutable_snapshot_used"],
                "enrichment": {
                    "extra": {
                        "source": "zotero_sqlite",
                        "sqlite_mode": "immutable",
                        "diagnostics": ["sqlite_immutable_snapshot_used"],
                    }
                },
            },
        }
    )

    assert payload["sources"][0]["source_provenance"] == "zotero_sqlite"
    assert payload["warnings"] == []
```

- [ ] **Step 2: Run test and confirm RED if diagnostics leak**

Run:

```bash
uv run pytest tests/test_secondary_sources.py -q
```

Expected: fails because `secondary_sources.py` currently copies `sqlite_immutable_snapshot_used` from `_paperread.warnings`.

- [ ] **Step 3: Filter non-actionable SQLite diagnostics**

In `src/zotero_paperread/secondary_sources.py`, add:

```python
NON_ACTIONABLE_WARNING_CODES = {"sqlite_immutable_snapshot_used", "sqlite_ro_retry_after_locked"}
```

Then change `_paperread_warnings()` to filter successful fallback diagnostics:

```python
cleaned: list[str] = []
for item in warnings:
    warning = str(item).strip()
    if not warning or warning in NON_ACTIONABLE_WARNING_CODES:
        continue
    cleaned.append(warning)
return cleaned
```

Do not copy `paperread["enrichment"]["extra"]["diagnostics"]` into `secondary_sources.json.warnings`; diagnostics stay under `item-details.json._paperread.enrichment.extra.diagnostics`.

- [ ] **Step 4: Run focused tests**

```bash
uv run pytest tests/test_secondary_sources.py tests/test_zotero_sqlite.py tests/test_zotero_item_io.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/zotero_paperread/secondary_sources.py tests/test_secondary_sources.py
git commit -m "fix: keep secondary source warnings actionable"
```

---

### Task 3: Add CDP Request Retry And Graceful Failure Output

**Files:**
- Modify: `skills/zotero-paper-summary/scripts/capture-secondary-url.mjs`
- Test: `tests/test_capture_secondary_url.py`

- [ ] **Step 1: Extend mock CDP server tests**

In `tests/test_capture_secondary_url.py`, update `MockCdpHandler.do_POST()` before the timeout/delayed data block:

```python
if self.server.mode == "transient_eval_400" and self.server.eval_count == 1:
    self._send_json({"error": "transient bad request"}, status=400)
    return
if self.server.mode == "persistent_eval_400":
    self._send_json({"error": "persistent bad request"}, status=400)
    return
```

Add test:

```python
def test_capture_secondary_url_recovers_from_transient_eval_400(tmp_path: Path) -> None:
    server, base_url = run_mock_cdp(mode="transient_eval_400")
    try:
        result = run_capture(
            tmp_path,
            base_url,
            "--timeout-ms",
            "2000",
            "--poll-ms",
            "10",
            "--request-retries",
            "1",
            "--request-retry-ms",
            "1",
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, result.stderr
    captured = (tmp_path / "secondary_context.md").read_text(encoding="utf-8")
    assert "source_status: secondary_context" in captured
    assert "capture_warning: transient_cdp_request_recovered:400 Bad Request" in captured
    assert "正文已经加载出来" in captured
```

Add test:

```python
def test_capture_secondary_url_writes_unavailable_file_for_persistent_eval_400(tmp_path: Path) -> None:
    server, base_url = run_mock_cdp(mode="persistent_eval_400")
    try:
        result = run_capture(
            tmp_path,
            base_url,
            "--timeout-ms",
            "80",
            "--poll-ms",
            "10",
            "--request-retries",
            "1",
            "--request-retry-ms",
            "1",
        )
    finally:
        server.shutdown()

    assert result.returncode == 1
    assert "Error:" not in result.stderr
    captured = (tmp_path / "secondary_context.md").read_text(encoding="utf-8")
    assert "source_status: secondary_context_unavailable" in captured
    assert "capture_warning: cdp_request_failed:400 Bad Request" in captured
```

- [ ] **Step 2: Run test and confirm RED**

```bash
uv run pytest tests/test_capture_secondary_url.py -q
```

Expected: fails because the script does not support `--request-retries` and throws raw errors on 400.

- [ ] **Step 3: Add retry options and CDP error type**

In `skills/zotero-paper-summary/scripts/capture-secondary-url.mjs`, add:

```javascript
const requestRetries = readPositiveIntOption("--request-retries", 2);
const requestRetryMs = readPositiveIntOption("--request-retry-ms", 500);

class CdpRequestError extends Error {
  constructor(status, statusText, bodyText) {
    super(`${status} ${statusText}`);
    this.status = status;
    this.statusText = statusText;
    this.bodyText = bodyText;
  }
}
```

Add the retryable-status helper. The full `request()` replacement comes in Step 4 so recovered retry diagnostics can be wired at the same time:

```javascript
function isRetryableStatus(status) {
  return status === 400 || status === 408 || status === 409 || status === 429 || status >= 500;
}
```

Add helpers:

```javascript
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
```

- [ ] **Step 4: Catch transient eval failures inside polling**

Change `request()` so internally recovered retries are visible to the caller:

```javascript
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
```

Then change `waitForLoadedText()` so eval failures continue until timeout and recovered retry diagnostics are returned on success:

```javascript
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
```

- [ ] **Step 5: Render multiple warnings and avoid raw stack traces**

Change `renderSecondaryContext()` to accept a `warnings` array:

```javascript
function renderWarningLines(warnings) {
  return warnings.map((warning) => `- capture_warning: ${warning}\n`).join("");
}
```

Then use:

```javascript
${renderWarningLines(warnings)}- usage_boundary: cross-check only; must not be cited in evidence_summary
```

Wrap the main open/capture flow:

```javascript
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
  console.log(JSON.stringify({ output, targetId, status: capture.status, finalUrl: data.finalUrl, title: data.title, textLength: data.text.length, warnings }));
  if (capture.status !== "captured") {
    process.exitCode = 1;
  }
} catch (error) {
  const data = { title: "", description: "", finalUrl: "about:blank", readyState: "", text: "" };
  const body = renderSecondaryContext({
    sourceStatus: "secondary_context_unavailable",
    warnings: [warningFromError(error, "cdp_request_failed")],
    data,
    capturedAt: new Date().toISOString(),
  });
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, body, "utf8");
  console.log(JSON.stringify({ output, targetId, status: "cdp_request_failed", finalUrl: data.finalUrl, title: data.title, textLength: 0, warnings: [warningFromError(error, "cdp_request_failed")] }));
  process.exitCode = 1;
} finally {
  if (targetId) {
    await request(`/close?target=${encodeURIComponent(targetId)}`).catch(() => null);
  }
}
```

- [ ] **Step 6: Run focused tests**

```bash
uv run pytest tests/test_capture_secondary_url.py -q
```

Expected: all capture tests pass.

- [ ] **Step 7: Commit**

```bash
git add skills/zotero-paper-summary/scripts/capture-secondary-url.mjs tests/test_capture_secondary_url.py
git commit -m "fix: retry transient CDP capture failures"
```

---

### Task 4: Update Workflow Documentation

**Files:**
- Modify: `README.md`
- Modify: `skills/zotero-paper-summary/SKILL.md`
- Modify: `tests/test_default_workflow_docs.py`

- [ ] **Step 1: Write docs-lock tests**

Add assertions to `tests/test_default_workflow_docs.py`:

```python
def test_docs_describe_sqlite_extra_diagnostics_not_warning_noise() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    skill = Path("skills/zotero-paper-summary/SKILL.md").read_text(encoding="utf-8")

    for text in (readme, skill):
        assert "successful immutable SQLite Extra reads are recorded as provenance diagnostics" in text
        assert "actual missing or unreadable Extra fallback remains a warning" in text
```

Add:

```python
def test_docs_describe_secondary_capture_retry_behavior() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    skill = Path("skills/zotero-paper-summary/SKILL.md").read_text(encoding="utf-8")

    for text in (readme, skill):
        assert "--request-retries" in text
        assert "transient CDP request failures are retried" in text
        assert "persistent CDP failures write secondary_context_unavailable" in text
```

- [ ] **Step 2: Run docs tests and confirm RED**

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected: fails until docs are updated.

- [ ] **Step 3: Update README**

In `README.md`, update the Secondary Context / Trusted Notes sections with this wording:

````markdown
Successful immutable SQLite Extra reads are recorded as provenance diagnostics under `_paperread.enrichment.extra.diagnostics`; they are not treated as extraction warnings because the Extra value was recovered. Actual missing or unreadable Extra fallback remains a warning.

The CDP secondary capture script retries transient CDP request failures:

```bash
node skills/zotero-paper-summary/scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_contexts/secondary-001.md --request-retries 2 --request-retry-ms 500
```

If the transient request recovers, the output keeps `source_status: secondary_context` and records a `capture_warning`. Persistent CDP failures write `source_status: secondary_context_unavailable` instead of a raw stack trace.
````

- [ ] **Step 4: Update skill doc**

In `skills/zotero-paper-summary/SKILL.md`, mirror the README behavior in the item-details and secondary-capture steps:

```markdown
Successful immutable SQLite Extra reads are recorded as provenance diagnostics, not normal workflow warnings. Actual missing, unreadable, or item-not-found fallback still records warnings and continues the main PDF workflow.
```

and:

```markdown
Transient CDP request failures are retried by default. Tune with `--request-retries <N>` and `--request-retry-ms <ms>`. Persistent CDP failures write `source_status: secondary_context_unavailable` and a `capture_warning`; do not treat that file as usable secondary context.
```

- [ ] **Step 5: Run docs tests**

```bash
uv run pytest tests/test_default_workflow_docs.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add README.md skills/zotero-paper-summary/SKILL.md tests/test_default_workflow_docs.py
git commit -m "docs: clarify Extra fallback and CDP retry behavior"
```

---

### Task 5: Full Verification

**Files:**
- No code edits.

- [ ] **Step 1: Run focused suite**

```bash
uv run pytest tests/test_zotero_sqlite.py tests/test_zotero_item_io.py tests/test_secondary_sources.py tests/test_capture_secondary_url.py tests/test_default_workflow_docs.py -q
```

Expected: all pass.

- [ ] **Step 2: Run project verification**

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

Expected:

```text
pytest: all tests pass
zotero-paperread --help: command list renders
extract-pdf: Wrote extraction JSON: /tmp/zotero-paperread-extract.json
```

- [ ] **Step 3: Run smoke check on recent run bundle**

Use the already-created run bundle if present:

```bash
jq '{extra, enrichment:._paperread.enrichment.extra, warnings:._paperread.warnings}' runs/2026-05-07/atomic-level-fabrication-of-oxychloride-interface-for-high-rate-and-high-voltage-lithium-ion-batteries/item-details.json
jq . runs/2026-05-07/atomic-level-fabrication-of-oxychloride-interface-for-high-rate-and-high-voltage-lithium-ion-batteries/secondary_sources.json
```

Expected after regenerating item details on the patched branch:

```json
{
  "warnings": [],
  "enrichment": {
    "source": "zotero_sqlite",
    "sqlite_mode": "ro or immutable",
    "diagnostics": []
  }
}
```

For immutable mode, diagnostics may be:

```json
["sqlite_immutable_snapshot_used"]
```

but `secondary_sources.json.warnings` should stay empty when the URL was successfully recovered.

- [ ] **Step 4: Handle verification fallout without a catch-all commit**

If Task 5 exposes a failure, return to the task that owns the failing file and make the fix there. Do not create a catch-all verification commit from this step.

---

## Self-Review

- Spec coverage: point 1 is covered by Task 1 and Task 2; point 3 is covered by Task 3; docs and verification are covered by Task 4 and Task 5.
- Placeholder scan: no open-ended implementation placeholders are required; each task names files, tests, commands, and expected behavior.
- Type consistency: Python result shape remains `{"extra", "warnings", "provenance"}`; diagnostics live under `provenance["diagnostics"]`. Node warnings are emitted as repeated `capture_warning` lines and do not become primary evidence.
