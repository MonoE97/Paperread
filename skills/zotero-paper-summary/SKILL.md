---
name: zotero-paper-summary
description: 输入 Zotero 论文标题，使用 Zotero MCP 定位条目，抽取 PDF，生成中文结构化论文总结，并在明确写入时创建 Zotero 子笔记。
---

# Zotero Paper Summary

## 目标

把 Zotero 中的一篇论文转换为中文结构化研究笔记。默认只 dry-run；只有用户明确要求写入 Zotero 时，才创建子笔记。

## 输入

接受 Zotero 条目标题或标题片段。V1 只处理单篇论文，不处理 collection 批量任务。

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

2. 获取条目详情：
   - 读取 title、creators、date、DOI、url、zoteroUrl、attachments。
   - 将原始 item details 保存到临时文件，例如 `/tmp/zotero-paperread-run/item-details.json`。

3. 准备 bundle：
   - 运行：

```bash
uv run zotero-paperread prepare-item /tmp/zotero-paperread-run/item-details.json --workdir /tmp/zotero-paperread-run --max-pages 15
```

   - 该命令会生成：
     - `metadata.json`
     - `extract.json`
     - `context.md`
   - `context.md` 是默认总结输入源，包含元数据、摘要、抽取告警和 PDF 正文。
   - 无 PDF 时也继续工作，只是在 `extract.json` 中记录 `missing_pdf_attachment`。

4. 生成 summary JSON：
   - 输出必须包含这些字段：

```json
{
  "one_sentence_summary": "",
  "abstract_translation": "",
  "key_points": [],
  "research_question": "",
  "method": "",
  "experiments": "",
  "contributions": [],
  "limitations": [],
  "ai4s_relevance": "",
  "follow_up_keywords": [],
  "quality_score": "",
  "extraction_warnings": []
}
```

5. 分析要求：
   - 参考 `evil-read-arxiv` 的 `paper-analyze` 思路，覆盖摘要翻译、研究背景、研究问题、方法、实验、贡献、局限、相关方向定位。
   - 公式使用 Markdown LaTeX：行内 `$...$`，块级 `$$...$$`。
   - 不编造论文没有支持的数据、实验或结论。
   - 对 AI+物理/材料的启发必须独立成节，结合用户研究方向给出判断。
   - 如果条目是综述、Perspective、评论文章或方法综述，明确按“综述类文献”处理，不要虚构本文原创实验。

6. 渲染和验证 note：

```bash
uv run zotero-paperread render-note /tmp/zotero-paperread-run/metadata.json /tmp/zotero-paperread-run/summary.json --output /tmp/zotero-paperread-run/note.md
uv run zotero-paperread validate-note /tmp/zotero-paperread-run/note.md
uv run zotero-paperread preview-note /tmp/zotero-paperread-run/note.md
```

7. 写入 Zotero：
   - 只有用户明确要求“写入”“创建 note”“保存到 Zotero”等动作时执行。
   - note 标题由模板生成：`[Codex Summary] <paper title> - YYYY-MM-DD`。
   - 调用 `write_note(action="create", parentKey=<item key>, content=<note markdown>, tags=["codex-summary","paper-summary"])`。
   - 成功后回读一次 `get_item_details`，确认子笔记已经挂载到目标条目下。

## Better Notes 兼容

生成普通 Zotero 子笔记。Better Notes 如果已安装，可直接显示和管理该 note；本 skill 不依赖 Better Notes。

## V1 不做

- 不批量处理 collection。
- 不抽取和插入图片。
- 不更新 Obsidian vault。
- 不维护 PaperGraph。
- 不下载 arXiv 源码包作为必需步骤。
