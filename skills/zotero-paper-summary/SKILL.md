---
name: zotero-paper-summary
description: Use when the user asks to summarize, analyze, preview, regenerate, or write a Zotero paper note from a Zotero title or title fragment.
---

# Zotero Paper Summary

## Canonical Entry

Recommended invocation:

```text
summarize-zotero-title "<paper title>"
```

Write-through invocation:

```text
summarize-zotero-title "<paper title>" and write to zotero
```

Natural-language write intent example:

```text
请帮我分析这篇文献并写入笔记：<paper title>
请对 Zotero 中的 <paper title> 文章进行分析并输出笔记
```

If historical memory or a user message contains an old absolute skill path, do not rely on plugin cache hashes. Prefer the current repo-local stable entry:

```bash
rg --files -g 'SKILL.md' skills /Users/jwxi/.codex/skills | rg 'zotero|paper'
```

Use `skills/zotero-paper-summary/SKILL.md` in this repository first.

## 目标

把 Zotero 中的一篇论文转换为中文结构化研究笔记。默认只 dry-run；当用户明确要求“输出笔记”“写入笔记”“写回 Zotero”“创建 note”“保存到 Zotero”等动作时，执行分析、预览并创建 Zotero 子笔记。

在本项目和用户约定中，“输出笔记”是 Zotero write-through 意图，不是单纯打印 Markdown；但仍必须通过完整写入门禁，且只有 `zotero-mcp write_note` 可以执行真实写入。

## 当前 Zotero note 渲染结构

最终写入 Zotero 的阅读笔记采用 0-7 reading-thread layout：

1. `0. 阅读结论`：30 秒结论、阅读决策、研究相关性、可信状态、主要风险、推荐先读章节和图表。
2. `1. 论文主张`：背景问题、已有缺口、本文切入点、一句话贡献和摘要翻译。
3. `2. 方法与设计`：方法总览、模块拆解、workflow 和必要技术细节。
4. `3. 结果可信度`：关键结果表、baseline/comparison、结果证据说明和证据质量。核心数值和 locator 只在这里完整展开。
5. `4. 图表导读`：说明关键图回答什么问题、如何支撑主线、图像/抽取质量如何；不要把图表段写成第二份结果总结。
6. `5. 边界与机会`：作者明示局限、Codex 推断限制、适用边界和 potential gaps。
7. `6. 我能怎么用`：可迁移启发、workflow lessons 和后续问题。
8. `7. 术语与检索`：核心概念卡片、后续检索关键词和 trailing tags。

`metadata`、`evidence_summary`、`review_status`、`improvement_status`、`improvement_notes` 是 audit-only for rendered note。它们必须保留在 JSON artifacts 和 gate inputs 中，但不要作为 `元数据`、`证据链附录` 或 `补充优化记录` 专门渲染进 Zotero note。

## 输入

接受 Zotero 条目标题或标题片段。V2 仍只处理单篇论文，不处理 collection 批量任务。

## 工具边界

- 开始前先用 `tool_search` 精确加载 Zotero MCP 工具，至少查询：

```text
zotero mcp search_library get_item_details get_content write_note annotations
```

- 必需读工具：
  - `zotero-mcp search_library`
  - `zotero-mcp get_item_details`
  - `zotero-mcp get_content` 或本项目 `prepare-item` 的 PDF 抽取路径
- 必需写工具：
  - 只有显式写入时才调用 `zotero-mcp write_note`
- `annotations` 相关工具只是可选增强；核心必需工具是 `search_library`、`get_item_details`、`get_content` 和 `write_note`。
- Known MCP behavior: `get_item_details` is available in `cookjohn/zotero-mcp` 1.4.7. If Codex does not show it initially, run a targeted tool search before assuming the MCP server lacks the tool.
- 如果 `get_item_details` 初始不可见，不要手工拼 metadata；先用 `tool_search` 重新加载。只有 `tool_search` 后仍不可用，才停止并说明这是 Codex App 工具发现/注入问题。
- 用本项目 Python CLI 抽取 PDF 与渲染 note。
- 不修改 Zotero SQLite。
- 不调用 Better Notes API。
- 不修改 Better Notes 配置。

## 工作流

1. 搜索 Zotero 条目：
   - 先使用标题 exact 搜索；如果 0 个匹配，再使用 contains 搜索作为发现入口。
   - 0 个匹配：停止，告诉用户没有找到。
   - 多个 exact 匹配且标题归一化后相同：same normalized title，stop before create-run，停止分析和写入，告诉用户 Zotero 中存在 duplicate Zotero entries，请先在 Zotero 中去重；不要在重复条目中帮用户选择一个。
   - 多个 contains 匹配但不是同一 normalized title：列出候选标题、作者、年份和 key，停止，要求用户提供更精确标题或 item key；不要写入。
   - 1 个匹配：继续。
   - normalized title 比较至少忽略大小写、连续空白、常见 dash 变体和首尾空格；如果仍有多个同题条目，视为重复条目。

2. 创建 run 目录：
   - 运行：

```bash
uv run zotero-paperread create-run --title "<title>" --item-key "<item_key>"
```

   - 使用返回的 run 目录作为本次任务的唯一工作目录，路径风格为 `runs/<date>/<paper-slug>/`。
   - `create-run` 负责建立该目录并写入 `run.json`，后续所有产物都留在这里。

3. 获取条目详情：
   - 调用 `get_item_details(itemKey=<item_key>, mode="complete")`。
   - 将 MCP 原始响应保存为 `<run_dir>/mcp-response.json` 后，运行：

```bash
uv run zotero-paperread save-item-details <run_dir>/mcp-response.json --output <run_dir>/item-details.json --raw-output <run_dir>/item-details.raw.json
```

   - 后续命令只读取规范化后的 `<run_dir>/item-details.json`；`item-details.raw.json` 作为 MCP 原始返回审计件保留。
   - `save-item-details` 默认在 MCP 响应缺少 `extra` 字段时，用只读 Zotero SQLite fallback 补齐 Zotero `Extra` / `其他` 字段。
   - 该 fallback 只读 `~/Zotero/zotero.sqlite`，不会写 Zotero；如果 SQLite 不可读、被锁、缺少 item 或没有 extra，保留 warning 并继续主 PDF workflow。
   - 如需禁用 fallback，追加 `--no-sqlite-extra-fallback`：

```bash
uv run zotero-paperread save-item-details <run_dir>/mcp-response.json --output <run_dir>/item-details.json --raw-output <run_dir>/item-details.raw.json --no-sqlite-extra-fallback
```

   - 如果返回中 `attachments[].path` 已有本地 PDF 路径，直接交给 `prepare-item`。
   - 如果没有 PDF path，但有 PDF attachment key，先报告 `missing_pdf_path_in_item_details`，不要直接猜 Zotero storage 路径；只有用户明确要求排障时才进行本机路径探测。

4. 检查已有 Codex 笔记：
   - 在 item details 中检查已有 child notes、notes、children 或可见 note 元数据。
   - 如果存在标题以 `[Codex Summary]` 开头、包含 `codex-summary` tag、或明显是本 workflow 创建的 Codex summary note，则默认停止，不继续抽取、总结或写入。
   - 停止时告诉用户已存在的 Codex note 标题和 key；如果无法取得标题，至少返回 note key。
   - 只有当用户明确要求“继续分析”“重新生成”“强制分析”“即使已有也继续”等动作时，才继续执行。继续时仍然创建新版本，不覆盖旧 note。
   - single-paper summary writes always create a new versioned Zotero child note；不要 update 既有 `[Codex Summary]` 总结 note。
   - 如果用户明确要求继续创建新版本，最终写入门禁必须运行 `prepare-write-candidate` 或等价底层链路，先用只读 live note refresh 刷新当前 Zotero 子笔记标题，再计算同日版本后缀：
     - 当日第一版：`[Codex Summary] <paper title> - YYYY-MM-DD`
     - 当日第二版：`[Codex Summary] <paper title> - YYYY-MM-DD (v2)`
     - 当日第三版：`[Codex Summary] <paper title> - YYYY-MM-DD (v3)`
   - 日常写入前先运行 `prepare-write-candidate <run_dir> --paper-title "<paper title>" --generated-date YYYY-MM-DD`，它会用只读 Zotero local API 刷新 live 子笔记标题、计算后缀、重新生成 `note.md`/`note.html`、预览、跑 gate 并生成 `write-payload.json`。
   - 只有调试底层链路时才手动运行 `refresh-live-notes -> next-version-suffix -> finalize-note --html-output -> note-tags -> preview-note -> gate-run -> prepare-write-payload`。

5. 准备 bundle：
   - 运行：

```bash
uv run zotero-paperread prepare-item <run_dir>/item-details.json --workdir <run_dir>
```

   - 该命令会生成：
     - `metadata.json`
     - `extract.json`
     - `context.md`
     - `section_context.md`
     - `secondary_sources.json`
     - `figures.json`
     - `figure_context.md`
     - `figures/`
   - 默认处理完整 PDF，不限制页数。只有用户明确要求快速调试、预览或截断抽取时，才显式追加 `--max-pages <N>`。
   - `context.md` 是默认总结输入源，包含元数据、摘要、抽取告警和 PDF 正文。
   - `section_context.md` 是章节和 table/value candidate 导航材料，not a canonical evidence source；最终 `evidence_summary` locator 仍必须引用 `context.md page 3 section Methods`、`context.md page 6 section Results table_candidate 1` 或 `figure_context.md fig_p4_1` 这类 canonical source。
   - `figure_context.md` 用于关键图片筛选与分析，包含 figure provenance、source attempts、warnings 和候选图摘要。
   - `metadata.json` 中的 PDF 默认选择主论文；文件名、路径或标题含 appendix、supplement、supporting information 等低优先级信号的 PDF 会排在主文后面。
   - `figure_context.md` 的每张图包含 `Caption Confidence`。caption confidence 低或 caption 缺失时，图分析必须保守表述。
   - 如果 figure extraction 失败，`prepare-item` 仍保留文本 bundle，并在 warnings 中写入 `figure_extraction_failed` 和 `figure_extraction_error:<type>:<message>`；此时总结必须降低图证据权重。
   - 无 PDF 时也继续工作，只是在 `extract.json` 中记录 `missing_pdf_attachment`。

6. 生成 summary JSON：
   - 输出必须包含这些字段：

```json
{
  "one_sentence_summary": "",
  "abstract_translation": "",
  "key_points": [],
  "research_question": "",
  "method": "",
  "figure_overview": "",
  "key_figures": [
    {
      "figure_id": "",
      "caption": "",
      "page": 0,
      "priority_score": 0,
      "title_short": "",
      "why_it_matters": "",
      "why_it_matters_short": "",
      "evidence_level": "unknown",
      "image_quality": "unknown",
      "figure_quality_note": "",
      "analysis": ""
    }
  ],
  "experiments": "",
  "contributions": [],
  "limitations": [],
  "ai4s_relevance": "",
  "follow_up_keywords": [],
  "note_labels": [],
  "research_object": "",
  "research_question_short": "",
  "core_method_short": "",
  "core_result_short": "",
  "relevance_to_user": "",
  "reading_decision": "unknown",
  "main_risk_short": "",
  "tldr": "",
  "background_problem": "",
  "existing_gap": "",
  "paper_entry_point": "",
  "method_overview": "",
  "method_modules": [
    {
      "name": "",
      "input": "",
      "target": "",
      "output": "",
      "role": ""
    }
  ],
  "workflow_steps": "",
  "technical_details": [],
  "key_results_table": [
    {
      "result": "",
      "value": "",
      "meaning": ""
    }
  ],
  "recommended_sections": [
    {
      "section": "",
      "locator": "context.md page 3 section Methods",
      "reason": ""
    }
  ],
  "recommended_figures": [
    {
      "figure_id": "",
      "locator": "figure_context.md fig_p4_1",
      "reason": ""
    }
  ],
  "baseline_or_comparison": [
    {
      "target": "",
      "result": "",
      "locator": "context.md page 6 section Results table_candidate 1"
    }
  ],
  "result_evidence_notes": [
    {
      "result": "",
      "evidence": "",
      "locator": "context.md page 6 section Results table_candidate 1",
      "confidence": "medium"
    }
  ],
  "applicability_limits": [],
  "author_stated_limitations": [
    {
      "text": "",
      "locator": "context.md page 8 section Discussion",
      "source_type": "author_stated"
    }
  ],
  "inferred_limits": [
    {
      "text": "",
      "basis": "",
      "locator": "context.md page 6 section Results",
      "source_type": "inferred"
    }
  ],
  "potential_gaps": [
    {
      "text": "",
      "basis": "",
      "locator": "context.md page 7 section Results",
      "uncertainty": "AI inference"
    }
  ],
  "evidence_quality_summary": "",
  "transferable_insight": "",
  "workflow_lessons": [],
  "follow_up_questions": [],
  "concept_cards": [
    {
      "term": "",
      "short_definition": "",
      "role_in_paper": "",
      "related_keywords": []
    }
  ],
  "quality_score": "",
  "extraction_warnings": [],
  "paper_type": "research_article",
  "trust_status": "usable_with_caveats",
  "evidence_summary": [
    {
      "claim": "",
      "evidence": [
        {
          "type": "text",
          "locator": "context.md page 1",
          "summary": ""
        }
      ],
      "confidence": "high"
    }
  ],
  "trust_rationale": "",
  "review_status": "not_reviewed",
  "review_issues": [],
  "improvement_status": "not_needed",
  "improvement_notes": []
}
```

7. 分析要求：
   - 参考 `evil-read-arxiv` 的 `paper-analyze` 思路，覆盖摘要翻译、研究背景、研究问题、方法、实验、贡献、局限、相关方向定位。
   - 参考 `extract-paper-images` 的思想，但改成 Zotero-first：优先使用 `figure_context.md` 中最重要的 1-4 张关键图做“图文联合分析”。
   - 公式使用 Markdown LaTeX：行内 `$...$`，块级 `$$...$$`。
   - 不编造论文没有支持的数据、实验或结论。
   - 对 AI+物理/材料的启发必须独立成节，结合用户研究方向给出判断。
   - 如果条目是综述、Perspective、评论文章或方法综述，明确按“综述类文献”处理，不要虚构本文原创实验。
   - `figure_overview` 必须解释关键图在整篇论文中的证据角色。
   - 优先分析 `figure_context.md` 中 priority score 高、caption confidence 高、source provenance 清楚的图。
   - 对 embedded-image 或 low-confidence caption 的图，不要把 caption 推断当成确定事实。
   - `key_figures` 中每个对象都必须解释：
     - 这张图展示什么
     - 为什么它重要
     - 它支撑了哪条核心结论或方法理解
   - `note_labels` 只写本文自动推断出的英文规范 key，不要包含固定系统标签 `codex-summary` 或 `paper-summary`。
   - `note_labels` 最多 4 个；使用 lowercase snake_case，例如 `metasurface`、`inverse_design`、`deep_learning`、`power_allocation`。
   - `reading_decision` 必须从 `strongly_recommended`、`recommended`、`skim_only`、`not_priority`、`unknown` 中选择。
   - `main_risk_short` 只写一个最重要的风险；完整告警保留在 `extraction_warnings` 和 `review_issues`。
   - `workflow_steps` 可以是 Markdown string，也可以是 string list。
   - `recommended_sections` 和 `recommended_figures` 只列最值得先看的 1-5 项，locator 使用 canonical form。
   - `baseline_or_comparison` 和 `result_evidence_notes` 用于支撑 `## 3. 结果可信度`，低置信 table/value candidate 必须写 caveat。
   - `author_stated_limitations` 只放作者明示的局限，object form 使用 `source_type: "author_stated"`。
   - `inferred_limits` 只放 Codex/读者基于范围、数据或缺失实验推断出的限制，object form 使用 `source_type: "inferred"`，不得写成作者声称。
   - `potential_gaps` 是后续研究 gap，必须包含 evidence locator 或显式 uncertainty。
   - `method_modules` 优先用于 AI4S、computational materials、battery、simulation 和 method papers，拆出方法模块的输入、目标、输出和作用。
   - `key_results_table` 汇总 RMSE、MAE、speedup、capacitance、energy density、cycle life、rate performance、diffusion barrier、conductivity、OOD performance 等关键结果。
   - `concept_cards` 写 3-8 个核心概念。
   - `workflow_lessons` 提炼可复用研究 workflow，不重复本文结论。
   - `paper_type` 必须从 `research_article`、`review`、`perspective`、`benchmark`、`method_paper`、`dataset_paper`、`theory_paper`、`unknown` 中选择。
   - `trust_status` 必须从 `trusted`、`usable_with_caveats`、`metadata_only`、`needs_manual_review` 中选择；默认用 `usable_with_caveats`，不要过度自信；不要输出 `needs_review`、`low_confidence` 或 `failed`。
   - `evidence_summary` 最多 5 条，每条结论最多列 3 个证据 locator；证据必须来自 `context.md` 或 `figure_context.md`。
   - `trust_rationale` 必须解释可信状态和抽取告警之间的关系。
   - `evidence_level` 必须从 `high`、`medium`、`low`、`text_only`、`caption_only`、`image_unverified`、`unknown` 中选择。
   - `image_quality` 必须从 `good`、`ok`、`poor`、`image_too_small`、`caption_only`、`unknown` 中选择。
   - 如果 `visual_quality.warnings` 存在，优先使用 `image_too_small` 等具体 warning，不要泛化成 `poor`。
   - 当 `image_quality` 为 `poor`、`image_too_small` 或 `caption_only` 时，`figure_quality_note` 和 `analysis` 必须说明图分析不依赖像素读取，只基于正文或 caption。

## 二级材料 capture

当用户提供微信公众号、新闻稿、博客或其他网页作为补充材料，或 `secondary_sources.json` 从 Zotero `Extra` / `其他` 检测到链接时，先用二级材料 capture，不要把网页正文混入 PDF 主证据。

```bash
node skills/zotero-paper-summary/scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_context.md
```

如果 `<run_dir>/secondary_sources.json` 中存在 `sources`，逐条使用现有 CDP capture 脚本写入 `<run_dir>/secondary_contexts/<source_id>.md`：

```bash
mkdir -p <run_dir>/secondary_contexts
node skills/zotero-paper-summary/scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_contexts/secondary-001.md --request-retries 2 --request-retry-ms 500
```

Successful immutable SQLite Extra reads are recorded as provenance diagnostics, not normal workflow warnings. Actual missing, unreadable, or item-not-found fallback still records warnings and continues the main PDF workflow.

微信公众号默认使用 Chrome CDP。脚本默认最多等待 `60000` ms，直到页面完成导航且正文非空；只有调试或测试时才使用 `--timeout-ms <ms>` / `--poll-ms <ms>` 缩短等待。Transient CDP request failures are retried by default. Tune with `--request-retries <N>` and `--request-retry-ms <ms>`。成功输出文件必须包含 `source_status: secondary_context`。如果 transient request 恢复成功，输出仍保持 `source_status: secondary_context`，并记录 `capture_warning`。如果页面一直停在 `about:blank` 或超时仍没有正文，输出 `source_status: secondary_context_unavailable` 和 `capture_warning: navigation_timeout`。Persistent CDP failures write secondary_context_unavailable and a `capture_warning`; do not treat that file as usable secondary context. `evidence_summary` must not cite secondary context；它只能用于 cross-check、补充阅读背景和提示后续问题。

`secondary_contexts/*.md` 和 `secondary_sources.json` 都是 `cross-check only; must not be cited in evidence_summary`。可信证据仍只来自 `context.md` 和 `figure_context.md`。

8. 初始渲染和验证 note（dry-run 审查输入）：

   - 写完 `<run_dir>/summary.json` 后，先检查 JSON 可读性：

```bash
uv run zotero-paperread validate-summary-json <run_dir>/summary.json
```

```bash
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --output <run_dir>/note.md --html-output <run_dir>/note.html
uv run zotero-paperread preview-note <run_dir>/note.md
uv run zotero-paperread preview-note <run_dir>/note.html
```

   - 生成的 `summary.json`、`note.md`、`note.html` 和预览输出都保留在同一个 run 目录里，便于审计和复查。
   - `validate-summary-json` 只证明文件是可读 UTF-8 JSON 且顶层是 object，不代表语义字段已经完整正确。
   - 推荐使用 `finalize-note`，它会按正确顺序执行 `render-note -> validate-note`。
   - Zotero note 内部是 HTML；`note.md` 用于人工审查，真实写入时使用 `note.html`，避免 Markdown 表格在 Zotero 中被当作普通文本。
   - 如果本次是同日新版本，把步骤 4 得到的 suffix 传给 `finalize-note`，例如：

```bash
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --output <run_dir>/note.md --html-output <run_dir>/note.html --version-suffix " (v2)"
```

   - 如果手动拆开执行，必须按 `render-note -> validate-note -> render-note-html -> preview-note` 串行执行，不能并行调度。
   - 这是供二次质量审查读取的初始 dry-run note，不是最终 write-through gate。即使用户已经明确要求写入，也必须先完成 `note.md` 和 `note.html` 的 `preview-note`，确认目标条目标题和 note 预览都已经生成，再进入 Zotero 写入步骤。

9. 二次质量审查：
   - 阅读 `<run_dir>/context.md`、`<run_dir>/figure_context.md`、`<run_dir>/summary.json` 和 `<run_dir>/note.md`。
   - 生成 `<run_dir>/review.json`。
   - 审查必须检查：
     - 主要结论是否有 page 或 figure 证据
     - 局限是否具体而不是泛泛而谈
     - 论文类型是否合理
     - 是否把背景知识写成本论文贡献
     - 图分析是否来自真实 `figure_context.md`
     - 是否过度相信低 `Caption Confidence`、embedded-image backfill、或 figure extraction warning
     - 是否因抽取告警需要降级可信状态
   - `review.json` 必须包含 `review_status`、`review_issues`、`trust_status_recommendation`、`needs_improvement` 和 `improvement_requests`。
   - 生成 `review.json` 后，必须按下方最终写入门禁顺序把审查字段确定性合并回 `summary.json` 并重新生成 note。如果 `needs_improvement` 为 true，`validate-trusted-summary` 会阻断写入；完成第 10 步补充优化并生成新的 `review.json` 后，重复同一门禁序列。

```bash
uv run zotero-paperread validate-summary-json <run_dir>/summary.json
uv run zotero-paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run zotero-paperread lint-summary <run_dir>/summary.json
uv run zotero-paperread validate-trusted-summary <run_dir>/summary.json
PAPER_TITLE="<paper title>"
GENERATED_DATE="<YYYY-MM-DD>"
uv run zotero-paperread refresh-live-notes <run_dir>/item-details.json --output <run_dir>/item-details.json
VERSION_SUFFIX="$(uv run zotero-paperread next-version-suffix <run_dir>/item-details.json --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE")"
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --generated-date "$GENERATED_DATE" --version-suffix "$VERSION_SUFFIX" --output <run_dir>/note.md --html-output <run_dir>/note.html
NOTE_TAGS_JSON="$(uv run zotero-paperread note-tags <run_dir>/summary.json)"
uv run zotero-paperread preview-note <run_dir>/note.md
uv run zotero-paperread preview-note <run_dir>/note.html
uv run zotero-paperread gate-run <run_dir> --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE" --output <run_dir>/gate-report.json
uv run zotero-paperread prepare-write-payload <run_dir>/gate-report.json --output <run_dir>/write-payload.json
```

10. 补充优化：
   - 如果 `review.json` 中 `needs_improvement` 为 true，允许一次补充优化。
   - 只允许重读当前 run 目录中的 `context.md`、`figure_context.md`、`extract.json`、`figures.json`。
   - 可以更新 `summary.json` 中的方法、局限、证据、可信状态、审查状态和 `improvement_notes`。
   - 不允许使用外部知识补证据。
   - 补充后必须重新运行 `finalize-note`，再做一次质量审查。
   - 自动补充最多一次。
   - 如果现有抽取材料不足以修复问题，设置 `improvement_status` 为 `blocked`，并降低或保留保守的 `trust_status`。

11. 写入 Zotero：
   - 只有用户明确要求“输出笔记”“写入”“写入笔记”“写回 Zotero”“创建 note”“保存到 Zotero”等动作时执行；其中“输出笔记”是本项目和用户约定的 Zotero write-through 触发词。
   - 写入 Zotero 前必须满足：

```text
review_status is passed or passed_with_caveats
review.json needs_improvement is false
summary.json improvement_status is neither needed nor blocked after apply-review
validate-trusted-summary passes
same-day version suffix has been computed from current item-details.json
Zotero note tags have been computed from current summary.json using `note-tags`
preview-note has been shown for note.md and note.html
target Zotero item title has been shown
```

   - 如果 `review_status` 为 `failed`，停止并报告审查问题，不写入 Zotero。
   - note 标题由模板生成：`[Codex Summary] <paper title> - YYYY-MM-DD`，同日重复创建时追加 ` (v2)`、` (v3)` 等后缀。
   - note 正文末尾 `Tags:` 和 Zotero note metadata tags 必须使用同一套标签：固定标签 `codex-summary`、`paper-summary`，加上 `summary.json` 中 `note_labels` 归一化后的最多 4 个推断标签。
   - Zotero local API is read-only in this project；只允许用于 live note refresh 和写后 readback validation，不允许 PUT/PATCH/POST/DELETE、SQLite mutation 或任何持久写入。
   - 日常写入前先运行 `prepare-write-candidate <run_dir> --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE"`；它会固定执行 live refresh、版本后缀计算、`finalize-note --html-output`、preview、`gate-run` 和 `prepare-write-payload`。
   - `prepare-write-payload does not write to Zotero`; it only prepares metadata for the agent-side `write_note` call and readback checklist. Real writes still happen only through `zotero-mcp write_note`.
   - 真实写入仍必须来自用户明确写入意图，且只能调用 `zotero-mcp write_note`。调用前读取 `<run_dir>/write-payload.json` 和 `<run_dir>/note.html`，传给 `write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`。
   - `write_note(action="create", ...)` 返回新 note key 后，立刻运行 `verify-zotero-note <note_key> --expected-parent <payload parentKey> --expected-title "<payload noteTitle>" --required-heading "0. 阅读结论" --required-heading "1. 论文主张" --required-heading "2. 方法与设计" --required-heading "3. 结果可信度" --required-heading "4. 图表导读" --required-heading "5. 边界与机会" --required-heading "6. 我能怎么用" --required-heading "7. 术语与检索" --forbidden-heading "9. 元数据" --forbidden-heading "10. 证据链附录" --forbidden-heading "11. 补充优化记录" --expected-tag codex-summary --expected-tag paper-summary --expected-content-sha256 <payload required_readback_checks.contentSha256> --min-content-length <payload required_readback_checks.contentLengthAtLeast>`。
   - 如果历史迁移中尝试更新既有 note 时 `write_note(action="update", ...)` 超时，必须先读回确认是否生效；若读回仍是旧内容，stop and report the failed update readback，不要改用 Zotero HTTP API、SQLite 或其他写入路径，也不要自动创建重复迁移 note。
   - 成功后回读一次 `get_item_details`，确认子笔记已经挂载到目标条目下。

## Better Notes 兼容

生成普通 Zotero 子笔记。Better Notes 如果已安装，可直接显示和管理该 note；本 skill 不依赖 Better Notes。

## 历史笔记表格迁移

当用户要求整理已有 Zotero 笔记中的表格显示问题时，不要重新总结论文。按内容格式迁移处理：

1. 先 dry-run 发现候选 `[Codex Summary]` notes。
2. 冻结 `runs/migrations/<date>-zotero-note-table-html/manifest.json`。
3. 保存每条原始 note 内容到 `raw/<noteKey>.html`。
4. 用 `classify-note-tables` 判断内容类型。
5. 用 `convert-note-tables` 生成本地 `converted/<noteKey>.html` 和转换报告。
6. 展示 manifest、blocked 列表、转换前后片段，等待用户明确确认。
7. 确认后才调用 `write_note(action="update", noteKey=<note_key>, content=<converted_html>)`。
8. update 时不要传 tags；写完逐条回读验证。

## V2 仍不做

- 不批量处理 collection。
- 不要求把图片真正嵌入 Zotero note。
- 不更新 Obsidian vault。
- 不维护 PaperGraph。
- 不把 arXiv 源码包作为必需前提；source-first 只是 opportunistic path。
