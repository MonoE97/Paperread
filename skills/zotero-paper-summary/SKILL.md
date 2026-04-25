---
name: zotero-paper-summary
description: 输入 Zotero 论文标题，使用 Zotero MCP 定位条目，抽取 PDF，生成中文结构化论文总结，并在明确写入时创建 Zotero 子笔记。
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
```

## 目标

把 Zotero 中的一篇论文转换为中文结构化研究笔记。默认只 dry-run；当用户明确要求“写入笔记”“写回 Zotero”“创建 note”“保存到 Zotero”等动作时，执行分析并创建子笔记。

## 输入

接受 Zotero 条目标题或标题片段。V2 仍只处理单篇论文，不处理 collection 批量任务。

## 工具边界

- 用 `zotero-mcp search_library` 搜索条目。
- 用 `zotero-mcp get_item_details` 获取元数据和 PDF attachment path。
- 用本项目 Python CLI 抽取 PDF 与渲染 note。
- 用 Zotero MCP 在显式写入步骤创建 Zotero 子笔记。
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
   - 读取 title、creators、date、DOI、url、zoteroUrl、attachments。
   - 将原始 item details 保存到返回的 run 目录中的 `item-details.json`。

4. 准备 bundle：
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
   - 无 PDF 时也继续工作，只是在 `extract.json` 中记录 `missing_pdf_attachment`。

5. 生成 summary JSON：
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
  "quality_score": "",
  "extraction_warnings": []
}
```

6. 分析要求：
   - 参考 `evil-read-arxiv` 的 `paper-analyze` 思路，覆盖摘要翻译、研究背景、研究问题、方法、实验、贡献、局限、相关方向定位。
   - 参考 `extract-paper-images` 的思想，但改成 Zotero-first：优先使用 `figure_context.md` 中最重要的 1-4 张关键图做“图文联合分析”。
   - 公式使用 Markdown LaTeX：行内 `$...$`，块级 `$$...$$`。
   - 不编造论文没有支持的数据、实验或结论。
   - 对 AI+物理/材料的启发必须独立成节，结合用户研究方向给出判断。
   - 如果条目是综述、Perspective、评论文章或方法综述，明确按“综述类文献”处理，不要虚构本文原创实验。
   - `figure_overview` 必须解释关键图在整篇论文中的证据角色。
   - `key_figures` 中每个对象都必须解释：
     - 这张图展示什么
     - 为什么它重要
     - 它支撑了哪条核心结论或方法理解

7. 渲染和验证 note：

```bash
uv run zotero-paperread finalize-note <run_dir>/metadata.json <run_dir>/summary.json --output <run_dir>/note.md
uv run zotero-paperread preview-note <run_dir>/note.md
```

   - 生成的 `summary.json`、`note.md` 和预览输出都保留在同一个 run 目录里，便于审计和复查。
   - 推荐使用 `finalize-note`，它会按正确顺序执行 `render-note -> validate-note`。
   - 如果手动拆开执行，必须按 `render-note -> validate-note -> preview-note` 串行执行，不能并行调度。

8. 写入 Zotero：
   - 只有用户明确要求“写入”“写入笔记”“写回 Zotero”“创建 note”“保存到 Zotero”等动作时执行。
   - note 标题由模板生成：`[Codex Summary] <paper title> - YYYY-MM-DD`。
   - 调用 `write_note(action="create", parentKey=<item key>, content=<note markdown>, tags=["codex-summary","paper-summary"])`。
   - 成功后回读一次 `get_item_details`，确认子笔记已经挂载到目标条目下。

## Better Notes 兼容

生成普通 Zotero 子笔记。Better Notes 如果已安装，可直接显示和管理该 note；本 skill 不依赖 Better Notes。

## V2 仍不做

- 不批量处理 collection。
- 不要求把图片真正嵌入 Zotero note。
- 不更新 Obsidian vault。
- 不维护 PaperGraph。
- 不把 arXiv 源码包作为必需前提；source-first 只是 opportunistic path。
