# Paperread

[English](README.md) | **简体中文**

Paperread 是面向 Codex 或 Claude 的自包含 skill bundle。可安装产物只有本仓库的 `skill/` 目录。安装时把它复制到名为 `paperread` 的目标目录，在安装后的 skill root 中运行命令；仓库根目录只保留维护文档和发布说明。

不要在 `skill/` 内放 `README.md`；skill 内只保留 `SKILL.md`、直接链接的 `references/`、bundled scripts、代码、测试、模板、依赖元数据和 fixtures。

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

Codex personal skill：

```bash
install_dir="${CODEX_HOME:-$HOME/.codex}/skills/paperread"
test ! -e "$install_dir" || { echo "target exists: $install_dir"; exit 1; }
mkdir -p "$(dirname "$install_dir")"
cp -R /path/to/Paperread/skill "$install_dir"
cd "$install_dir"
uv sync --locked
uv run paperread --help
```

Claude Code personal skill：

```bash
install_dir="$HOME/.claude/skills/paperread"
test ! -e "$install_dir" || { echo "target exists: $install_dir"; exit 1; }
mkdir -p "$(dirname "$install_dir")"
cp -R /path/to/Paperread/skill "$install_dir"
cd "$install_dir"
uv sync --locked
uv run paperread --help
```

如果目标 `paperread/` 目录已经存在，先停止，不要直接覆盖复制。替换已安装 skill 必须是用户明确批准的操作；盲目 `cp -R` 可能生成无法被发现的嵌套目录。

第一次运行 `uv sync --locked` 会根据 `skill/uv.lock` 初始化安装后 skill 的本地环境。更新已复制的 skill 目录后，也应重新运行一次。

## 工作流

Paperread 支持两类输入：

- **Zotero 标题或标题片段**：通过 Zotero MCP 定位论文，准备确定性的证据产物，渲染 `note.md` 和 `note.html`，并且只在用户明确要求写入后创建新的 Zotero 子笔记。
- **本地 PDF path**：对本地 PDF 运行同一套抽取、总结、审查、lint 和渲染门禁，然后在 PDF 同目录写本地输出，不写入 Zotero。

两个工作流默认都会抽取完整 PDF。最终 `evidence_summary` locator 必须引用 `context.md` 或 `figure_context.md`；`section_context.md` 只作为导航辅助。通过 `scripts/capture-secondary-url.mjs` 抓取的 secondary web context 只用于 cross-check，不能在 `evidence_summary` 中作为证据引用。

## 运行要求

- 安装和运行 CLI：`uv`，以及可由 `uv` 使用的 Python `>=3.13`；如果没有兼容解释器，用 `uv python install 3.13` 补齐。
- 本地 PDF 工作流：不需要 Zotero。
- Zotero 标题工作流：需要 Zotero Desktop，以及 Zotero MCP tools 或本地 MCP endpoint。
- Secondary web context capture：仅在使用该可选路径时需要 Node.js 和可访问的 CDP helper。

## 验证

在安装后的目录或源码 `skill/` 目录中运行：

```bash
uv sync --locked
uv run pytest
uv run paperread --help
uv run paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/paperread-extract.json
uv run python scripts/validate-skill.py .
```

维护者在认为 `skill/` 已自包含前，还应把它复制到仓库外的临时目录，并在复制后的目录中运行同一组验证。

## 安全边界

- 默认先 dry-run 和 preview，再写入。
- Zotero 写入只能通过 Zotero MCP `write_note`，且必须有用户明确写入意图。
- Zotero local API 和 SQLite 只读。
- 本地 PDF path workflow 只能输出本地文件；不能写入 Zotero，不能调用 `refresh-live-notes`，不能创建 `write-payload.json`。
- 渲染出的笔记正文应中文优先，同时保留论文标题、人名、公式、方法名、单位、证据 locator 和 tag key。
