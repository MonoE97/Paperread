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

## 目标

把 Zotero 中的一篇论文转换为中文结构化研究笔记。默认只 dry-run；当用户明确要求“输出笔记”“写入笔记”“写回 Zotero”“创建 note”“保存到 Zotero”等动作时，执行分析、预览并创建 Zotero 子笔记。

在本项目和用户约定中，“输出笔记”是 Zotero write-through 意图，不是单纯打印 Markdown；但仍必须通过写入门禁：先展示 note 预览和目标 Zotero item 标题（target Zotero item title），且只有 `zotero-mcp write_note` 可以执行真实写入。

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
- 如果 `get_item_details` 初始不可见，不要手工拼 metadata；先用 `tool_search` 重新加载。只有 `tool_search` 后仍不可用，才停止并说明这是 Codex App 工具发现/注入问题。
- 用本项目 Python CLI 抽取 PDF 与渲染 note。
- 不修改 Zotero SQLite。
- 不调用 Better Notes API。
- 不修改 Better Notes 配置。

## 工作流

1. 搜索 Zotero 条目：
   - 使用标题 exact 或 contains 搜索。
   - 0 个匹配：停止，告诉用户没有找到。
   - 多个匹配：列出候选标题、作者、年份和 key，停止，不写入。
   - 1 个匹配：继续。

2. 创建 run 目录：
   - 运行：

```bash
uv run zotero-paperread create-run --title "<title>" --item-key "<item_key>"
```

   - 使用返回的 run 目录作为本次任务的唯一工作目录，路径风格为 `runs/<date>/<paper-slug>/`。
   - `create-run` 负责建立该目录并写入 `run.json`，后续所有产物都留在这里。

3. 获取条目详情：
   - 调用 `get_item_details(itemKey=<item_key>, mode="complete")`。
   - 保存原始返回到 `<run_dir>/item-details.json`。
   - 如果返回中 `attachments[].path` 已有本地 PDF 路径，直接交给 `prepare-item`。
   - 如果没有 PDF path，但有 PDF attachment key，先报告 `missing_pdf_path_in_item_details`，不要直接猜 Zotero storage 路径；只有用户明确要求排障时才进行本机路径探测。

4. 检查已有 Codex 笔记：
   - 在 item details 中检查已有 child notes、notes、children 或可见 note 元数据。
   - 如果存在标题以 `[Codex Summary]` 开头、包含 `codex-summary` tag、或明显是本 workflow 创建的 Codex summary note，则默认停止，不继续抽取、总结或写入。
   - 停止时告诉用户已存在的 Codex note 标题和 key；如果无法取得标题，至少返回 note key。
   - 只有当用户明确要求“继续分析”“重新生成”“强制分析”“即使已有也继续”等动作时，才继续执行。继续时仍然创建新版本，不覆盖旧 note。
   - 如果用户明确要求继续创建新版本，根据已有 note 标题计算同日版本后缀：
     - 当日第一版：`[Codex Summary] <paper title> - YYYY-MM-DD`
     - 当日第二版：`[Codex Summary] <paper title> - YYYY-MM-DD (v2)`
     - 当日第三版：`[Codex Summary] <paper title> - YYYY-MM-DD (v3)`
   - 调用 `finalize-note` 时用 `--version-suffix` 传入后缀；当日第一版传空后缀或省略该参数。

5. 准备 bundle：
   - 运行：

```bash
uv run zotero-paperread prepare-item <run_dir>/item-details.json --workdir <run_dir> --max-pages 15
```

   - 该命令会生成：
     - `metadata.json`
     - `extract.json`
     - `context.md`
     - `figures.json`
     - `figure_context.md`
     - `figures/`
   - `context.md` 是默认总结输入源，包含元数据、摘要、抽取告警和 PDF 正文。
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
      "why_it_matters": "",
      "analysis": ""
    }
  ],
  "experiments": "",
  "contributions": [],
  "limitations": [],
  "ai4s_relevance": "",
  "follow_up_keywords": [],
  "note_labels": [],
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
          "locator": "page 1",
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
   - `paper_type` 必须从 `research_article`、`review`、`perspective`、`benchmark`、`method_paper`、`dataset_paper`、`theory_paper`、`unknown` 中选择。
   - `trust_status` 必须从 `trusted`、`usable_with_caveats`、`metadata_only`、`needs_manual_review` 中选择；默认用 `usable_with_caveats`，不要过度自信。
   - `evidence_summary` 最多 5 条，每条结论最多列 3 个证据 locator；证据必须来自 `context.md` 或 `figure_context.md`。
   - `trust_rationale` 必须解释可信状态和抽取告警之间的关系。

8. 渲染和验证 note：

   - 写完 `<run_dir>/summary.json` 后，先检查 JSON 可读性：

```bash
uv run zotero-paperread validate-summary-json <run_dir>/summary.json
```

```bash
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --output <run_dir>/note.md
uv run zotero-paperread preview-note <run_dir>/note.md
```

   - 生成的 `summary.json`、`note.md` 和预览输出都保留在同一个 run 目录里，便于审计和复查。
   - `validate-summary-json` 只证明文件是可读 UTF-8 JSON 且顶层是 object，不代表语义字段已经完整正确。
   - 推荐使用 `finalize-note`，它会按正确顺序执行 `render-note -> validate-note`。
   - 如果本次是同日新版本，把步骤 4 得到的 suffix 传给 `finalize-note`，例如：

```bash
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --output <run_dir>/note.md --version-suffix " (v2)"
```

   - 如果手动拆开执行，必须按 `render-note -> validate-note -> preview-note` 串行执行，不能并行调度。
   - 即使用户已经明确要求写入，也必须先完成 `preview-note`，确认目标条目标题和 note 预览都已经生成，再进入 Zotero 写入步骤。

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
   - 生成 `review.json` 后，必须先把审查门禁字段确定性合并回 `summary.json`。如果 `needs_improvement` 为 true，`validate-trusted-summary` 会阻断写入；完成第 10 步补充优化并生成新的 `review.json` 后，重复同一门禁序列。

```bash
uv run zotero-paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run zotero-paperread validate-trusted-summary <run_dir>/summary.json
PAPER_TITLE="<paper title>"
GENERATED_DATE="<YYYY-MM-DD>"
VERSION_SUFFIX="$(uv run zotero-paperread next-version-suffix <run_dir>/item-details.json --paper-title "$PAPER_TITLE" --generated-date "$GENERATED_DATE")"
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --generated-date "$GENERATED_DATE" --version-suffix "$VERSION_SUFFIX" --output <run_dir>/note.md
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
     - `review_status` 为 `passed` 或 `passed_with_caveats`
     - 没有待处理的 `needs_improvement`
     - `validate-trusted-summary` 已通过
     - 已完成 `preview-note`
     - 已展示 note 预览和目标 Zotero item 标题（target Zotero item title）
   - 如果 `review_status` 为 `failed`，停止并报告审查问题，不写入 Zotero。
   - note 标题由模板生成：`[Codex Summary] <paper title> - YYYY-MM-DD`，同日重复创建时追加 ` (v2)`、` (v3)` 等后缀。
   - 真实写入只能调用 `zotero-mcp write_note`，调用形式为 `write_note(action="create", parentKey=<item key>, content=<note markdown>, tags=["codex-summary","paper-summary"])`。
   - 成功后回读一次 `get_item_details`，确认子笔记已经挂载到目标条目下。

## Better Notes 兼容

生成普通 Zotero 子笔记。Better Notes 如果已安装，可直接显示和管理该 note；本 skill 不依赖 Better Notes。

## V2 仍不做

- 不批量处理 collection。
- 不要求把图片真正嵌入 Zotero note。
- 不更新 Obsidian vault。
- 不维护 PaperGraph。
- 不把 arXiv 源码包作为必需前提；source-first 只是 opportunistic path。
