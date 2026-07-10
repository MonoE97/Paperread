# AGENTS.md

## 项目目标

本项目维护一个自包含的 paper_reader skill repo。可安装运行产物是两个 skill source：`paper_reader/` 复制到 Codex 或 Claude 的 skills 目录并命名为 `paper_reader` 后，用户应能在安装后的 skill root 内运行 `uv sync --locked`、`uv run paper_reader ...`，使用 Zotero 标题工作流和本地 PDF path 工作流；`paper_reader_batch/` 复制并命名为 `paper_reader_batch` 后，用户应能运行 `uv run paper_reader_batch ...`，把多篇论文派发给 `$paper_reader`，对 Zotero-backed items 默认走 verified `zotero_write`，并生成 batch report。仓库根目录只承担维护文档、发布说明和规划记录职责，不是运行时 Python project。

## Paper Reader 2.0 约束（binding target contract）

本文件定义 Paper Reader 2.0 的 binding target contract。当前重构按文档、合同、运行时、发布元数据分阶段落地；写入本文只绑定最终行为，不等于尚未实现的 grouped CLI 已经可用，也不授权提前更新 public README release claims。两个独立 package 的目标版本均为且只能为 `2.0.0`。

### Breaking 规则

- `paper_reader` 的活动 schema 只能是 `paper_reader.run.v2`、`paper_reader.summary.v2`、`paper_reader.review.v2`、`paper_reader.review-package.v2`、`paper_reader.candidate.v2`、`paper_reader.write-authorization.v2`、`paper_reader.verification.v2`、`paper_reader.reconciliation.v2` 和 `paper_reader.command-result.v2`。
- `paper_reader_batch` 的活动 schema 只能是 `paper_reader_batch.manifest.v2`、`paper_reader_batch.state.v2`、`paper_reader_batch.event.v2`、`paper_reader_batch.worker-result.v2`、`paper_reader_batch.local-prepare-result.v2`、`paper_reader_batch.write-result.v2`、`paper_reader_batch.reconciliation.v2`、`paper_reader_batch.report.v2` 和 `paper_reader_batch.command-result.v2`。
- 所有 V2 模型使用 Pydantic v2 strict mode 与 `extra=forbid`；不接受未知字段、隐式类型转换或模糊 schema。
- V1/unversioned artifacts are historical-only。它们只能作为不可变历史证据存在；V2 loader 必须在加锁、写文件、分配输出、网络调用或任何其他 mutation 之前以结构化错误码 `unsupported_run_schema` 拒绝 V1、无版本和未知版本。
- There are no aliases, migration loaders, dual readers, schema guessing, compatibility shims or hidden V1 fallbacks。旧 flat commands 在 V2 public CLI 中不可达，但替代实现完成前不得物理删除其源码、测试或 reference。
- 所有 operational commands 的 stdout 必须恰好是一份对应的 `*.command-result.v2` JSON；诊断写 stderr。`--help` 和 `--version` 可保持 human-oriented。

### Grouped CLI

`paper_reader` public grouped CLI 只包含：

- `route`
- `run init-local|init-zotero|prepare|status|validate`
- `review validate|seal`
- `candidate build`
- `local publish`
- `zotero authorize|verify|reconcile`
- `maintenance`，只承接与核心状态机无关的纯工具

`paper_reader_batch` public grouped CLI 只包含：

- `manifest ...`
- `run init|validate|status|recover|report`
- `worker claim|renew|finish|release|retry`
- `local-prepare claim|renew|finish|release|retry`
- `write claim|preview|renew|release|begin|commit|mark-uncertain|reconcile|retry`

所有 batch state mutation 都必须带 `--request-id UUID`，并通过 request fingerprint 提供幂等 replay；相同 request id 只允许重放相同请求，不允许代表另一项操作。

### Immutable candidate 与 authorization

- Evidence、sealed review package、candidate 和 authorization 都是 immutable artifacts；任何输入、目标、正文、标签或 hash 改变都必须重建后继 artifact，禁止原地修补。
- Candidate 必须绑定 run、source identity、evidence、sealed review、固定输出目标或 Zotero parent、精确 note title/tags/content，以及所有相关 artifact 的 canonical SHA-256 与 size。
- `local publish` 只能把 local candidate 发布到 candidate 已固定的目标，使用 same-filesystem atomic no-replace；目标已占用时停止并 rebuild candidate，禁止覆盖或换目标继续发布。Local candidates 绝不产生 Zotero authorization。
- `zotero authorize` 不接受 parent/title/content/tag override；它必须重新计算所有 hash，刷新只读 parent/children snapshot，检查 parent fingerprint 与 title availability，并在本地 parent lease 下创建一次性 immutable authorization。TTL 默认且最大均为 300 seconds，authorization 绑定 random nonce/token、exact HTML、canonical HTML hash/length、tags、candidate digest、parent snapshot 和 external claim id。
- The external agent is the only Zotero writer。它只能按 authorization 的精确 MCP envelope 调用 MCP `write_note` / `write_note(action="create", ...)` at most once。CLI 与 batch runtime must not call `write_note`。
- `zotero verify` 强校验 parent、note key、title、完整 tag set、required headings、minimum length 和 canonical HTML hash。`zotero reconcile` 只读地按 exact parent + title + hash 匹配；one match -> verified，zero -> not found 且需要显式 retry confirmation，many -> ambiguous/blocked。过期 authorization 仍可用于 verify/reconcile，但绝不能再次写入。

### Batch journal 与 lease

- `events/<20-digit-seq>.json` append-only hash-chain 是唯一 source of truth；`state.json` 只是 reconstructable snapshot。任何 gap、hash mismatch 或非法 event 都返回 `journal_corrupt` 并禁止 mutation。
- 每次 transaction 在 `.run.lock` 下校验 manifest SHA、journal、request id/fingerprint 与 lease token，按 durable atomic ordering 写 content-addressed result、event，再替换 snapshot。Stale snapshot 必须 replay journal；orphan result 必须忽略。
- Worker 与 local-prepare lease 默认 900 seconds，支持 claim/renew/finish/release/retry；stale lease token 必须拒绝，failed/blocked 只可显式 retry，同一 resolved PDF 必须保持 same-PDF mutual exclusion。
- Write claim 每次只返回一个 candidate 和绑定的 claim/lease identity，lease 默认 120 seconds。固定顺序是 claim -> preview immutable candidate（此时 authorization 尚不存在）-> 展示目标并取得用户 explicit real-write intent -> external agent 调 single `$paper_reader zotero authorize` 且绑定 external claim id -> batch `write begin`。Begin 要求 authorization 至少剩余 30 seconds，并在返回精确 MCP envelope 前原子消费 nonce、提交 `write.started`。同 request id 只返回 `replayed=true`，不得再次发送；新 request id 也不能创建第二次 start。
- Write 状态为 queued -> claimed -> started -> written。Claimed release/expiry 可回 queued；started 后的 crash、error 或 expiry 必须进入 uncertain, never queued。只读 reconcile 唯一匹配才可 written，零匹配要求 `--acknowledge-no-match` 后以新 authorization/new request id retry，多匹配必须 blocked。
- Reducer priority 固定为 corrupt > write_uncertain > running > needs_attention > awaiting_write > ready > succeeded。Local PDF 不进入 write queue；全本地 batch 的 `effective_write_policy=local_only`。Batch report 保留从单篇笔记 `30 秒结论` 提取、再 fallback 到 `tldr` / `one_sentence_summary` 的规则。
- Batch worker success 必须绑定 sealed single-paper review package，并证明所有 fallback 展开后的实际 rendered note 已通过 Chinese-first gate；只有 candidate path 不足以记录成功。

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
- `AGENTS.md`: agent behavior and safety rules.

Do not add `README.md`, `INSTALLATION_GUIDE.md`, `QUICK_REFERENCE.md`, or `CHANGELOG.md` inside `paper_reader/` or `paper_reader_batch/`.

## 运行产物与证据边界

- Local PDF path and directory path inputs always route before Zotero text。已存在 `.pdf` -> `local_pdf`；已存在目录 -> `local_pdf_directory` 并交给 `$paper_reader_batch`；看起来像 path 但不存在 -> `unsupported_local_path`；只有其他非 path 文本才可成为 `zotero_title`。Existing local paths are not Zotero title fragments。
- Local PDF output is local-only：不搜索 Zotero、不做同名/同 DOI duplicate check、不创建 Zotero candidate/authorization、不进入 batch write lane。首次目标仍是 `<pdf_stem>_analysis/` 与 `<pdf_stem>_note.md`，重复运行只可分配 `_v2`、`_v3` 等新路径，禁止覆盖。
- Zotero title workflow 的 V2 run 默认位于安装后 skill root 的 `runs/YYYY-MM-DD/<title-slug>/`；batch run 默认位于 batch skill root 的 `runs/YYYY-MM-DD/<batch-slug>/`。两个 skill root 相互独立，batch 只能索引 `$paper_reader` 的 immutable artifacts，不能复制单篇 schema、模板、证据规则或 gate。
- V2 evidence 由 immutable `evidence/<evidence_id>/` 拥有；`context.md`、`section_context.md`、`figure_context.md` 与 secondary capture 必须通过 `evidence.json` membership 和 hash 解析。`section_context.md` 只用于导航，不是 canonical evidence source。
- 最终 `evidence_summary` locator 必须使用 canonical 格式：`context.md page <N>`、`context.md page <N> section <Section Name>`、`context.md page <N> section <Section Name> table_candidate <N>` 或 `figure_context.md <figure_id>`。裸 `context.md` / `figure_context.md`、散文式 locator、`section_context.md` 和 secondary context 路径都必须阻断 review sealing / candidate build。
- 微信公众号、新闻稿、博客等网页只作为 secondary context capture，用于 cross-check 和补充背景；`evidence_summary` 只能引用 canonical PDF / figure evidence。Secondary context must not cite secondary context in `evidence_summary`。

## 阅读笔记语言规则

- Zotero 阅读笔记正文默认使用中文描述；除论文题名、作者名、机构名、化学式、材料/模型/方法专名、缩写、单位、引用 locator、代码式 key 和 Zotero tags 外，不要用整句英文解释。
- 会渲染到 `note.md` / `note.html` 的自由文本字段必须优先中文化，包括 `research_object`、`main_risk_short`、`method_modules`、`workflow_steps`、`technical_details`、`key_figures.analysis`、`key_figures.why_it_matters`、缺少 `analysis` 时会作为 fallback 渲染的 `key_figures.caption`、`author_stated_limitations`、`inferred_limits` 和 `applicability_limits`。
- `note_labels` 和 Zotero metadata tags 保持英文规范 key；它们是机器标签，不是正文描述。
- V2 review validation 必须在所有 fallback 展开后的 resolved render context 上检查整段英文 prose；任何违规都是 sealing / candidate blocker，不能靠省略字段绕过。

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

- Use `paper_reader`: 单篇论文阅读。先执行 path-first route，再通过 V2 grouped CLI 完成 run、review、candidate 与 local/Zotero 生命周期。单篇 skill 独占 extraction、summary/review schema、render、candidate、authorization、verification 和 reconciliation 规则。
- Use `paper_reader_batch`: 多篇论文调度。Batch skill 独占 manifest、journal、lease、claim/recover/report 与 serial write lane；每篇深度阅读仍派发给 `$paper_reader`。PDF folder/path items 保持 local-output only 且不做 Zotero lookup / duplicate check。
- 本文的 grouped CLI 是正在落地的 2.0 public contract。实现任务必须让 `--help` 与 schema export 最终匹配它；在该实现完成前，不得把仍存在的 V1 flat commands 描述成 2.0 兼容入口。

## Git 与发布

- 当前项目是本地 Git repo，默认分支 `main`。
- 功能开发在 feature branch 或 worktree 中进行。
- 可以创建本地 commit。
- 禁止在未获用户明确确认前执行 `git push`、创建 GitHub remote、公开发布或部署。
- 删除文件、目录或 git history 命中用户 redline。V2 替代实现完成后也必须停止，等待 second explicit deletion authorization，才可物理删除 V1 source files、tests、references 或任何其他 tracked 文件；实现授权和删除授权是两次独立批准。
- Historical run/output artifacts 永远视为用户数据，不因第二次删除授权自动进入删除范围。
- `.DS_Store`、虚拟环境、缓存、本地预览文件、PDF 分析目录、生成笔记和本地 `docs/` scratch 必须被 `.gitignore` 忽略。
- `docs/` 不是发布内容；不要重新引入 tracked `docs/` 规划文档或根文档 validator，除非用户明确要求恢复公开文档树。
- Paper Reader 2.0 runtime 未完成前，不更新 public README release claims；README 发布同步与 pyproject/lock 版本更新属于独立 release task。

## Zotero 边界

- 读取 Zotero 信息优先使用 `zotero-mcp`。
- 写入 Zotero 只能通过 `zotero-mcp write_note`，且必须由用户明确触发。
- 禁止直接修改 Zotero SQLite、Zotero storage 元数据、Better Notes 配置或 Better Notes 模板。
- Zotero local API and SQLite are read-only in this project；只允许用于 live 子笔记标题/正文读取和写后验证，禁止通过 Zotero local API、SQLite 或其他非 MCP 路径写入 Zotero。
- dry-run 必须只输出预览，不写 Zotero。

## Better Notes 策略

Better Notes 是可选阅读增强层。paper_reader 生成 Zotero 子笔记，保证 Better Notes 能正常显示；不调用 `Zotero.BetterNotes.api`，不依赖 Better Notes 存在。

## 验证命令

改完 single skill 的运行时代码、测试、模板、reference、dependency 或 `SKILL.md` 后，从 `paper_reader/` 运行：

```bash
uv sync --locked
uv run pytest
uv run paper_reader --help
uv run python scripts/validate-skill.py .
```

改完 batch runtime、测试、reference、dependency 或 `paper_reader_batch/SKILL.md` 后，从 `paper_reader_batch/` 运行：

```bash
uv sync --locked
uv run pytest
uv run paper_reader_batch --help
uv run python scripts/validate-skill.py .
```

只改合同文档时，至少运行受影响的 `tests/test_default_workflow_docs.py` 与两套 portable validator；若同一 task 改动运行时，再扩展到各自 full pytest、grouped help/version 与最小 PDF smoke。Legacy flat command 是否仍暂时存在不能作为 GREEN 证据。

涉及根 README、中文 README、AGENTS 或安装说明时，必须运行与修改范围对应的 skill-root 验证命令；仓库根目录不维护单独的根文档 validator。

V2 发布前必须把 `paper_reader/` 和 `paper_reader_batch/` 分别复制到仓库外临时目录并在复制后的目录中运行同一组 skill-root 验证，证明两个 skill source 都自包含。

## 写入规则

- 写入前必须先形成 sealed review package 和 immutable candidate，并展示固定目标、`note.md` / `note.html`、tags、hash 与所有 blockers。用户明确的真实写入意图只授权 `zotero authorize`；它不允许绕过 gate、改变 candidate 或调用第二次 MCP write。
- Local PDF 只允许 `local publish`，必须复核 source identity、candidate digest 与所有 artifact hash，并原子 no-replace 发布到 candidate 固定的 `<pdf_stem>_note[_vN].md`。它禁止任何 Zotero lookup、duplicate check、live-note refresh、authorization 或 write。
- Zotero exact search 出现多个 same normalized title 时，在 run allocation / lock / mutation 前停止并要求用户先去重；不得替用户选择 parent。
- `run init-zotero` 只消费已经保存的 raw MCP discovery bundle 与 exact expected item key；bundle 必须原样包含 exact search inventory 和 selected item details，使 duplicate normalized title 与 key selection 可离线复核。Raw 与 normalized source snapshot 都必须纳入 run identity；key mismatch 或 duplicate normalized title 都是 blocker。
- Candidate build 使用只读 live child snapshot 计算 exact versioned note title；candidate 必须包含 exact HTML、tags、parent fingerprint 和 canonical hashes。真实写入前重新 authorize，不允许直接从 candidate 推导可写 payload。
- External agent 取得未过期 authorization 后，只能调用一次 `zotero-mcp write_note(action="create", parentKey=<authorization parentKey>, content=<exact authorization HTML>, tags=<authorization tags>)`。Content 必须是 authorization 绑定的 HTML，禁止 Markdown、override 或重新渲染。
- 写调用后必须立即 read-only verify；若返回丢失、进程 crash、authorization/write lease 过期或结果不确定，进入 reconciliation/uncertain，不得自动重发。只有 exact parent + title + canonical HTML hash 唯一匹配才能确认 written。
- Batch 的 manifest 默认 policy 仍可表达 `zotero_write`；显式 dry-run 使用 `prepare_only`。Batch CLI 永远不调用 Zotero MCP `write_note`，只管理 journal、lease、authorization handoff、result 与 report。PDF batch items 始终 `effective_write_policy=local_only`。
- Single-paper write 永远 create 新的 versioned Zotero child note，不 update 既有 `[Codex Summary]` note。Authorization title availability 与 parent fingerprint 必须在每次授权时重新只读检查。
