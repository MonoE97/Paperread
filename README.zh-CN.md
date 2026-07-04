# paper_reader

[English](README.md) | **简体中文**

paper_reader 是面向 Codex 或 Claude 的自包含 skill repo。它包含两个可安装 skill source：

- `paper_reader/` 安装为 `paper_reader`，负责单篇深度阅读。
- `paper_reader_batch/` 安装为 `paper_reader_batch`，负责批量调度和轻量报告。

安装时把对应 source 目录复制到目标 skill 目录，在安装后的 skill root 中运行命令；仓库根目录只保留维护文档和发布说明。

不要在 `paper_reader/` 或 `paper_reader_batch/` 内放 `README.md`；skill 内只保留 `SKILL.md`、直接链接的 `references/`、bundled scripts、代码、测试、模板、依赖元数据和 fixtures。

## 安装

复制 skill 前先安装 `uv`。可使用官方 installer 或包管理器；常见方式：

```bash
# 方式 A：standalone installer
curl -LsSf https://astral.sh/uv/install.sh | sh

# 方式 B：Homebrew
brew install uv

uv --version
```

Windows 和其他包管理器安装方式见官方 `uv` 安装文档：<https://docs.astral.sh/uv/getting-started/installation/>。

如果 `uv sync --locked` 找不到 Python `>=3.13`，先安装由 `uv` 管理的解释器后重试：

```bash
uv python install 3.13
```

Codex personal 单篇 skill：

```bash
install_dir="${CODEX_HOME:-$HOME/.codex}/skills/paper_reader"
test ! -e "$install_dir" || { echo "target exists: $install_dir"; exit 1; }
mkdir -p "$(dirname "$install_dir")"
cp -R /path/to/paper_reader/paper_reader "$install_dir"
cd "$install_dir"
uv sync --locked
uv run paper_reader --help
```

Codex personal batch skill：

```bash
install_dir="${CODEX_HOME:-$HOME/.codex}/skills/paper_reader_batch"
test ! -e "$install_dir" || { echo "target exists: $install_dir"; exit 1; }
mkdir -p "$(dirname "$install_dir")"
cp -R /path/to/paper_reader/paper_reader_batch "$install_dir"
cd "$install_dir"
uv sync --locked
uv run paper_reader_batch --help
```

Claude Code personal 单篇 skill：

```bash
install_dir="$HOME/.claude/skills/paper_reader"
test ! -e "$install_dir" || { echo "target exists: $install_dir"; exit 1; }
mkdir -p "$(dirname "$install_dir")"
cp -R /path/to/paper_reader/paper_reader "$install_dir"
cd "$install_dir"
uv sync --locked
uv run paper_reader --help
```

Claude Code personal batch skill：

```bash
install_dir="$HOME/.claude/skills/paper_reader_batch"
test ! -e "$install_dir" || { echo "target exists: $install_dir"; exit 1; }
mkdir -p "$(dirname "$install_dir")"
cp -R /path/to/paper_reader/paper_reader_batch "$install_dir"
cd "$install_dir"
uv sync --locked
uv run paper_reader_batch --help
```

如果目标 `paper_reader/` 或 `paper_reader_batch/` 目录已经存在，先停止，不要直接覆盖复制。替换已安装 skill 必须是用户明确批准的操作；盲目 `cp -R` 可能生成无法被发现的嵌套目录。

第一次运行 `uv sync --locked` 会根据对应 skill root 的 `uv.lock` 初始化安装后的本地环境。更新已复制的 skill 目录后，也应重新运行一次。

## Zotero MCP 设置

Zotero-backed workflow 需要先安装 Zotero Desktop 和 Zotero MCP plugin，agent 才能搜索文献库或调用 `write_note`。按 [cookjohn/zotero-mcp](https://github.com/cookjohn/zotero-mcp#readme) 安装并启用：

1. 从该仓库 releases 下载最新的 `zotero-mcp-plugin-*.xpi`。
2. 在 Zotero 中通过 `Tools -> Add-ons` 安装 `.xpi`，然后重启 Zotero。
3. 打开 `Preferences -> Zotero MCP Plugin`，启用 integrated server，并生成 client configuration。
4. 使用生成的 Streamable HTTP MCP 配置，或直接配置本地 endpoint：`http://127.0.0.1:23120/mcp`。

该插件内置 MCP server，不需要额外启动独立的 Zotero MCP server 进程。paper_reader 把 Zotero local API 和 SQLite 视为只读；真正写入只允许走 Zotero MCP `write_note`。

## Skill 使用方式

### Use `paper_reader`

`paper_reader` 用于一次分析一篇论文。

- Zotero 标题或标题片段：让 agent 使用 `$paper_reader` 和论文标题。agent 会通过 Zotero MCP 搜索文献库，准备 evidence artifacts，渲染 `note.md` 与 `note.html`，预览候选笔记，只在明确写入意图后通过 MCP `write_note` 写入，并回读校验新建笔记。
- 本地 PDF path：给出绝对或相对 `.pdf` 路径。skill 会在 PDF 同目录写 `<pdf_stem>_analysis/` 和 `<pdf_stem>_note.md`，不会搜索 Zotero 中是否有相同文献，也不会写 Zotero。
- 本地目录 path：使用 `paper_reader_batch` 的 local PDF folder workflow。已存在的本地路径不是 Zotero 标题片段。

安装后的常用命令：

```bash
uv run paper_reader --help
uv run paper_reader prepare-pdf "/abs/path/to/paper.pdf"
```

### Use `paper_reader_batch`

`paper_reader_batch` 用于一次处理多篇论文。必须同时安装 `paper_reader` 和 `paper_reader_batch`；batch skill 只负责调度与报告，单篇阅读仍由 `$paper_reader` 负责。

典型 batch CLI 形态：

```bash
uv run paper_reader_batch manifest from-zotero-titles titles.txt --batch-title "my batch" --output manifest.json
uv run paper_reader_batch init --manifest manifest.json
uv run paper_reader_batch next <batch_run_dir> --limit 3
uv run paper_reader_batch next-write <batch_run_dir> --limit 1
uv run paper_reader_batch record-write <batch_run_dir> <item_id> --result write-result.json
uv run paper_reader_batch report <batch_run_dir>
```

Zotero-backed items 默认 `write_policy=zotero_write`。需要 dry-run 时，在 manifest builder 中传 `--write-policy prepare_only`。PDF batch items 仍然只输出本地文件，并跳过 Zotero 搜索和去重检查。

## 工作流

paper_reader 支持两类输入：

- **Zotero 标题或标题片段**：通过 Zotero MCP 定位论文，准备确定性的证据产物，渲染 `note.md` 和 `note.html`，并且只在用户明确要求写入后创建新的 Zotero 子笔记。
- **本地 PDF path**：对本地 PDF 运行同一套抽取、总结、审查、lint 和渲染门禁，然后在 PDF 同目录写本地输出，不写入 Zotero，也不检查 Zotero 中是否已有同一篇论文。

本地 PDF path 和目录 path 输入会跳过 Zotero 搜索和去重检查。已存在的本地路径不是 Zotero 标题片段；目录路径交给 `paper_reader_batch manifest from-pdf-folder`，默认不递归，只有显式传 `--recursive` 才递归。

两个工作流默认都会抽取完整 PDF。最终 `evidence_summary` locator 必须使用以下 canonical 格式之一：`context.md page <N>`、`context.md page <N> section <Section Name>`、`context.md page <N> section <Section Name> table_candidate <N>` 或 `figure_context.md <figure_id>`。裸 `context.md` / `figure_context.md`、`page 3 method section` 这类散文式 locator、`section_context.md` 和 secondary context 路径都无效。`section_context.md` 只作为导航辅助。通过 `scripts/capture-secondary-url.mjs` 抓取的 secondary web context 只用于 cross-check，不能在 `evidence_summary` 中作为证据引用。

paper_reader_batch 支持四类批量输入：Zotero collection inventory、多个 Zotero 标题、本地 PDF 文件夹、多个 PDF path。它会归一化为 manifest，把每篇交给 `$paper_reader`，Zotero-backed items 默认使用 `zotero_write`：生成写入候选后通过 `next-write` 串行交给外层 agent 调 Zotero MCP 写入、只读校验，再用 `record-write` 记录结果。PDF folder/path items 是 `pdf_path` items，`expected_output=local_note`；它们不做 Zotero 搜索、去重检查、`next-write` 或 write-through。需要 dry-run 时显式传 `--write-policy prepare_only`。Codex 默认并发数为 3；外层 agent 并行不可用时，可用 `prepare-local-pdfs` 先并发预抽取本地 PDF bundle，再由单个 agent 顺序继续深读。每篇 30 秒结果直接从单篇 note 的 `30 秒结论` 行提取，batch 不再重新总结论文。

## 产物位置

- Zotero 标题工作流的本地产物默认写到 `<skill_root>/runs/YYYY-MM-DD/<title-slug>/`。准备写入候选时，会在同一目录生成 `note.md`、`note.html`、`gate-report.json` 和 `write-payload.json`，然后才可能写入 Zotero。
- 本地 PDF path 工作流的产物默认写在 PDF 同目录：`<pdf_stem>_analysis/` 保存分析产物，`<pdf_stem>_note.md` 是最终 Markdown 笔记。已有输出不会覆盖，会自动使用 `_v2`、`_v3` 等后缀。
- Batch workflow 的本地产物默认写到 `<paper_reader_batch_root>/runs/YYYY-MM-DD/<batch-slug>/`，包含 `manifest.json`、`state.json`、`items/*.json`、`items/*.write.json`、`batch-report.json` 和 `batch-report.md`。单篇产物仍归 `paper_reader` 所有；batch 只保存索引、local-only path、Zotero note key 和 verify report path。

## 运行要求

- 安装和运行 CLI：`uv`，以及可由 `uv` 使用的 Python `>=3.13`；如果没有兼容解释器，用 `uv python install 3.13` 补齐。
- 本地 PDF 工作流：不需要 Zotero，也不做 Zotero 去重检查。图表抽取在元数据或 PDF 文件名出现 arXiv ID 时，可能尝试下载 arXiv source；该请求有有界 network timeout，失败后会回退到只基于 PDF 的抽取。
- Zotero 标题工作流：需要 Zotero Desktop，以及 Zotero MCP tools 或本地 MCP endpoint。
- Secondary web context capture：仅在使用该可选路径时需要 Node.js 和可访问的 CDP helper。
- Batch workflow：需要已安装的 `paper_reader` 和 `paper_reader_batch`；batch validation 会在派发前检查配置的 `paper_reader` root。

## 验证

在安装后的目录或源码 `paper_reader/` 目录中运行：

```bash
uv sync --locked
uv run pytest
uv run paper_reader --help
uv run paper_reader extract-pdf tests/fixtures/minimal.pdf --output /tmp/paper_reader-extract.json
uv run python scripts/validate-skill.py .
```

维护者在认为 `paper_reader/` 已自包含前，还应把它复制到仓库外的临时目录，并在复制后的目录中运行同一组验证。

在安装后的目录或源码 `paper_reader_batch/` 目录中运行：

```bash
uv sync --locked
uv run pytest
uv run paper_reader_batch --help
uv run python scripts/validate-skill.py .
```

维护者在认为 `paper_reader_batch/` 已自包含前，也应把它复制到仓库外的临时目录，并在复制后的目录中运行同一组验证。

## 安全边界

- 每次 Zotero 写入前必须先 preview 并通过单篇 write gate；批量 dry-run 显式使用 `--write-policy prepare_only`。
- Zotero 写入只能通过 Zotero MCP `write_note`，且必须有用户明确写入意图。
- Zotero local API 和 SQLite 只读。
- 本地 PDF path workflow 只能输出本地文件；不能搜索 Zotero，不能做去重检查，不能写入 Zotero，不能调用 `refresh-live-notes`，不能创建 `write-payload.json`。
- Zotero-backed batch workflow 默认 `zotero_write`：batch CLI 只输出待写项并记录校验，外层 agent 调 Zotero MCP `write_note`；PDF batch items 仍然 local-only。需要 dry-run 时传 `--write-policy prepare_only`。
- 渲染出的笔记正文应中文优先，同时保留论文标题、人名、公式、方法名、单位、证据 locator 和 tag key。
