# AGENTS.md

## 项目目标

本项目实现 Zotero-first 文献总结工作流：输入 Zotero 中的文章标题，Codex 通过 Zotero MCP 定位条目，使用本地 Python 工具默认抽取完整 PDF 内容、章节上下文和表格/数值候选，生成中文结构化论文总结，并在用户明确要求写入时创建 Zotero 子笔记。审计源保留为 `note.md`，真实写入 Zotero 时使用由同一份 Markdown 转换出的 `note.html`。

## 目录约定

- `src/zotero_paperread/`：Python 包代码，只放确定性工具逻辑。
- `tests/`：pytest 测试，禁止真实写入 Zotero。
- `templates/`：Jinja2 note 模板。
- `skills/`：Codex skill 定义。
- `docs/references/`：外部项目参考、设计取舍记录和可复用 runbook。
- `docs/superpowers/plans/`：实施计划。

## 运行产物与证据边界

- `prepare-item` 默认生成 `context.md`，并在结构化抽取可用时生成 `section_context.md`；`section_context.md` 只用于帮助 Codex 定位章节、表格候选和值候选。
- `section_context.md` is not a canonical evidence source；它只辅助阅读定位。最终 `evidence_summary` locator 必须引用 `context.md` 或 `figure_context.md`，例如 `context.md page 3 section Methods`、`context.md page 6 section Results table_candidate 1`、`figure_context.md fig_p4_1`。
- 用户提供微信公众号、新闻稿、博客等网页时，只作为二级材料 capture，用于 cross-check 和补充背景；`evidence_summary` 只能引用 `context.md` 和 `figure_context.md`。

## 环境与依赖

- Python 环境必须用 `uv` 管理。
- 默认执行命令使用 `uv run`。
- 缺少项目依赖时使用 `uv add` 或 `uv add --dev`，不使用 `pip install`、`conda install` 或全局安装。
- 不修改系统 Python、conda base 环境或 shell 全局配置。

## Git 与发布

- 当前项目是本地 Git repo，默认分支 `main`。
- 功能开发在 feature branch 或 worktree 中进行。
- 可以创建本地 commit。
- 禁止在未获用户明确确认前执行 `git push`、创建 GitHub remote、公开发布或部署。
- `.DS_Store`、虚拟环境、缓存和本地预览文件必须被 `.gitignore` 忽略。

## Zotero 边界

- 读取 Zotero 信息优先使用 `zotero-mcp`。
- 写入 Zotero 只能通过 `zotero-mcp write_note`，且必须由用户明确触发。
- 禁止直接修改 Zotero SQLite、Zotero storage 元数据、Better Notes 配置或 Better Notes 模板。
- dry-run 必须只输出预览，不写 Zotero。

## Better Notes 策略

Better Notes 是可选阅读增强层。V1 生成 Zotero 子笔记，保证 Better Notes 能正常显示；不调用 `Zotero.BetterNotes.api`，不依赖 Better Notes 存在。

## 验证命令

改完代码后运行：

```bash
uv run pytest
uv run zotero-paperread --help
```

涉及 PDF 抽取时额外运行：

```bash
uv run zotero-paperread extract-pdf tests/fixtures/minimal.pdf --output /tmp/zotero-paperread-extract.json
```

## 写入规则

- 默认先 dry-run。
- Zotero exact 搜索出现多个 normalized title 相同的条目时，停止分析和写入，要求用户先在 Zotero 去重；不要替用户选择父条目。
- MCP 原始 `get_item_details` 响应必须先落盘，再用 `save-item-details` 生成规范化的 `item-details.json`，后续本地命令只读规范化文件。
- 当 MCP 响应缺少 `extra` 时，`save-item-details` 可用只读 Zotero SQLite fallback 补齐 `Extra` / `其他`；成功补齐只记录 `_paperread.enrichment.extra.diagnostics`，不写入 `_paperread.warnings`；缺失、不可读或找不到条目才保留 warning。
- `prepare-item`、`extract-pdf`、`extract-figures` 默认处理完整 PDF；只有用户明确要求快速调试、预览或截断抽取时才传 `--max-pages <N>`。
- 真实写入 Zotero 前，必须展示 `note.md` 与 `note.html` 预览和目标 Zotero item 标题。
- 真实写入 Zotero 前必须完成最终门禁：推荐运行 `prepare-write-candidate`；等价底层链路为 `validate-summary-json -> apply-review -> lint-summary -> validate-trusted-summary -> refresh-live-notes -> next-version-suffix -> finalize-note --html-output -> note-tags -> preview-note note.md/note.html -> gate-run -> prepare-write-payload`，且 `gate-report.json` 必须为 `write_ready`。
- 真实写入 Zotero 时，只能调用 `zotero-mcp write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`；`content` 必须使用 `note.html` 的内容，避免 Markdown 表格在 Zotero 中被当作普通文本。
- 真实写入 Zotero 后必须用只读 `verify-zotero-note` 回读校验 parent、标题、必需章节、标签、最小长度和 `contentSha256`。
- single-paper summary writes always create a new versioned Zotero child note；不 update 既有 `[Codex Summary]` 总结 note。真实写入前必须运行 `prepare-write-candidate` 或等价底层链路，用只读 live note refresh 计算同日后缀；同日重复创建时使用 `[Codex Summary] <paper title> - YYYY-MM-DD (v2)`、`(v3)` 等标题后缀创建新版本。
- Zotero local API is read-only in this project；只允许用于 live 子笔记标题/正文读取和写后验证，禁止通过 Zotero local API、SQLite 或其他非 MCP 路径写入 Zotero。
