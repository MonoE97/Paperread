# AGENTS.md

## 项目目标

本项目维护一个自包含的 paper_reader skill repo。可安装运行产物是两个 skill source：从 committed revision 只导出 `paper_reader/` 的 tracked files，验证 release bundle 后安装到 Codex 或 Claude 的 skills 目录并命名为 `paper_reader`，用户应能在安装后的 skill root 内运行 `uv sync --locked`、`uv run paper_reader ...`，使用 Zotero 标题工作流和本地 PDF path 工作流；`paper_reader_batch/` 同样只从 committed revision 导出 tracked files、验证并命名为 `paper_reader_batch` 后，用户应能运行 `uv run paper_reader_batch ...`，把多篇论文派发给 `$paper_reader`，对 Zotero-backed items 默认走 verified `zotero_write`，并生成 batch report。仓库根目录只承担维护文档、发布说明和规划记录职责，不是运行时 Python project。

## Paper Reader 2.2 约束（released runtime contract）

本文件定义 Paper Reader 2.2 的 released runtime contract。两个独立 package 的版本均为 `2.2.0`；public README、`SKILL.md`、UI metadata、JSON Schema、grouped CLI 与 lockfile 必须同步反映同一个已发布合同。Schema identifier 继续使用 `.v2`，不引入 V1 fallback、schema guessing 或自动迁移。

### Breaking 规则

- `paper_reader` 的活动 schema 只能是 `paper_reader.run.v2`、`paper_reader.summary.v2`、`paper_reader.review.v2`、`paper_reader.review-package.v2`、`paper_reader.candidate.v2`、`paper_reader.write-authorization.v2`、`paper_reader.verification.v2`、`paper_reader.reconciliation.v2` 和 `paper_reader.command-result.v2`。
- `paper_reader_batch` 的活动 schema 只能是 `paper_reader_batch.manifest.v2`、`paper_reader_batch.state.v2`、`paper_reader_batch.event.v2`、`paper_reader_batch.worker-result.v2`、`paper_reader_batch.local-prepare-result.v2`、`paper_reader_batch.write-result.v2`、`paper_reader_batch.reconciliation.v2`、`paper_reader_batch.report.v2` 和 `paper_reader_batch.command-result.v2`。
- 所有 V2 模型使用 Pydantic v2 strict mode 与 `extra=forbid`；不接受未知字段、隐式类型转换或模糊 schema。
- V1/unversioned artifacts are historical-only。它们只能作为不可变历史证据存在；V2 loader 必须在加锁、写文件、分配输出、网络调用或任何其他 mutation 之前以结构化错误码 `unsupported_run_schema` 拒绝 V1、无版本和未知版本。
- There are no aliases, migration loaders, dual readers, schema guessing, compatibility shims or hidden V1 fallbacks。V1 runtime surface has been removed；active source tree 只保留 V2 runtime，以及用于证明历史 schema 被只读拒绝的测试。
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
- `worker claim|prompt|renew|finish|release|retry`
- `local-prepare claim|renew|finish|release|run`
- `write claim|preview|renew|release|begin|commit|mark-uncertain|reconcile|retry`

所有 batch state mutation 都必须带 `--request-id UUID`，并通过 request fingerprint 提供幂等 replay；相同 request id 只允许重放相同请求，不允许代表另一项操作。

### Immutable candidate 与 authorization

- Evidence、sealed review package、candidate 和 authorization 都是 immutable artifacts；任何输入、目标、正文、标签或 hash 改变都必须重建后继 artifact，禁止原地修补。
- 单篇 run 的 deterministic ownership 必须使用 `authorizations/<authorization_id>.json`、`verifications/<authorization_id>/<note_key>.json` 和 `reconciliations/<authorization_id>.json`；snapshot sidecar 可使用对应无 `.json` 的同 stem 目录，但 main artifact 必须最后发布并通过持续打开的 no-follow parent fd 完成 no-replace，禁止中间 symlink、hardlink 或检查后 pathname race 逃逸 run root。
- Candidate 必须绑定 run、source identity、evidence、sealed review、固定输出目标或 Zotero parent、精确 note title/tags/content，以及所有相关 artifact 的 canonical SHA-256 与 size。Zotero title 中的 Markdown 元字符必须在 `note.md` H1 中按字面量转义，并用与 single lock 完全相同的 Markdown renderer 生成 `note.html`；渲染后可见 H1、candidate title、authorization title 与 readback title 必须逐字符一致。
- `local publish` 只能把 local candidate 发布到 candidate 已固定的目标，使用 same-filesystem atomic no-replace；目标已占用时停止并 rebuild candidate，禁止覆盖或换目标继续发布。Local candidates 绝不产生 Zotero authorization。
- `zotero authorize` 不接受 parent/title/content/tag override；它必须重新计算所有 hash，刷新只读 parent/children snapshot，检查 parent fingerprint 与 title availability，并在本地 parent lease 下创建一次性 immutable authorization。Direct single-paper authorize 不传 batch identity options；命令必须在同一 atomic authorization transaction 内分别生成两个不可变 `direct_<uuid>` 作为 external claim identity 与 attempt identity，持久化到 authorization 并在 command result 返回，调用者不得自造或覆盖。Batch path 的 `--external-claim-id` 与 `--write-attempt-id` 仅 batch authorize 使用，both batch identity options must appear together，partial input 在 mutation 前拒绝，并绑定 batch claim + candidate digest；batch path 不生成 direct identity。TTL 默认且最大均为 300 seconds，authorization 绑定 random nonce/token、exact HTML、canonical HTML hash/length、tags、candidate digest、parent snapshot、external claim id 和 `write_attempt_id`。Authorization 不绑定 batch `lease_token`；lease 可独立 renew，batch begin 另行校验当前 lease。
- The external agent is the only Zotero writer。它只能按 authorization 的精确 MCP envelope 调用 MCP `write_note` / `write_note(action="create", ...)` at most once。CLI 与 batch runtime must not call `write_note`。
- `zotero verify` 强校验 parent、note key、exact title、完整 tag set、required headings、minimum length 和 canonical HTML hash。An exact parent + title + canonical HTML hash match locates one note but does not verify it。`zotero reconcile` 定位唯一 note 后仍必须对该 note 执行完整 verify；只有全部字段通过才可 verified。Zero -> not found 且需要显式 retry confirmation，many -> ambiguous/blocked。过期 authorization 仍可用于 verify/reconcile，但绝不能再次写入。

### Batch journal 与 lease

- `events/<20-digit-seq>.json` append-only hash-chain 是唯一 source of truth；`state.json` 只是 reconstructable snapshot。任何 gap、hash mismatch 或非法 event 都返回 `journal_corrupt` 并禁止 mutation。
- Event 的完整 `.writing` / digest `.tmp` 只表示 provisional intent，绝不进入 reducer truth；只有 originating request 可在 committed-prefix state 上完成 final source/closure/freshness precommit validation 后原子提升为 `<20-digit-seq>.json`。任何 proposal 在 staging 前必须证明自身与派生 abort marker 都不超过 JSON artifact limit。校验失败时，必须先在原 proposal 的同一 sequence 以 no-replace + parent `fsync` 提交 deterministic `request.aborted` no-op marker；marker 内嵌并 hash 绑定原 proposal 的 canonical event、request id、command 与 fingerprint，主 journal 因而永久返回 `request_aborted`。Loader 必须以单次顺序 prefix replay 校验 marker 内嵌 proposal 的 command、request/event identity、domain identity 与 reducer legality，pending marker 也必须在 promotion 前通过同一校验。原 proposal staging 只能在 marker committed 后成为 exact inert residue，并通过 held no-follow descriptor 清空为 zero-byte logical tombstone；exact aborted origin 可在返回 `request_aborted` 前只清理与自身 canonical event digest 绑定的 residue，其他请求只有通过完整 pre-recovery/pre-mutation closure 后才可做该内部清理，任何被拒绝的 unrelated request 必须保持 residue bytes/mtime 不变。无法解析为完整 pending event 的 current-next `.writing` 同样不是事实源：通过全部 live closure 的新 transaction 必须在自身 staging/commit 前以 held descriptor 将 exact inode/raw 清空并 reload，校验失败的请求不得触碰它。任何 `.aborted.*` sidecar 都不是事实源；event/transition namespace 必须在 materialize/sort 前有界枚举，staging/residue 数量与 aggregate reads 必须有硬上限，committed event canonical bytes aggregate 上限固定为 256 MiB。新 transaction 在任何 side effect 前必须为 proposal tombstone 与 abort marker 预留两个 event-directory entries；已有 pending proposal 在 promotion/recovery 前必须重新校验 marker entry 与 committed-byte headroom。唯一的非空 exact marker prefix `.writing` 必须在 held inode 上续写、`fsync` 并提升，禁止另建 marker 后遗留 partial entry。Final event rename 之后只允许 structural guard，禁止用时钟或外部 closure 反判已经 committed 的 event。
- `state.json` repair transition 必须同时绑定 manifest、journal head、old/new SHA-256 与 size，并用 `.run.lock` secret HMAC；invalid/noncanonical V2 snapshot 的 exchange/retired-leaf crash 必须可恢复，V1/unversioned/unknown snapshot 仍在 mutation 前只读拒绝。
- 每次 transaction 在 `.run.lock` 下校验 manifest SHA、journal、request id/fingerprint 与 lease token，按 durable atomic ordering 写 content-addressed result、event，再替换 snapshot。Named `.lock` 在 POSIX 上必须先持有 lock parent 的稳定 ancestor guard，再 flock parent directory 与 named inode，防止 lock 或整个 parent pathname replacement 形成第二个临界区；需要跨 coordinator crash 延续原子边界时，child 必须继承 named + parent + ancestor descriptor bundle 到 durable marker 发布完成。Stale snapshot 必须 replay journal；orphan result 必须忽略。
- Manifest builder 与默认 `run init` 尚未拥有 run journal 时，必须使用 skill-root 内、受全局 no-follow lock 保护的持久化 request receipt；receipt 先绑定 exact UUID、request fingerprint 与预留目标，crash/replay 只能恢复该 exact target，禁止扫描 runs 猜测。任何 output 首次发布前必须用实际 JSON artifact limit 同时预检 output、reserved receipt 与 committed receipt 的 canonical bytes；任一超限必须在 receipt/target publication 前以 `resource_limit` 只读失败，同 request 重试不得退化为 `receipt_corrupt`。
- Worker 与 local-prepare lease 默认 900 seconds。Worker 支持 claim/prompt/renew/finish/release/retry；`worker prompt` 只读且不 dispatch LLM。Local-prepare 支持 claim/renew/finish/release/run；`local-prepare run` 只可通过显式 `--paper-reader-root` 调用 V2 grouped local init/prepare，不 import 单篇 package、不做 LLM/Zotero 工作。每个 worker/local-prepare claim event 至多绑定一个 PDF；worker 可在同一 limit 内跳过后续 PDF 并用非 PDF item 填满，其余 PDF 通过独立 claim 获得并发。Worker claim/prompt/renew/finish 与 local-prepare claim/run/finish 必须在各自副作用前重新绑定当前 PDF path、size、SHA-256、device 和 inode；stale lease token / source drift 必须拒绝。Worker failed/blocked 只可显式 retry，local prepare failed 只可用新 request id/new attempt 显式 run，同一 resolved PDF 必须保持 same-PDF mutual exclusion。
- `local-prepare run` 的内部协调状态固定在 `results/local-prepare/.coordination/.attempts/<attempt_id>.json` 与 `results/local-prepare/.coordination/<request_id>/{coordinator.lock,record.json,init.started,init.stdout,prepare.started,prepare.stdout}`；`*.started` 只在对应 child 真正启动后出现。Record 先发布，HMAC attempt owner 绑定 request-directory device/inode；在任何 child reservation/spawn 前，还必须用 derived internal request id 提交 `local_prepare.coordination_reserved` journal event，把 external request/fingerprint 与该目录 identity 锚定进 source of truth。Journal binding 已存在时，整个 coordination tree、owner、record 或目录 identity 的缺失/替换只能 fail closed，禁止重建。Child launcher 必须先核对目录 identity、fork gated executor，再原子发布 HMAC started marker，最后放行 exact argv；supervisor 在 marker 前崩溃时 executor 只读确认 marker 缺失并退出，marker 后崩溃时 executor 仍执行一次。Reservation 之后、runner spawn 之前必须在 authoritative run lock 内再次校验 PDF path/bytes/device/inode 与完整 `paper_reader_root` identity。Child 继承 `.run.lock` descriptor bundle、stdout flock 与 child-owned timeout；仅 reservation、没有 started/stdout 的崩溃可安全重试，started 但无 strict stdout 是 `coordination_uncertain` 且禁止二发。Init timeout 固定 60 seconds，prepare 默认 600 seconds；首次 side effect 前剩余 lease 至少为 `60 + prepare_timeout + 60` seconds，prepare 前至少为 `prepare_timeout + 60` seconds。Expired local attempt 若无 execution marker 才可 requeue；已有/无法排除 execution side effect 时 `run recover` 必须续租同一 claim/attempt 供恢复，绝不能分配 attempt 2。
- Worker/local release 只允许在外部 side effect 与单篇 artifact 产生前，并必须显式 `--acknowledge-no-side-effects`；缺少确认、已有 result 或 identity 过期时只读拒绝，不能把可能已执行的工作重新排队。
- Write claim 每次只返回一个 candidate，并生成绑定 writer/item 的 `claim_id`、`lease_token` 与 `write_attempt_id`；lease 默认 120 seconds。固定顺序是 claim -> preview immutable candidate（此时 authorization 尚不存在）-> 展示目标并取得用户 explicit real-write intent -> external agent 调 single `$paper_reader zotero authorize`，authorization 只绑定 external claim id + candidate digest + `write_attempt_id` -> batch `write begin` 独立校验当前 `claim_id`、`lease_token`、`write_attempt_id` 与 candidate digest。Begin 要求 authorization 至少剩余 30 seconds，并在返回精确 MCP envelope 前原子消费 nonce、提交 `write.started`。同 request id 只返回 `replayed=true`，不得再次发送；新 request id 也不能创建第二次 start。
- 所有 write preview/renew/release/begin/commit/mark-uncertain 命令，以及对应 journal events 和 content-addressed results，都必须绑定同一 `claim_id`、`lease_token` 与 `write_attempt_id`；stale 或 cross-attempt identity 在 mutation 前拒绝。
- Write 状态为 queued -> claimed -> started -> written。Claimed release/expiry 可回 queued；started 后的 crash、error 或 expiry 必须进入 uncertain, never queued。`run recover` 在 `.run.lock` 内检测 exact `write_attempt_id` 的 `write.started` lease expiry，并追加唯一 `write.lease_expired_uncertain` event；expired token 不作为输入，同 recover request id 幂等 replay，该 attempt 不得回 queued 或再次 begin。`write mark-uncertain` 仅接受未过期 exact writer/claim/token/write-attempt identity，用于主动报告已知异常。只读 reconcile 的 parent + title + hash 唯一匹配只定位 note；该 note 完成 exact parent、note key、exact title、complete tags、required headings、minimum length 和 canonical hash 全量验证后才可 written。零匹配要求 `--acknowledge-no-match` 后以新 authorization/new request id retry，多匹配必须 blocked。
- Reducer priority 固定为 corrupt > write_uncertain > running > needs_attention > awaiting_write > ready > succeeded。Local PDF 不进入 write queue；全本地 batch 的 `effective_write_policy=local_only`。Batch report 保留从单篇笔记 `30 秒结论` 提取、再 fallback 到 `tldr` / `one_sentence_summary` 的规则。
- Batch worker success 必须绑定 sealed single-paper review package，并证明所有 fallback 展开后的实际 rendered note 已通过 Chinese-first gate；只有 candidate path 不足以记录成功。

### Secondary finding anchor

- 新建 Zotero run 的 immutable `source/secondary-plan.json` 必须显式写入 `finding_anchor_policy: "codepoint_sha256_v1"`，无论 eligible URL 数量是否为零。历史 V2 plan 缺少该字段时保持原 canonical bytes 与 legacy no-anchor 语义；不得按包版本、时间或 Summary 内容猜测、补写或迁移 policy。未知 policy、未知字段和非严格类型继续失败关闭。
- `paper_reader.summary.v2.secondary_cross_checks[].findings[]` 可新增至多一个可选 `anchor`，其精确形状为 `capture_sha256`、`start_codepoint`、`end_codepoint`、`excerpt_sha256`。两个 digest 都必须是小写 SHA-256；offset 必须是 strict integer，按 immutable capture `text` 的 Unicode code point 索引；区间为左闭右开，长度只能为 20–2,000 code points。不得存储 URL、标题、发布者或正文副本。
- `codepoint_sha256_v1` plan 下，每个 `status="used"` finding 必须恰好绑定一个 anchor；anchor 的 `capture_sha256` 必须同时等于 evidence ArtifactRef、secondary inventory 和 immutable capture member raw bytes 的 SHA-256，`excerpt_sha256` 必须等于 `sha256(capture.text[start_codepoint:end_codepoint].encode("utf-8"))`。计算前禁止 Unicode、换行或空白规范化。legacy plan 禁止任何 anchor。
- Resolver 必须在任何 projection 前一次性验证所有 assessment/anchor；任一缺失、错配、越界或 hash 错误都阻断 review sealing 与 candidate build，不得产生部分投影。渲染仍只显示现有带来源交叉核对标注，不显示 excerpt、offset 或 hash，也不修改 Summary。
- `30 秒结论`、一句话总结、论文背景/方法/贡献、作者明示局限与全部 canonical locator 继续保持 PDF-only；`evidence_summary` 不得引用 secondary locator。`templates/zotero_note.md.j2` 必须逐字节不变。
- Local PDF 与 local batch 不产生或消费 secondary plan/anchor。Batch 不解析、验证或复制 anchor 语义，只要求 `$paper_reader` 返回 sealed 单篇制品并校验其 opaque hash closure。
- raw-CDP 对无效 request URL 的新增信息只能是固定、有界、隐私保护的诊断分类：checkpoint、allow-listed resource-type class、value type、length bucket、parseability 与 `http|https|other|none` scheme class。不得把 raw URL、host、path 或 query 写入 artifact、stdout 或 stderr；分类不得放宽既有 request policy，Fetch cancellation 未确认仍为 fatal。

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
- `scripts/`: repository maintenance and committed release-bundle automation only; it is not either Skill's runtime package.
- `.github/workflows/`: repository CI configuration only; it is not a runtime package and requires separate explicit authorization before modification.

Do not add `README.md`, `INSTALLATION_GUIDE.md`, `QUICK_REFERENCE.md`, or `CHANGELOG.md` inside `paper_reader/` or `paper_reader_batch/`.

## 运行产物与证据边界

- Local PDF path and directory path inputs always route before Zotero text。已存在 `.pdf` -> `local_pdf`；已存在目录 -> `local_pdf_directory` 并交给 `$paper_reader_batch`；看起来像 path 但不存在 -> `unsupported_local_path`；只有其他非 path 文本才可成为 `zotero_title`。Existing local paths are not Zotero title fragments。
- Local PDF output is local-only：不搜索 Zotero、不做同名/同 DOI duplicate check、不创建 Zotero candidate/authorization、不进入 batch write lane。首次目标仍是 `<pdf_stem>_analysis/` 与 `<pdf_stem>_note.md`，重复运行只可分配 `_v2`、`_v3` 等新路径，禁止覆盖。
- Zotero title workflow 的 V2 run 默认位于安装后 skill root 的 `runs/YYYY-MM-DD/<title-slug>/`；batch run 默认位于 batch skill root 的 `runs/YYYY-MM-DD/<batch-slug>/`。两个 skill root 相互独立，batch 只能索引 `$paper_reader` 的 immutable artifacts，不能复制单篇 schema、模板、证据规则或 gate。
- V2 evidence 由 immutable `evidence/<evidence_id>/` 拥有；`context.md`、`section_context.md` 与 `figure_context.md` 必须通过 `evidence.json` membership 和 hash 解析。`section_context.md` 只用于导航，不是 canonical evidence source。Zotero `Extra` 中 eligible 的 secondary capture 只能通过 `run prepare --secondary-capture-dir` 进入同一个新的 immutable evidence membership；已发布 evidence 不得补写。
- Plan-bound Zotero run 的 source closure 必须精确为 `source/discovery.raw.json`、`source/source.json` 和 `source/secondary-plan.json`；current run、sealed evidence 和 candidate 内的 run snapshot 必须保留同一个 opaque `secondary_source_plan` ArtifactRef，且 evidence 中固定的 `secondary_plan` member 必须与 source plan 的 bytes、SHA-256 和 size 完全一致。Batch 只校验这些 identity、hash 和 closed-world membership，不解析 URL、capture state 或 finding 语义。完整且自洽的 historical V2 no-plan closure 仍可只读验证和 journal replay，但不得从 package version、wall clock、summary 或其他启发式信息猜测其版本；plan file 无 ref、ref 无 exact file/evidence copy、plan member 出现于 no-plan 或 local closure 都必须失败关闭。
- 最终 `evidence_summary` locator 必须使用 canonical 格式：`context.md page <N>`、`context.md page <N> section <Section Name>`、`context.md page <N> section <Section Name> table_candidate <N>` 或 `figure_context.md <figure_id>`。裸 `context.md` / `figure_context.md`、散文式 locator、`section_context.md` 和 secondary context 路径都必须阻断 review sealing / candidate build。
- `run init-zotero` 必须从选中 parent 的权威 raw snapshot 读取字符串 `data.extra`，在 run 分配前拒绝 selected details / parent snapshot 冲突和非字符串值，并生成绑定 item key、snapshot digest、稳定 source id 与 exact URL 的 immutable `source/secondary-plan.json`。显式 `<URL>` 定界符内逐字保留；裸 URL 大小写不敏感地识别 HTTP(S)，并去除不匹配的自然语言包装符与句末标点；非 HTTP(S) 文本不进入 plan。URL 按出现顺序去重，保留 query，排除论文 DOI / publisher URL，最多 8 个 source eligible，后续 URL 标记 `source_limit`。Rejected source 保留其顺序 id，因此调用者必须读取 plan 中实际 eligible `source_id`，不得用 `eligible_source_count` 猜连续编号。新建 current-policy plan 的 producer 必须在 run 分配前保证 canonical bytes 不超过 strict capture 同一 2 MiB 上限；内部 warnings 只接受至多 256 个字符串且每个不超过 4,096 UTF-8 bytes，不得通过隐式字符串转换或 consumer-only failure 生成不可抓取 run。这些新 admission gates 不得应用于缺少 policy 的 historical V2 plan deterministic rebuild，以保持其既有 canonical bytes 和 legacy warning projection。
- `scripts/capture-secondary-url.mjs --plan ... --source-id ... --output ...` 是唯一可进入 evidence 的 strict capture 模式；它使用 direct raw CDP，且 strict mode does not use the legacy 3456 relay，也不负责启动 Chrome。Browser endpoint 只可来自 `ZOTERO_PAPER_READER_CDP_WS_ENDPOINT`、稳定 `DevToolsActivePort` 或 loopback `/json/version`（显式 `ZOTERO_PAPER_READER_CDP_HTTP_BASE_URL`，否则只探测 9222/9229/9333）。导航前必须创建 isolated empty BrowserContext，安装 `Fetch.requestPaused` / `Network.requestWillBeSent` 拦截与 WebRTC/WebTransport/WebSocket/EventSource/worker pre-document deny，禁用 cache、绕过 service worker，并应用 `Browser.setDownloadBehavior(deny)`。所有 passive binary image/media/font/prefetch 请求必须在 Fetch 阶段阻断；其余请求只允许无 body 的 `GET|HEAD|OPTIONS`。unsafe method/body 只有在 `Fetch.failRequest` 成功确认后才算阻断，取消失败必须 fatal。默认单 source deadline 60 秒、瞬时 request retry 最多 2 次；可用正文 200–100,000 Unicode code points，单 capture JSON 1 MiB、总正文 500,000 code points。CDP 对 decoded/encoded response 都实施单 response 8 MiB、总计 32 MiB 上限，cleartext proxy 另有单 response 8 MiB 上限。所有允许的 HTTP(S) hop 必须先解析为公网地址，再由 in-process loopback HTTP/CONNECT proxy 连接 pinned public IP；request event、guarded target session、blocked CONNECT 与 fatal proxy diagnostic 必须有硬上限，所有退出路径必须先 seal proxy。捕获 target 外的 Chrome background CONNECT 只能在没有 upstream dial 的情况下阻断并审计，owned target 观察到的未授权 authority 必须 fatal。每份 capture 必须精确绑定 `run_id`、`item_key`、`source_snapshot_sha256`、`secondary_plan_sha256`、source id 与 requested URL，并以 no-replace 发布。严格模式的 stdout 即使 argument/setup error 也必须恰好一个 machine JSON，诊断写 stderr；setup error 若未产生 artifact，调用者不得从 stdout 伪造 JSON，只能让该 source 进入 `not_attempted`/`unavailable`。Private/reserved answer、unsafe method/body、unguarded/over-limit request、authentication、popup、unsupported transport、direct-socket event、download 或 proxy violation 都使该 source `unavailable`。它不登录、不点击、不提交，并把页面文字当作不可信数据。系统 DNS 默认失败关闭；可信 TUN/fake-IP 环境只可显式使用 `--public-dns-over-https` 通过 Cloudflare 同时核验 A/AAAA，禁止整体放行合成地址段。拒绝 identity/URL/source/hash 错配、symlink、hardlink、TOCTOU 替换和额外文件；`unavailable` 只降级 secondary evidence，不阻断 PDF 主流程。Legacy positional mode 仍只是诊断材料，不得进入 review 或 candidate。
- `paper_reader.summary.v2.secondary_cross_checks` 仅用于结构化外部交叉核对；每个 eligible source 必须恰好有一个 `used|irrelevant|unavailable` assessment。Resolver 只把经验证 finding 投影到现有允许字段，不修改 Summary 或 `zotero_note.md.j2`；`30 秒结论`、一句话总结、论文背景/方法/贡献、作者明示局限与全部 canonical locator 始终 PDF-only。`evidence_summary` 始终只能引用 canonical PDF / figure evidence。
- 无 eligible `Extra` 链接时不抓网页、不生成交叉核对文字；Local PDF 和 local batch 在任何 evidence 分配前拒绝 secondary capture 路径。Batch 不抓取、解析或总结网页，只在 Zotero worker prompt 中委托 `$paper_reader` 执行该流程。

## 阅读笔记语言规则

- Zotero 阅读笔记正文默认使用中文描述；除论文题名、作者名、机构名、化学式、材料/模型/方法专名、缩写、单位、引用 locator、代码式 key 和 Zotero tags 外，不要用整句英文解释。
- 会渲染到 `note.md` / `note.html` 的自由文本字段必须优先中文化，包括 `research_object`、`main_risk_short`、`method_modules`、`workflow_steps`、`technical_details`、`key_figures.analysis`、`key_figures.why_it_matters`、缺少 `analysis` 时会作为 fallback 渲染的 `key_figures.caption`、`author_stated_limitations`、`inferred_limits` 和 `applicability_limits`。
- `note_labels` 和 Zotero metadata tags 保持英文规范 key；它们是机器标签，不是正文描述。
- V2 review validation 必须在所有 fallback 展开后的 resolved render context 上检查整段英文 prose；任何违规都是 sealing / candidate blocker，不能靠省略字段绕过。

## 环境与依赖

- Python 环境必须用 `uv` 管理。
- 默认在 `paper_reader/` 内执行命令，使用 `uv run`。
- 修改 batch runtime 时默认在 `paper_reader_batch/` 内执行命令，使用 `uv run`。
- 首次使用或 tracked-file clean install 后，先在安装后的 skill root 运行 `uv --version` 确认 `uv` 可用，再运行 `uv sync --locked` 初始化本地环境。
- 如果 `uv sync --locked` 找不到 Python `>=3.13`，在 skill root 运行 `uv python install 3.13` 后重试。
- Zotero-backed workflow 需要 Zotero Desktop 和 `zotero-mcp-plugin`：按 <https://github.com/cookjohn/zotero-mcp#readme> 下载 `.xpi`，在 Zotero 里通过 `Tools -> Add-ons` 安装，启用 `Preferences -> Zotero MCP Plugin` integrated server；默认 Streamable HTTP endpoint 是 `http://127.0.0.1:23120/mcp`。
- 缺少项目依赖时在 `paper_reader/` 内使用 `uv add` 或 `uv add --dev`，不使用 `pip install`、`conda install` 或全局安装。
- 不修改系统 Python、conda base 环境或 shell 全局配置。

## Skill 使用方式

- Use `paper_reader`: 单篇论文阅读。先执行 path-first route，再通过 V2 grouped CLI 完成 run、review、candidate 与 local/Zotero 生命周期。单篇 skill 独占 extraction、summary/review schema、render、candidate、authorization、verification 和 reconciliation 规则。
- Use `paper_reader_batch`: 多篇论文调度。Batch skill 独占 manifest、journal、lease、claim/recover/report 与 serial write lane；每篇深度阅读仍派发给 `$paper_reader`。PDF folder/path items 保持 local-output only 且不做 Zotero lookup / duplicate check。
- 本文列出的 grouped CLI 是 2.2 public runtime。`--help`、`--version` 与 schema export 必须持续匹配它；不得重新引入 V1 flat module、命令注册或兼容入口。

## Git 与发布

- 当前项目是本地 Git repo，默认分支 `main`。
- 功能开发在 feature branch 或 worktree 中进行。
- 可以创建本地 commit。
- 禁止在未获用户明确确认前执行 `git push`、创建 GitHub remote、公开发布或部署。
- 删除文件、目录或 git history 命中用户 redline，未来任何删除仍需新的明确授权。本次 2.0 迁移的 V1 source/test 删除已取得并执行独立授权。
- Historical run/output artifacts 永远视为用户数据；本次 V1 runtime 删除未触碰、移动、迁移或重新索引任何历史产物。
- `.DS_Store`、虚拟环境、缓存、本地预览文件、PDF 分析目录、生成笔记和本地 `docs/` scratch 必须被 `.gitignore` 忽略。
- `docs/` 不是发布内容；不要重新引入 tracked `docs/` 规划文档或根文档 validator，除非用户明确要求恢复公开文档树。
- 2.2 安装文档采用 clean install：只从 committed revision 导出两个独立 skill source 的 tracked files，在 `uv sync` 前以 `--release-bundle` 验证 staging tree，再移动到全新目标目录；禁止递归复制含 `.venv`、cache 或 `runs/` 的 working source，也禁止覆盖旧安装。旧安装目录可只读保留，但不得被 V2 自动发现、迁移或索引。根维护命令 `scripts/validate-committed-release-bundles.sh [git-revision] [staging-parent]` 必须解析一个精确 commit、保留仓库外 mode-0700 evidence root，并在相互独立的 install roots 中验证两个 Skill；它不得把本地通过描述为远程 CI 通过。

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
uv run paper_reader --version
uv run paper_reader --help
uv run paper_reader maintenance extract-pdf tests/fixtures/minimal.pdf
uv run python scripts/validate-skill.py .
```

改完 batch runtime、测试、reference、dependency 或 `paper_reader_batch/SKILL.md` 后，从 `paper_reader_batch/` 运行：

```bash
uv sync --locked
uv run pytest
uv run paper_reader_batch --version
uv run paper_reader_batch --help
uv run python scripts/validate-skill.py .
```

只改合同文档时，至少运行受影响的 `tests/test_default_workflow_docs.py` 与两套 portable validator；若同一 task 改动运行时，再扩展到各自 full pytest、grouped help/version 与最小 PDF smoke。发布扫描必须证明 active source 不含 V1 flat runtime surface。

涉及根 README、中文 README、AGENTS 或安装说明时，必须运行与修改范围对应的 skill-root 验证命令；仓库根目录不维护单独的根文档 validator。

修改 `scripts/validate-committed-release-bundles.sh` 或其测试时，还必须从仓库根目录运行：

```bash
uv run --project paper_reader pytest scripts/tests/test_validate_committed_release_bundles.py -q
bash -n scripts/validate-committed-release-bundles.sh
```

V2 发布前必须从待发布的 committed revision 把 `paper_reader/` 和 `paper_reader_batch/` 的 tracked files 分别导出到仓库外 staging 目录；在 `uv sync` 前运行 portable validator 的 `--release-bundle` 模式，再在 staging/安装目录中运行同一组 skill-root 验证，证明两个 skill source 都自包含且未夹带运行状态。
Batch 的 local-prepare 集成测试必须通过显式 `PAPER_READER_TEST_ROOT=/path/to/separately-staged/paper_reader` 支持非 sibling staging；不得把仓库 sibling 布局当作发布合同。

## 写入规则

- 写入前必须先形成 sealed review package 和 immutable candidate，并展示固定目标、`note.md` / `note.html`、tags、hash 与所有 blockers。用户明确的真实写入意图只授权 `zotero authorize`；它不允许绕过 gate、改变 candidate 或调用第二次 MCP write。
- Local PDF 只允许 `local publish`，必须复核 source identity、candidate digest 与所有 artifact hash，并原子 no-replace 发布到 candidate 固定的 `<pdf_stem>_note[_vN].md`。它禁止任何 Zotero lookup、duplicate check、live-note refresh、authorization 或 write。
- Zotero exact search 出现多个 same normalized title 时，在 run allocation / lock / mutation 前停止并要求用户先去重；不得替用户选择 parent。
- `run init-zotero` 只消费已经保存的 raw MCP discovery bundle 与 exact expected item key；bundle 必须原样包含 exact search inventory 和 selected item details，使 duplicate normalized title 与 key selection 可离线复核。Raw 与 normalized source snapshot 都必须纳入 run identity；key mismatch 或 duplicate normalized title 都是 blocker。
- Candidate build 使用只读 live child snapshot 计算 exact versioned note title；candidate 必须包含 exact HTML、tags、parent fingerprint 和 canonical hashes。真实写入前重新 authorize，不允许直接从 candidate 推导可写 payload。
- External agent 取得未过期 authorization 后，只能调用一次 `zotero-mcp write_note(action="create", parentKey=<authorization parentKey>, content=<exact authorization HTML>, tags=<authorization tags>)`。Content 必须是 authorization 绑定的 HTML，禁止 Markdown、override 或重新渲染。
- 写调用后必须立即 read-only verify；若返回丢失、进程 crash、authorization/write lease 过期或结果不确定，进入 reconciliation/uncertain，不得自动重发。三元组唯一匹配只定位候选 note；只有对其 exact parent、note key、exact title、complete tags、required headings、minimum length 和 canonical HTML hash 全部验证通过才能确认 written。
- Batch 的 manifest 默认 policy 仍可表达 `zotero_write`；显式 dry-run 使用 `prepare_only`。Batch CLI 永远不调用 Zotero MCP `write_note`，只管理 journal、lease、authorization handoff、result 与 report。PDF batch items 始终 `effective_write_policy=local_only`。
- Single-paper write 永远 create 新的 versioned Zotero child note，不 update 既有 `[Codex Summary]` note。Authorization title availability 与 parent fingerprint 必须在每次授权时重新只读检查。
