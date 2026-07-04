# AGENTS.md

## 项目目标

本项目维护一个自包含的 paper_reader skill repo。可安装运行产物是两个 skill source：`paper_reader/` 复制到 Codex 或 Claude 的 skills 目录并命名为 `paper_reader` 后，用户应能在安装后的 skill root 内运行 `uv sync --locked`、`uv run paper_reader ...`，使用 Zotero 标题工作流和本地 PDF path 工作流；`paper_reader_batch/` 复制并命名为 `paper_reader_batch` 后，用户应能运行 `uv run paper_reader_batch ...`，把多篇论文派发给 `$paper_reader`，对 Zotero-backed items 默认走 verified `zotero_write`，并生成 batch report。仓库根目录只承担维护文档、发布说明和规划记录职责，不是运行时 Python project。

## 目录约定

- `paper_reader/SKILL.md`: skill 入口，只保留触发、路由和核心安全边界。
- `paper_reader/agents/openai.yaml`: Codex UI metadata；更新 `SKILL.md` 后检查是否同步。
- `paper_reader/src/paper_reader/`: Python package code for deterministic CLI/tooling logic.
- `paper_reader/tests/`: pytest tests; never perform real Zotero writes.
- `paper_reader/templates/`: Jinja2 note template.
- `paper_reader/references/`: workflow and schema references loaded by the skill when needed.
- `paper_reader/scripts/`: bundled helper scripts, including portable skill validation.
- `paper_reader/pyproject.toml` and `paper_reader/uv.lock`: dependency and lock metadata for the installed skill root.
- `paper_reader_batch/SKILL.md`: batch skill 入口，只保留批量触发、路由和 Zotero write-through 安全边界。
- `paper_reader_batch/agents/openai.yaml`: Codex UI metadata for `paper_reader_batch`。
- `paper_reader_batch/src/paper_reader_batch/`: deterministic batch CLI/tooling logic，包括 manifest、state、takeaway extraction 和 report。
- `paper_reader_batch/tests/`: pytest tests; never perform real Zotero writes or real LLM dispatch.
- `paper_reader_batch/references/`: batch workflow reference loaded by the skill when needed.
- `paper_reader_batch/scripts/`: bundled helper scripts, including portable batch skill validation.
- `paper_reader_batch/pyproject.toml` and `paper_reader_batch/uv.lock`: dependency and lock metadata for the installed batch skill root.
- `README.md`: English public entry point; explain install from `paper_reader/`, not root runtime usage.
- `README.zh-CN.md`: Chinese README paired with `README.md`; keep workflow commands, safety boundaries, and public claims synchronized when either README changes.
- `docs/superpowers/specs/`: planning and review artifacts.
- `docs/superpowers/scripts/`: maintainer-only validation scripts.
- `AGENTS.md`: agent behavior and safety rules.

Do not add `README.md`, `INSTALLATION_GUIDE.md`, `QUICK_REFERENCE.md`, or `CHANGELOG.md` inside `paper_reader/` or `paper_reader_batch/`.

## 运行产物与证据边界

- Zotero title workflow 使用 `create-run`，默认把本地产物写到安装后的 skill root 下：`runs/YYYY-MM-DD/<title-slug>/`；准备写入候选后，同目录包含 `note.md`、`note.html`、`gate-report.json` 和 `write-payload.json`。
- Batch workflow 使用 `paper_reader_batch init/next/record-result/next-write/record-write/report`，默认把 batch 产物写到安装后的 batch skill root 下：`runs/YYYY-MM-DD/<batch-slug>/`；同目录包含 `manifest.json`、`state.json`、`items/*.json`、`items/*.prepare.json`、`items/*.write.json`、`batch-report.json` 和 `batch-report.md`。单篇产物仍由 `paper_reader` 生成并拥有，batch 只记录索引、local-only path、Zotero note key 和 verify report path。全本地 PDF batch 的 report 使用 `effective_write_policy=local_only` 表达实际写入语义，即使 manifest 的 `write_policy` 仍是默认 `zotero_write`。
- `prepare-item` 默认生成 `context.md`，并在结构化抽取可用时生成 `section_context.md`；`section_context.md` 只用于帮助 Codex 定位章节、表格候选和值候选。
- PDF path workflow 使用 `prepare-pdf <pdf_path>`，首次在 PDF 同目录生成 `<pdf_stem>_analysis/` 和 `<pdf_stem>_note.md`，重复运行使用 `<pdf_stem>_analysis_v2/`、`<pdf_stem>_note_v2.md` 等后缀，不覆盖旧输出。自动化调用应优先用 `prepare-pdf --json-output <path>` 读取机器 JSON，不依赖 stdout 必须纯净。
- 本地 `.pdf` path 和本地目录 path 优先于 Zotero title routing；只要输入能解析成已存在的本地 PDF 或目录，就直接走 local PDF / batch local PDF folder workflow，不搜索 Zotero、不做同名/同 DOI 去重检查，也不因 Zotero 中已有相同文献而阻塞分析。目录 path 交给 `paper_reader_batch manifest from-pdf-folder`，默认非递归，只有用户显式要求才传 `--recursive`。
- `section_context.md` is not a canonical evidence source；最终 `evidence_summary` locator 必须使用 canonical 格式：`context.md page <N>`、`context.md page <N> section <Section Name>`、`context.md page <N> section <Section Name> table_candidate <N>` 或 `figure_context.md <figure_id>`。裸 `context.md` / `figure_context.md`、`page 3 method section` 这类散文式 locator、`section_context.md` 和 secondary context 路径都不是 write-ready evidence locator。
- 用户提供微信公众号、新闻稿、博客等网页时，只作为 secondary context capture，用于 cross-check 和补充背景；`evidence_summary` 只能引用 `context.md` 和 `figure_context.md`。Secondary context must not cite secondary context in `evidence_summary`.

## 阅读笔记语言规则

- Zotero 阅读笔记正文默认使用中文描述；除论文题名、作者名、机构名、化学式、材料/模型/方法专名、缩写、单位、引用 locator、代码式 key 和 Zotero tags 外，不要用整句英文解释。
- 会渲染到 `note.md` / `note.html` 的自由文本字段必须优先中文化，包括 `research_object`、`main_risk_short`、`method_modules`、`workflow_steps`、`technical_details`、`key_figures.analysis`、`key_figures.why_it_matters`、缺少 `analysis` 时会作为 fallback 渲染的 `key_figures.caption`、`author_stated_limitations`、`inferred_limits` 和 `applicability_limits`。
- `note_labels` 和 Zotero metadata tags 保持英文规范 key；它们是机器标签，不是正文描述。
- `lint-summary` 会把渲染字段中的整段英文 prose 视为写入阻断项；真实写入前必须修正到 `gate-report.json` 为 `write_ready`。

## 环境与依赖

- Python 环境必须用 `uv` 管理。
- 默认在 `paper_reader/` 内执行命令，使用 `uv run`。
- 修改 batch runtime 时默认在 `paper_reader_batch/` 内执行命令，使用 `uv run`。
- 首次使用或复制安装后，先在安装后的 skill root 运行 `uv --version` 确认 `uv` 可用，再运行 `uv sync --locked` 初始化本地环境。
- 如果 `uv sync --locked` 找不到 Python `>=3.13`，在 skill root 运行 `uv python install 3.13` 后重试。
- Zotero-backed workflow 需要 Zotero Desktop 和 `zotero-mcp-plugin`：按 <https://github.com/cookjohn/zotero-mcp#readme> 下载 `.xpi`，在 Zotero 里通过 `Tools -> Add-ons` 安装，启用 `Preferences -> Zotero MCP Plugin` integrated server；默认 Streamable HTTP endpoint 是 `http://127.0.0.1:23120/mcp`。
- 缺少项目依赖时在 `paper_reader/` 内使用 `uv add` 或 `uv add --dev`，不使用 `pip install`、`conda install` 或全局安装。
- 不修改系统 Python、conda base 环境或 shell 全局配置。

## Skill 使用方式

- Use `paper_reader`: 单篇论文阅读。Zotero 标题/标题片段走 Zotero MCP workflow；本地 `.pdf` path 走 local PDF workflow；本地目录 path 转交 `paper_reader_batch`，不要当成 Zotero 标题片段。单篇 skill 负责 extraction、summary/review、note rendering、write gate 和 read-only verification。
- Use `paper_reader_batch`: 多篇论文调度。batch skill 负责 manifest/state/report、`prepare-local-pdfs`、`next`/`record-result`、`next-write`/`record-write`；每篇仍派发给 `$paper_reader`，PDF folder/path items 保持 local-output only 且不做 Zotero lookup / duplicate check。

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

Better Notes 是可选阅读增强层。paper_reader 生成 Zotero 子笔记，保证 Better Notes 能正常显示；不调用 `Zotero.BetterNotes.api`，不依赖 Better Notes 存在。

## 验证命令

改完运行时代码、测试、模板、reference、dependency 或 `SKILL.md` 后，从 `paper_reader/` 运行：

```bash
uv sync --locked
uv run pytest
uv run paper_reader --help
uv run paper_reader extract-pdf tests/fixtures/minimal.pdf --output /tmp/paper_reader-extract.json
uv run python scripts/validate-skill.py .
```

改完 batch runtime、测试、reference、dependency 或 `paper_reader_batch/SKILL.md` 后，从 `paper_reader_batch/` 运行：

```bash
uv sync --locked
uv run pytest
uv run paper_reader_batch --help
uv run python scripts/validate-skill.py .
```

涉及根 README、中文 README、AGENTS 或安装说明时，还必须在仓库根目录运行：

```bash
python docs/superpowers/scripts/validate-root-docs.py
```

V2 发布前必须把 `paper_reader/` 和 `paper_reader_batch/` 分别复制到仓库外临时目录并在复制后的目录中运行同一组 skill-root 验证，证明两个 skill source 都自包含。

## 写入规则

- 写入前默认先 preview 并通过 gate；dry-run 作为显式策略或显式用户要求处理。
- Batch workflow 对 Zotero-backed items 默认 `write_policy=zotero_write`；batch CLI 只负责调度、状态和报告，禁止直接调用 Zotero MCP `write_note`。外层 agent 必须用 `next-write` 串行取出待写项，按 `write-payload.json` 和 `note.html` 调 Zotero MCP `write_note`，用只读 `verify-zotero-note` 校验后再 `record-write`。需要 dry-run 时显式传 `--write-policy prepare_only`。PDF batch items 保持 local-output only。每篇 30 秒结果必须从单篇 note 的 `30 秒结论` 行提取，fallback 才使用 `tldr` / `one_sentence_summary`，不能由 batch 重新总结。
- `prepare-local-pdfs` 只做本地 PDF 预抽取 fallback；成功结果优先来自 `prepare-pdf --json-output`，stdout 只作兼容回退。若只能从 `run.json` 恢复，必须要求该 manifest 为 `status=prepared`，且 `metadata_json`、`extract_json`、`section_context_md`、`secondary_sources_json` 和 `context.md` 都可读；初始化但未完成的 `run.json` 不能记为 prepared。
- PDF path workflow 是 local-output only；禁止搜索 Zotero、禁止做 Zotero duplicate check、禁止调用 `refresh-live-notes`，禁止生成 `write-payload.json`，禁止写 Zotero。PDF 本地笔记必须通过 `validate-summary-json -> apply-review -> lint-summary -> validate-trusted-summary -> prepare-local-note-candidate`，最终 Markdown 写到 PDF 同目录的 `<pdf_stem>_note.md` 或版本后缀路径。
- Zotero exact 搜索出现多个 normalized title 相同的条目时，停止分析和写入，要求用户先在 Zotero 去重；不要替用户选择父条目。
- MCP 原始 `get_item_details` 响应必须先落盘，再用 `save-item-details` 生成规范化的 `item-details.json`，后续本地命令只读规范化文件。
- 当 MCP 响应缺少 `extra` 时，`save-item-details` 可用只读 Zotero SQLite fallback 补齐 `Extra` / `其他`；成功补齐只记录 `_paper_reader.enrichment.extra.diagnostics`，不写入 `_paper_reader.warnings`；缺失、不可读或找不到条目才保留 warning。
- `prepare-item`、`extract-pdf`、`extract-figures` 默认处理完整 PDF；只有用户明确要求快速调试、预览或截断抽取时才传 `--max-pages <N>`。
- 真实写入 Zotero 前，必须展示 `note.md` 与 `note.html` 预览和目标 Zotero item 标题。
- 真实写入 Zotero 前必须完成最终门禁：推荐运行 `prepare-write-candidate`；等价底层链路为 `validate-summary-json -> apply-review -> lint-summary -> validate-trusted-summary -> refresh-live-notes -> next-version-suffix -> finalize-note --html-output -> note-tags -> preview-note note.md/note.html -> gate-run -> prepare-write-payload`，且 `gate-report.json` 必须为 `write_ready`。
- `prepare-write-candidate` 是日常写入准备入口；它会删除 stale `write-payload.json`，只在 gate 为 `write_ready` 时重新生成 payload。
- `prepare-write-payload` 的输出必须是当前 run 目录下的 `write-payload.json`；禁止把 payload 写到 `gate-report.json`、`note.html`、非 `write-payload.json` 文件名或 gate run 目录之外。
- 真实写入 Zotero 时，只能调用 `zotero-mcp write_note(action="create", parentKey=<payload parentKey>, content=<contents of note.html>, tags=<payload tags>)`；`content` 必须使用 `note.html` 的内容，避免 Markdown 表格在 Zotero 中被当作普通文本。
- 真实写入 Zotero 后必须用只读 `verify-zotero-note` 回读校验 parent、标题、必需章节、标签、最小长度和 `contentSha256`；`contentSha256` 使用项目内 canonical hash，不要用临时 shell hash 替代。
- single-paper summary writes always create a new versioned Zotero child note；不 update 既有 `[Codex Summary]` 总结 note。真实写入前必须运行 `prepare-write-candidate` 或等价底层链路，用只读 live note refresh 计算同日后缀。
