# Paperread

[English](README.md) | **简体中文**

Paperread 是一个面向 Codex 或 Claude 的 clone-and-run 文献阅读工作流。它使用本地 `paperread` CLI 从论文 PDF 中抽取证据，然后引导 agent 写出中文优先的结构化阅读笔记。

## 功能概览

Paperread 的 public v1 支持两类输入：

- **Zotero 标题**：通过 Zotero MCP 在 Zotero 中定位论文，准备确定性的证据产物，渲染 `note.md` 和 `note.html`，并且只在用户明确要求写入后创建新的 Zotero 子笔记。
- **本地 PDF path**：对不在 Zotero 中的 PDF 运行同一套抽取、总结、审查、lint 和渲染规则，然后在 PDF 同目录写本地输出，不写入 Zotero。

两个工作流默认都会抽取完整 PDF。`summary.json` 中的证据 locator 应引用 `context.md` 或 `figure_context.md`；`section_context.md` 只作为导航辅助。

## Public V1 设置

克隆本仓库，安装 `uv`，然后在仓库根目录运行：

```bash
uv sync
uv run paperread --help
```

Zotero 标题工作流还需要 Zotero Desktop 和可用的 Zotero MCP server。本地 PDF path 工作流不需要 Zotero。

## 作为 Repo-Local Skill 使用

唯一公开的工作流 bundle 是 `skill/`，skill 名称是 `paperread`。Public v1 是 repo-local 的：克隆本仓库，运行 `uv sync`，并在仓库根目录执行命令。不要只复制 `skill/` 目录后期待工作流能独立运行，因为它依赖本仓库的 Python package、templates、lockfile 和 CLI。

将你的 agent 指向 `skill/SKILL.md`。该 skill 会把本地 PDF path 路由到 `skill/references/pdf-path-workflow.md`；其他输入会被视为 Zotero 标题或标题片段，并使用 `skill/references/zotero-workflow.md`。

## Zotero 标题工作流

当输入是 Zotero 标题或标题片段时，使用该路径。

高层流程：

1. 通过 MCP 搜索 Zotero；如果出现 normalized title 相同的重复条目，停止并要求先去重。
2. 用 `create-run` 创建 run directory。
3. 保存原始 MCP response，然后用 `save-item-details` 规范化。
4. 运行 `prepare-item` 生成 `metadata.json`、`extract.json`、`context.md`、`section_context.md`，以及可选的 `figures.json` 和 `figure_context.md`。
5. 如果 `secondary_sources.json` 列出 Extra/web URL，将它们抓取为仅用于 cross-check 的 context：

```bash
mkdir -p <run_dir>/secondary_contexts
node skill/scripts/capture-secondary-url.mjs "<url>" --output <run_dir>/secondary_contexts/secondary-001.md --request-retries 2 --request-retry-ms 500
```

可用的 secondary capture 会使用 `source_status: secondary_context`。不可用的 capture 会使用 `source_status: secondary_context_unavailable`，并包含 `navigation_timeout` 等 warning。Secondary context 不能在 `evidence_summary` 中作为证据引用；它只用于 cross-check 和背景补充。

6. Agent 阅读证据产物，并写出 `summary.json` 和 `review.json`。
7. 运行确定性 review chain：

```bash
uv run paperread validate-summary-json <run_dir>/summary.json
uv run paperread apply-review <run_dir>/summary.json <run_dir>/review.json
uv run paperread lint-summary <run_dir>/summary.json
uv run paperread validate-trusted-summary <run_dir>/summary.json
```

8. 只有在需要 Zotero 输出时，才准备写入候选：

```bash
uv run paperread prepare-write-candidate <run_dir> --paper-title "<paper title>" --generated-date YYYY-MM-DD
```

9. 预览目标 Zotero item、`note.md` 和 `note.html`。
10. 在用户明确要求写入后，通过 Zotero MCP `write_note` 创建新的 Zotero 子笔记。
11. 使用 `verify-zotero-note` 验证已创建的笔记。

## 本地 PDF Path 工作流

当输入是已存在的本地 PDF path 时，使用该路径。

```bash
uv run paperread prepare-pdf "/path/to/paper.pdf"
```

第一次运行会在 PDF 同目录创建 `<pdf_stem>_analysis/`，并将最终 note 目标设为 `<pdf_stem>_note.md`。重复运行会创建 `<pdf_stem>_analysis_v2/`、`<pdf_stem>_note_v2.md` 和更高版本后缀，不覆盖旧输出。

Agent 在 analysis directory 中写入 `summary.json` 和 `review.json`，然后运行：

```bash
uv run paperread validate-summary-json <analysis_dir>/summary.json
uv run paperread apply-review <analysis_dir>/summary.json <analysis_dir>/review.json
uv run paperread lint-summary <analysis_dir>/summary.json
uv run paperread validate-trusted-summary <analysis_dir>/summary.json
uv run paperread prepare-local-note-candidate <analysis_dir> --generated-date YYYY-MM-DD
```

`prepare-local-note-candidate` 会写出 `note.md`、`note.html`、preview files、`note-tags.json`、`local-gate-report.json`，以及 PDF 同目录下的最终 Markdown note。该工作流仅输出本地文件。

## 隐私与本地输出

生成的论文数据默认应视为私有数据。`.gitignore` 会排除常见本地产物：

- `runs/`
- `papers/`
- `<pdf_stem>_analysis/` 和 `<pdf_stem>_analysis_vN/`
- `<pdf_stem>_note.md` 和 `<pdf_stem>_note_vN.md`
- extracted text、summary JSON 和 generated note files
- `.superpowers/`、`.worktrees/` 等 agent/session state

除非已经有意审查过，否则不要提交论文 PDF、抽取文本、Zotero metadata、生成笔记、review reports 或本地 run artifacts。

## 验证

在认为变更完成前运行：

```bash
uv run pytest
uv run paperread --help
uv run paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/paperread-extract.json
```

如果 Codex 用户有可用的 bundled `skill-creator` validator，也可以选择用其本地 `quick_validate.py` 脚本验证 `skill/`。该 validator 不是使用本仓库的必要条件。

## 安全边界

- 默认先 dry-run 和 preview，再写入。
- Zotero 写入只能通过 Zotero MCP `write_note`，且必须有用户明确写入意图。
- Zotero local API 和 SQLite 在本项目中只读。
- PDF path workflow 不能写入 Zotero，不能调用 `refresh-live-notes`，不能创建 `write-payload.json`。
- 渲染出的笔记正文应中文优先，同时保留论文标题、人名、公式、方法名、单位、证据 locator 和 tag key。
