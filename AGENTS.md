# AGENTS.md

## 项目目标

本项目维护一个自包含的 Paperread skill repo。唯一可安装运行产物是 `skill/`：复制到 Codex 或 Claude 的 skills 目录并命名为 `paperread` 后，用户应能在安装后的 skill root 内运行 `uv sync --locked`、`uv run paperread ...`，使用 Zotero 标题工作流和本地 PDF path 工作流。仓库根目录只承担维护文档、发布说明和规划记录职责，不是运行时 Python project。

## 目录约定

- `skill/SKILL.md`: skill 入口，只保留触发、路由和核心安全边界。
- `skill/agents/openai.yaml`: Codex UI metadata；更新 `SKILL.md` 后检查是否同步。
- `skill/src/paperread/`: Python package code for deterministic CLI/tooling logic.
- `skill/tests/`: pytest tests; never perform real Zotero writes.
- `skill/templates/`: Jinja2 note template.
- `skill/references/`: workflow and schema references loaded by the skill when needed.
- `skill/scripts/`: bundled helper scripts, including portable skill validation.
- `skill/pyproject.toml` and `skill/uv.lock`: dependency and lock metadata for the installed skill root.
- `README.md`: English public entry point; explain install from `skill/`, not root runtime usage.
- `README.zh-CN.md`: Chinese README paired with `README.md`; keep workflow commands, safety boundaries, and public claims synchronized when either README changes.
- `docs/superpowers/specs/`: planning and review artifacts.
- `docs/superpowers/scripts/`: maintainer-only validation scripts.
- `AGENTS.md`: agent behavior and safety rules.

Do not add `README.md`, `INSTALLATION_GUIDE.md`, `QUICK_REFERENCE.md`, or `CHANGELOG.md` inside `skill/`.

## 运行产物与证据边界

- `prepare-item` 默认生成 `context.md`，并在结构化抽取可用时生成 `section_context.md`；`section_context.md` 只用于帮助 Codex 定位章节、表格候选和值候选。
- PDF path workflow 使用 `prepare-pdf <pdf_path>`，首次在 PDF 同目录生成 `<pdf_stem>_analysis/` 和 `<pdf_stem>_note.md`，重复运行使用 `<pdf_stem>_analysis_v2/`、`<pdf_stem>_note_v2.md` 等后缀，不覆盖旧输出。
- `section_context.md` is not a canonical evidence source；最终 `evidence_summary` locator 必须引用 `context.md` 或 `figure_context.md`，例如 `context.md page 3 section Methods`、`context.md page 6 section Results table_candidate 1`、`figure_context.md fig_p4_1`。
- 用户提供微信公众号、新闻稿、博客等网页时，只作为 secondary context capture，用于 cross-check 和补充背景；`evidence_summary` 只能引用 `context.md` 和 `figure_context.md`。Secondary context must not cite secondary context in `evidence_summary`.

## 阅读笔记语言规则

- Zotero 阅读笔记正文默认使用中文描述；除论文题名、作者名、机构名、化学式、材料/模型/方法专名、缩写、单位、引用 locator、代码式 key 和 Zotero tags 外，不要用整句英文解释。
- 会渲染到 `note.md` / `note.html` 的自由文本字段必须优先中文化，包括 `research_object`、`main_risk_short`、`method_modules`、`workflow_steps`、`technical_details`、`key_figures.analysis`、`key_figures.why_it_matters`、缺少 `analysis` 时会作为 fallback 渲染的 `key_figures.caption`、`author_stated_limitations`、`inferred_limits` 和 `applicability_limits`。
- `note_labels` 和 Zotero metadata tags 保持英文规范 key；它们是机器标签，不是正文描述。
- `lint-summary` 会把渲染字段中的整段英文 prose 视为写入阻断项；真实写入前必须修正到 `gate-report.json` 为 `write_ready`。

## 环境与依赖

- Python 环境必须用 `uv` 管理。
- 默认在 `skill/` 内执行命令，使用 `uv run`。
- 缺少项目依赖时在 `skill/` 内使用 `uv add` 或 `uv add --dev`，不使用 `pip install`、`conda install` 或全局安装。
- 不修改系统 Python、conda base 环境或 shell 全局配置。

## Git 与发布

- 当前项目是本地 Git repo，默认分支 `main`。
- 功能开发在 feature branch 或 worktree 中进行。
- 可以创建本地 commit。
- 禁止在未获用户明确确认前执行 `git push`、创建 GitHub remote、公开发布或部署。
- `.DS_Store`、虚拟环境、缓存、本地预览文件、PDF 分析目录和生成笔记必须被 `.gitignore` 忽略。

## Zotero 边界

- 读取 Zotero 信息优先使用 `zotero-mcp`。
- 写入 Zotero 只能通过 `zotero-mcp write_note`，且必须由用户明确触发。
- 禁止直接修改 Zotero SQLite、Zotero storage 元数据、Better Notes 配置或 Better Notes 模板。
- Zotero local API and SQLite are read-only in this project；只允许用于 live 子笔记标题/正文读取和写后验证，禁止通过 Zotero local API、SQLite 或其他非 MCP 路径写入 Zotero。
- dry-run 必须只输出预览，不写 Zotero。

## Better Notes 策略

Better Notes 是可选阅读增强层。Paperread 生成 Zotero 子笔记，保证 Better Notes 能正常显示；不调用 `Zotero.BetterNotes.api`，不依赖 Better Notes 存在。

## 验证命令

改完运行时代码、测试、模板、reference、dependency 或 `SKILL.md` 后，从 `skill/` 运行：

```bash
uv sync --locked
uv run pytest
uv run paperread --help
uv run paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/paperread-extract.json
uv run python scripts/validate-skill.py .
```

涉及根 README、中文 README、AGENTS 或安装说明时，还必须在仓库根目录运行：

```bash
python docs/superpowers/scripts/validate-root-docs.py
```

V2 发布前必须把 `skill/` 复制到仓库外临时目录并在复制后的目录中运行同一组 skill-root 验证，证明 `skill/` 自包含。

## 写入规则

- 默认先 dry-run。
- PDF path workflow 是 local-output only；禁止调用 `refresh-live-notes`，禁止生成 `write-payload.json`，禁止写 Zotero。PDF 本地笔记必须通过 `validate-summary-json -> apply-review -> lint-summary -> validate-trusted-summary -> prepare-local-note-candidate`，最终 Markdown 写到 PDF 同目录的 `<pdf_stem>_note.md` 或版本后缀路径。
- Zotero exact 搜索出现多个 normalized title 相同的条目时，停止分析和写入，要求用户先在 Zotero 去重；不要替用户选择父条目。
- MCP 原始 `get_item_details` 响应必须先落盘，再用 `save-item-details` 生成规范化的 `item-details.json`，后续本地命令只读规范化文件。
- 当 MCP 响应缺少 `extra` 时，`save-item-details` 可用只读 Zotero SQLite fallback 补齐 `Extra` / `其他`；成功补齐只记录 `_paperread.enrichment.extra.diagnostics`，不写入 `_paperread.warnings`；缺失、不可读或找不到条目才保留 warning。
- `prepare-item`、`extract-pdf`、`extract-figures` 默认处理完整 PDF；只有用户明确要求快速调试、预览或截断抽取时才传 `--max-pages <N>`。
- 真实写入 Zotero 前，必须展示 `note.md` 与 `note.html` 预览和目标 Zotero item 标题。
- 真实写入 Zotero 前必须完成最终门禁：推荐运行 `prepare-write-candidate`；等价底层链路为 `validate-summary-json -> apply-review -> lint-summary -> validate-trusted-summary -> refresh-live-notes -> next-version-suffix -> finalize-note --html-output -> note-tags -> preview-note note.md/note.html -> gate-run -> prepare-write-payload`，且 `gate-report.json` 必须为 `write_ready`。
- `prepare-write-candidate` 是日常写入准备入口；它会删除 stale `write-payload.json`，只在 gate 为 `write_ready` 时重新生成 payload。
- `prepare-write-payload` 的输出必须是当前 run 目录下的 `write-payload.json`；禁止把 payload 写到 `gate-report.json`、`note.html`、非 `write-payload.json` 文件名或 gate run 目录之外。
- 真实写入 Zotero 时，只能调用 `zotero-mcp write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`；`content` 必须使用 `note.html` 的内容，避免 Markdown 表格在 Zotero 中被当作普通文本。
- 真实写入 Zotero 后必须用只读 `verify-zotero-note` 回读校验 parent、标题、必需章节、标签、最小长度和 `contentSha256`；`contentSha256` 使用项目内 canonical hash，不要用临时 shell hash 替代。
- single-paper summary writes always create a new versioned Zotero child note；不 update 既有 `[Codex Summary]` 总结 note。真实写入前必须运行 `prepare-write-candidate` 或等价底层链路，用只读 live note refresh 计算同日后缀。
