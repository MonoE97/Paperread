# Zotero 笔记抽取与排版设计

**状态：** 已批准进入实施规划。

**日期：** 2026-06-18

**范围决定：** 只优化 Zotero-first 论文笔记的内容、排版和抽取质量。本项目不创建 ResearchWiki 风格的 `wiki/`、`synthesis/` 或 `memory/` 层。

## 摘要

本设计把 `Zotero_paperread` 从“PDF plain text + 结构化笔记”升级为两阶段阅读工作流：

1. 从论文 PDF 构建更结构化的 source context。
2. 基于该 source context 渲染双层 Zotero 子笔记。

第一层通过 section-aware 和 table/value-aware context 提高证据质量。第二层优化最终笔记，让首屏支持快速阅读决策，同时在后续章节保留学习笔记、图表、局限和证据附录。

工作流仍保持 Zotero-first：

- Zotero 访问仍然封装在 `zotero-mcp` 后面。
- 项目只有在用户明确要求时才写入 Zotero child note。
- `note.md` 仍然是审计件。
- `note.html` 仍然是 Zotero 写入 payload。
- Better Notes 仍然只是可选显示软件，不作为运行时依赖。
- ResearchWiki 只作为 evidence、claim、method、gap 纪律的设计参考，不作为文件系统目标。

## 背景

当前项目已经具备几块可靠能力：

- `prepare-item` 会创建 run 目录，包含 metadata、PDF text、secondary-source metadata、figure output 和 figure context。
- `summary.json` 已经包含 learning-note 字段，例如 `method_modules`、`key_results_table`、`concept_cards`、`workflow_lessons` 和 `reading_decision`。
- note renderer 会生成 `note.md` 和 Zotero-ready 的 `note.html`。
- write gate 会在真实 Zotero 写入前校验 trust status、review status、evidence locator、note tags 和 note preview。
- figure extraction 已经记录 provenance、confidence、quality warnings 和 evidence tiers。

主要缺口在于文本抽取仍然基本是 plain text。`context.md` 有用，但 evidence locator 粒度偏粗，通常只能定位到 page level。这会让生成的笔记更难说明某个 method detail、limitation、result 或 numeric comparison 来自哪里。

ResearchWiki 提供了一个有价值的原则：阅读笔记不应只总结论文，还应保留可复用的 claims、methods、gaps、uncertainty 和 source evidence。对本项目来说，正确的适配方式不是创建一个 Obsidian-style wiki，而是在现有 run-directory workflow 内，让 Zotero notes 更 evidence-aware、更可复用。

## 目标

1. 增加 section-aware PDF extraction，同时不破坏旧的 `extract.json` consumers。
2. 增加保守的 table/value candidates，用于 scientific results 和 numeric comparisons。
3. 生成新的 `section_context.md` artifact，用于帮助 source-grounded summarization，同时保留 canonical evidence locators。
4. 让 evidence locators 更精确、更稳定。
5. 把 Zotero note 重新设计成双层阅读笔记：
   - 前部是紧凑决策层。
   - 后部是详细学习和证据层。
6. 所有新增 summary 字段都保持 optional，旧 run 仍然能渲染。
7. 保持 write-through gates 严格，并且不改变 Zotero write behavior。

## 非目标

1. 不创建 ResearchWiki `wiki/`、`synthesis/` 或 `memory/` 目录。
2. 本阶段不创建 `knowledge_units.json`。
3. 不做 Obsidian 或 Better Notes graph synchronization。
4. 不做 cross-run relation graph 或 related-paper state。
5. 不引入数据库或 persistent index。
6. 不改 broad batch collection workflow。
7. 不直接写 Zotero SQLite。
8. 不改变 Zotero 写入行为。

## 架构

新的高层 workflow：

```text
item-details.json
  -> prepare-item
  -> extract.json
  -> context.md
  -> section_context.md
  -> figures.json
  -> figure_context.md
  -> summary.json
  -> review.json
  -> note.md / note.html
  -> gate-run
```

`context.md` 仍然是 full-text fallback。`section_context.md` 是额外的 summarization aid，不替代 `context.md`，也不是新的 canonical evidence source。如果 section detection 较弱或失败，workflow 仍然使用 page-level `context.md` locators，并记录 warning。

## 结构化抽取

### 向后兼容

`extract.json` 保留现有 top-level fields：

```json
{
  "pdf_path": "",
  "page_count": 0,
  "extracted_pages": 0,
  "text": "",
  "warnings": []
}
```

新增字段只做 additive：

```json
{
  "pages": [],
  "sections": [],
  "table_candidates": []
}
```

旧 commands、旧 tests 和旧 run directories 应保持有效。

### Page Records

每个 page record 应该 deterministic 且 lightweight：

```json
{
  "page": 1,
  "text": "Extracted page text.",
  "char_count": 1234,
  "warnings": []
}
```

Page warnings 可以包括：

- `empty_page_text`
- `short_page_text`
- `possible_ocr_needed`

这些 warnings 是 extraction diagnostics。它们不自动成为 write blockers，但应该影响 `trust_status` 和 `trust_rationale`。

### Section Records

Extractor 应该保守识别常见 scientific paper sections：

```json
{
  "kind": "methods",
  "title": "Methods",
  "start_page": 3,
  "end_page": 5,
  "text": "Aggregated section text.",
  "confidence": "high"
}
```

第一版允许的 section `kind`：

- `abstract`
- `introduction`
- `background`
- `methods`
- `experimental`
- `computational`
- `results`
- `discussion`
- `conclusion`
- `limitations`
- `references`
- `acknowledgements`
- `unknown`

检测策略应基于规则：

- 匹配 extracted text lines 中的 standalone headings。
- 支持 `1 Introduction` 或 `2. Methods` 这样的 numeric prefixes。
- 归一化常见变体，例如 `Materials and methods`、`Experimental section`、`Computational details`、`Results and discussion`。
- 覆盖材料和电池论文常见标题，例如 `Electrochemical performance`、`Ionic conductivity`、`Characterization` 和 `DFT calculations`。

Extractor 应避免 aggressive classification。如果 heading ambiguous，使用 `confidence: "low"` 或 `kind: "unknown"`，不要假装确定。

### Table and Value Candidates

第一版不尝试完美 table reconstruction。它应该输出保守 candidates，帮助 Codex 找到 results、baselines 和 numeric comparisons。

示例：

```json
{
  "page": 6,
  "section": "Results",
  "text": "Conductivity ... 1.2 mS cm-1 ... activation energy ...",
  "signals": ["conductivity", "activation energy", "baseline"],
  "confidence": "medium",
  "locator": "context.md page 6 section Results table_candidate 1"
}
```

Candidate signals 应包含 general scientific 和 AI4S/materials terms：

- `accuracy`
- `mae`
- `rmse`
- `r2`
- `speedup`
- `baseline`
- `ablation`
- `conductivity`
- `ionic conductivity`
- `activation energy`
- `diffusion barrier`
- `capacity`
- `cycle life`
- `rate performance`
- `energy density`
- `voltage`
- `bandgap`
- `formation energy`
- `ehull`

Confidence rule 应保持保守：

- `high`：明确 table-like text，并且包含多个 numeric/result signals。
- `medium`：paragraph 或 line block 中包含 numeric/result signals。
- `low`：弱 numeric signal，只能作为 search hint。

Low-confidence table candidates 不能在没有 supporting text 或 figure context 的情况下被当作强证据。

## Section Context Artifact

当 `extract.json` 包含 page、section 或 table-candidate data 时，`prepare-item` 应写入 `section_context.md`。

建议格式：

```md
# Section Context

## Extraction Summary

- PDF Path: <path>
- Page Count: <N>
- Extracted Pages: <N>
- Section Count: <N>
- Table Candidate Count: <N>

## Sections

### Methods

- Kind: methods
- Pages: 3-5
- Confidence: high
- Locator: context.md page 3 section Methods

<section text excerpt or full section text>

## Table / Value Candidates

### Candidate 1

- Locator: context.md page 6 section Results table_candidate 1
- Confidence: medium
- Signals: conductivity, activation energy

<candidate text>
```

`section_context.md` 应列入 `prepare-item` output，并且当 manifest 存在时写入 `run.json`。

## Evidence Locator Contract

Workflow 应优先使用这些 locator forms：

```text
context.md page 3 section Methods
context.md page 6 section Results table_candidate 1
figure_context.md fig_p4_1
```

Locator rules：

1. Trusted evidence locators 仍然仅限 `context.md` 和 `figure_context.md`。
2. Secondary contexts 仍然只是 cross-check material，不得在 `evidence_summary` 中引用。
3. `section_context.md` 可以帮助 Codex 找到 section 和 table/value candidates，但最终 `evidence_summary` locators 应引用 canonical source form，例如 `context.md page 3 section Methods`。
4. Low-confidence section 和 table candidates 只能通过 canonical `context.md ...` locators 引用，并且必须在 evidence summary 中带 caveat。
5. `lint-summary` 应拒绝 secondary context locators 和 malformed trusted locators。

## Summary Contract Additions

Renderer 应保留旧字段，并新增 optional fields：

```json
{
  "recommended_sections": [],
  "recommended_figures": [],
  "baseline_or_comparison": [],
  "result_evidence_notes": [],
  "author_stated_limitations": [],
  "inferred_limits": [],
  "potential_gaps": [],
  "evidence_quality_summary": ""
}
```

### `recommended_sections`

值得优先阅读的 section 短列表：

```json
[
  {
    "section": "Methods",
    "locator": "context.md page 3 section Methods",
    "reason": "Best source for the model design and assumptions."
  }
]
```

### `recommended_figures`

值得打开的关键图短列表：

```json
[
  {
    "figure_id": "fig_p4_1",
    "locator": "figure_context.md fig_p4_1",
    "reason": "Shows the overall workflow and evidence chain."
  }
]
```

### `baseline_or_comparison`

Comparison targets、baselines 或 reference systems：

```json
[
  {
    "target": "DFT baseline",
    "result": "Lower MAE on formation energy prediction.",
    "locator": "context.md page 6 section Results table_candidate 1"
  }
]
```

### `result_evidence_notes`

主要结果的简短 evidence-quality notes：

```json
[
  {
    "result": "Ionic conductivity improved at room temperature.",
    "evidence": "Reported in the Results section with table-like numeric comparison.",
    "locator": "context.md page 6 section Results table_candidate 1",
    "confidence": "medium"
  }
]
```

### `author_stated_limitations`

论文作者明确陈述的 limitations。它们应与 inferred limits 分开。

推荐 object form：

```json
[
  {
    "text": "The authors state that only one material family was evaluated.",
    "locator": "context.md page 8 section Discussion",
    "source_type": "author_stated"
  }
]
```

### `inferred_limits`

Codex 或读者根据 scope、dataset、missing experiments 或 weak extraction 推断出的 limits。它们不能被呈现为 author claims。

推荐 object form：

```json
[
  {
    "text": "Generalization to sulfide solid electrolytes is not established.",
    "basis": "The experiments cover oxide examples only.",
    "locator": "context.md page 6 section Results",
    "source_type": "inferred"
  }
]
```

### `potential_gaps`

论文提示的 follow-up research gaps。每个 gap 应包含 paper evidence，或者显式 uncertainty label。

推荐 object form：

```json
[
  {
    "text": "Whether the workflow transfers to reactive battery interfaces remains open.",
    "basis": "The paper validates non-reactive interface examples.",
    "locator": "context.md page 7 section Results",
    "uncertainty": "AI inference"
  }
]
```

### `evidence_quality_summary`

简要说明笔记依赖 full text、section context、figure context、weak extraction 还是 metadata-only evidence。

## Note Layout

最终 note 应成为双层阅读笔记。

### 建议章节

```md
# [Codex Summary] <title> - <date>

## 0. 速读决策
## 1. 论文核心
## 2. 方法怎么做
## 3. 结果是否站得住
## 4. 图表导读
## 5. 局限、适用边界与潜在 gap
## 6. 可迁移启发
## 7. 术语与概念卡片
## 8. 后续检索关键词
## 9. 元数据
## 10. 证据链附录
## 11. 补充优化记录

Tags: codex-summary, paper-summary, ...
```

### Section 0：`速读决策`

这一节应该紧凑、行动导向：

- 30 second takeaway
- reading decision
- relevance to the user's AI4S / materials / battery work
- trust status
- main risk
- recommended sections
- recommended figures

不要让大表格成为首屏唯一体验。表格仍然有价值，但这一节应该优先支持人的快速 triage。

### Section 1：`论文核心`

这一节应该解释：

- research object
- core problem
- existing gap
- paper entry point
- one-sentence contribution

### Section 2：`方法怎么做`

这一节应保留现有 method module table 和 workflow：

- method overview
- method modules
- workflow
- assumptions and technical details

### Section 3：`结果是否站得住`

这一节应强制进行 evidence-aware result reading：

- key results table
- baseline or comparison targets
- table/value evidence notes
- evidence-quality summary
- 当 results 来自 weak extraction 或 low-confidence table candidates 时给出 caveats

### Section 4：`图表导读`

这一节保留当前 figure overview、figure index 和 expanded key-figure analysis。它应继续尊重 figure evidence tiers 和 visual-quality warnings。

### Section 5：`局限、适用边界与潜在 gap`

这一节必须分开：

- author-stated limitations
- inferred limits
- potential gaps and follow-up questions

Codex-inferred limits 和 gaps 不能写成 paper-authored claims。

### Sections 6-11

这些章节保留当前 learning-note value：

- transferable insight
- workflow lessons
- concept cards
- follow-up keywords
- metadata
- evidence chain
- improvement notes

Evidence chain 仍放在后部，这样 provenance 可用，但不打断阅读流。

## Lint and Gate Changes

`lint-summary` 应扩展检查：

1. secondary context 没有被用作 trusted evidence
2. malformed trusted locators 会被标记
3. `author_stated_limitations` entries 在使用 object form 时具有 `source_type: "author_stated"`
4. `inferred_limits` entries 在使用 object form 时具有 `source_type: "inferred"`
5. `workflow_steps` 作为 multi-line Markdown 或 normalized list text 时仍可读

需要 paper understanding 的 semantic checks 应留在 review pass，而不是 deterministic lint。尤其是 review pass 应检查 low-confidence table candidates 是否被过度宣称，以及 inferred limits 是否被写成 paper-authored limitations。

`validate_trusted_summary` 应继续专注 write-through readiness。它不应要求每个新增 optional field，但 write-ready notes 仍应要求 core evidence、trust、review 和 improvement fields。

## 实施阶段

### Phase 1：Structured Extraction

可能改动的文件：

- `src/zotero_paperread/pdf_extract.py`
- `src/zotero_paperread/workflow.py`
- `src/zotero_paperread/cli.py`
- `tests/test_pdf_extract.py`
- `tests/test_workflow.py`
- `README.md`
- `skills/zotero-paper-summary/SKILL.md`

Deliverables：

- `extract.json` 中 additive 的 `pages`、`sections` 和 `table_candidates`
- `section_context.md`
- `section_context_md` 的 run manifest support
- docs 和 skill 更新：说明 Codex 在可用时应读取 `section_context.md`，但最终 evidence locators 仍是 `context.md ...` 或 `figure_context.md ...`

### Phase 2：Note Layout and Summary Contract

可能改动的文件：

- `src/zotero_paperread/note.py`
- `src/zotero_paperread/summary_lint.py`
- `templates/zotero_note.md.j2`
- `tests/test_note.py`
- `tests/test_cli_note.py`
- `tests/test_summary_lint.py`
- `README.md`
- `skills/zotero-paper-summary/SKILL.md`

Deliverables：

- 新的 two-layer note section order
- optional new fields 的安全 cleaning/rendering
- old-summary compatibility
- canonical evidence locator rules 和 structured limitation fields 的 summary lint checks
- new note structure 的 docs 和 skill updates

## 验证

Phase 1 focused verification：

```bash
uv run pytest tests/test_pdf_extract.py tests/test_workflow.py -q
```

Phase 2 focused verification：

```bash
uv run pytest tests/test_note.py tests/test_cli_note.py tests/test_summary_lint.py -q
```

Final project verification：

```bash
uv run pytest
uv run zotero-paperread --help
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

任何 verification step 都不应写入 Zotero。

## 风险与缓解

### Section Detection 错误

缓解：

- 保持 section detection conservative。
- 记录 confidence。
- 允许 page-level fallback。
- 让 low-confidence section locators 以 caveat 形式可见。

### Table Candidates 噪声过多

缓解：

- 称为 candidates，而不是 extracted truth。
- 包含 confidence。
- 要求 summary/review 引用 supporting text，或对 weak candidates 给出 caveat。

### Note 过长

缓解：

- 把 decision material 放在第一节。
- 把 evidence 和 audit material 后置。
- recommendation lists 保持简短。

### ResearchWiki Scope Creep

缓解：

- 不创建 wiki directories。
- 本阶段不做 knowledge-unit export。
- 不做 cross-run synthesis state。
- 不使用 Obsidian-specific links。

### Old Runs 破坏

缓解：

- 所有 new fields 都是 optional。
- Python render context 提供 defaults。
- `extract.json` additions 保持 backward compatible。
- tests 覆盖 old summary rendering。

## 待决策项

本 implementation plan 没有遗留 product decisions。用户已选择：

- 只优化 Zotero note 和 extraction quality
- 使用 two-stage plan
- 先做 section-aware extraction，再做 table/value candidates
- 使用 two-layer note layout
- 实施完整 two-stage approach，而不是 template-only update
