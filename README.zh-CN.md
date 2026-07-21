# Paper Reader 2.1

[English](README.md) | **简体中文**

Paper Reader `2.1.0` 是面向 Codex 或 Claude 的自包含 skill repo。它把 Zotero 论文标题、本地 PDF path 或多篇论文批量输入，转成有证据链的中文结构化阅读笔记；实现方式是确定性的 grouped CLI 加 agent 写出的结构化总结。仅对 Zotero 条目，`Extra` 中符合条件的公开链接现在可以只读抓取，用于交叉核对 PDF 分析，但不能替代论文证据。

仓库根目录只是维护壳，不是运行时 Python project。安装和运行时使用一个或两个 skill source：

- `paper_reader/` 安装为 `paper_reader`，负责单篇深度阅读。
- `paper_reader_batch/` 安装为 `paper_reader_batch`，负责批量调度和轻量报告。

采用 clean install：只把对应 skill source 中已由 Git 跟踪的文件导出到 staging 目录，先验证 release bundle，再移动到全新的目标 skill 目录。不要递归复制工作中的 source 目录；其中可能包含 ignored `.venv`、cache 或 `runs/` 状态。不要覆盖 V1 安装。V1、无版本和未知版本 run 只作为未修改的历史文件保留；V2 不自动发现或迁移它们，显式读取时以 `unsupported_run_schema` 只读拒绝。

CLI 负责准备不可变 artifacts、校验 gate、渲染笔记和记录 batch 状态；agent 仍然负责阅读抽取出的 context，并写出严格的 `paper_reader.summary.v2` / `paper_reader.review.v2` 输入。

不要在 `paper_reader/` 或 `paper_reader_batch/` 内放 `README.md`；skill 内只保留 `SKILL.md`、直接链接的 `references/`、bundled scripts、代码、测试、模板、依赖元数据和 fixtures。

## 安装

Paper Reader 2.1 运行时目前支持 macOS 和 Linux；Windows 请使用 WSL。下面的 tracked-file 安装 helper 需要 POSIX shell。

staging skill 前先安装 `uv`。可使用官方 installer 或包管理器；常见方式：

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

先设置一次仓库根目录，再使用下面的 tracked-file staging helper。它会在 `uv sync` 创建安装目录自己的运行状态之前验证 staging tree：

```bash
set -eu
repo="/path/to/Paperread"

install_tracked_skill() {
  source_name="$1"
  install_dir="$2"
  command_name="$3"
  test ! -e "$install_dir" || { echo "target exists: $install_dir"; return 1; }
  install_parent="$(dirname "$install_dir")"
  mkdir -p "$install_parent"
  stage_dir="$(mktemp -d "$install_parent/.${source_name}.install.XXXXXX")"
  git -C "$repo" archive --format=tar "HEAD:${source_name}" | tar -xf - -C "$stage_dir"
  (
    cd "$stage_dir"
    uv run --no-project --python 3.13 python scripts/validate-skill.py . --release-bundle
  )
  mv "$stage_dir" "$install_dir"
  (
    cd "$install_dir"
    uv sync --locked
    uv run "$command_name" --version
    uv run "$command_name" --help
  )
}
```

source 必须是 Git checkout，并且 `HEAD:<source_name>` 必须存在。该流程只安装 committed `HEAD` tree 中的文件，不包含 working tree 或 index 中尚未 commit 的改动；安装前必须先形成目标 release commit。

Codex personal skills：

```bash
install_tracked_skill paper_reader \
  "${CODEX_HOME:-$HOME/.codex}/skills/paper_reader" paper_reader
install_tracked_skill paper_reader_batch \
  "${CODEX_HOME:-$HOME/.codex}/skills/paper_reader_batch" paper_reader_batch
```

Claude Code personal skills：

```bash
install_tracked_skill paper_reader \
  "$HOME/.claude/skills/paper_reader" paper_reader
install_tracked_skill paper_reader_batch \
  "$HOME/.claude/skills/paper_reader_batch" paper_reader_batch
```

如果目标 `paper_reader/` 或 `paper_reader_batch/` 目录已经存在，先停止，不要继续安装。Paper Reader 2.1 要求 clean install 到新目录。旧安装可以在其他位置只读保留。staging validation 失败时，隐藏 staging 目录会保留供检查，但绝不会提升为目标安装目录。

第一次运行 `uv sync --locked` 会根据对应 skill root 的 `uv.lock` 初始化安装后的本地环境。安装新导出的 revision 后，也应重新运行一次。

## Zotero MCP 设置

Zotero-backed workflow 需要先安装 Zotero Desktop 和 Zotero MCP plugin，agent 才能搜索文献库或调用 `write_note`。按 [cookjohn/zotero-mcp](https://github.com/cookjohn/zotero-mcp#readme) 安装并启用：

1. 从该仓库 releases 下载最新的 `zotero-mcp-plugin-*.xpi`。
2. 在 Zotero 中通过 `Tools -> Add-ons` 安装 `.xpi`，然后重启 Zotero。
3. 打开 `Preferences -> Zotero MCP Plugin`，启用 integrated server，并生成 client configuration。
4. 使用生成的 Streamable HTTP MCP 配置，或直接配置本地 endpoint：`http://127.0.0.1:23120/mcp`。

该插件内置 MCP server，不需要额外启动独立的 Zotero MCP server 进程。paper_reader 把 Zotero local API 和 SQLite 视为只读；真正写入只允许走 Zotero MCP `write_note`。

## Skill 使用方式

### Use `paper_reader`

`paper_reader` 用于一次分析一篇论文。CLI 是确定性工具，不是 standalone summarizer：它负责准备不可变抽取产物并校验笔记是否 ready，agent 负责阅读生成的 context 文件并写出严格 summary/review 输入。

- Zotero 标题或标题片段：让 agent 使用 `$paper_reader` 和论文标题。agent 会通过 Zotero MCP 搜索文献库，初始化 V2 run，并按需交叉核对选中条目 `Extra` 中符合条件的公开链接，再封存 review package、预览不可变 candidate；只有明确写入意图后才签发 300 秒 authorization，让外层 agent 最多调用一次 MCP `write_note`，随后只读 verify 或 reconcile。
- 本地 PDF path：给出绝对或相对 `.pdf` 路径。V2 会预留 `<pdf_stem>_analysis/` 和 `<pdf_stem>_note.md`，准备不可变 evidence，封存 review，构建 local candidate，并以不覆盖语义原子发布。该 workflow 不搜索 Zotero 中是否有相同文献，也不会写 Zotero。
- 本地目录 path：使用 `paper_reader_batch` 的 local PDF folder workflow。已存在的本地路径不是 Zotero 标题片段。

安装后的常用命令：

```bash
uv run paper_reader --help
uv run paper_reader route "/abs/path/to/paper.pdf"
uv run paper_reader run init-local "/abs/path/to/paper.pdf"
uv run paper_reader run prepare <run_dir>
uv run paper_reader review validate <run_dir>
uv run paper_reader review seal <run_dir>
uv run paper_reader candidate build <run_dir>
uv run paper_reader local publish <candidate.json>
uv run paper_reader zotero authorize <candidate.json>
uv run paper_reader zotero verify <authorization.json> --note-key <note_key>
uv run paper_reader zotero reconcile <authorization.json>
```

当 Zotero run 的不可变 `source/secondary-plan.json` 含有 eligible 链接时，应读取 `eligibility=eligible` 的实际条目并使用其中的精确 `source_id`；rejected 条目仍占据原顺序，因此 eligible id 不一定连续。Plan 保留 URL 出现顺序和 query，按 exact URL 去重，排除论文 DOI/publisher URL，并最多接纳 8 个 eligible HTTP(S) source。字面量 unsafe target 在 planning 阶段拒绝，hostname DNS 则在浏览器导航前校验。每个 source 都使用全新的 flat capture 目录和未占用的输出路径：

```bash
node scripts/capture-secondary-url.mjs --plan <run_dir>/source/secondary-plan.json --source-id secondary-001 --output <temporary_capture_dir>/secondary-001.json
uv run paper_reader run prepare <run_dir> --secondary-capture-dir <temporary_capture_dir>
```

该可选路径需要带原生 WebSocket 的 Node.js 22+，以及已经运行的 Chrome/Chromium raw debugging endpoint；脚本不会自行启动浏览器。可通过 `ZOTERO_PAPER_READER_CDP_WS_ENDPOINT` 指定精确 browser WebSocket，或把 loopback `/json/version` base 配成类似 `ZOTERO_PAPER_READER_CDP_HTTP_BASE_URL=http://127.0.0.1:9222`。两者都未设置时，strict mode 会检查稳定的 `DevToolsActivePort` 文件及 loopback 端口 9222、9229、9333。Chrome 144+ 的新 debugging connection 可能弹出 approval dialog；必须由用户在 Chrome 中明确批准，agent 不得绕过或代点该提示。

Strict mode 使用 direct raw CDP 和 isolated empty browser context，拒绝下载及主动交互，并在使用前逐跳校验 HTTP(S)；网页文字始终视为不可信数据。Capture JSON 以 no-replace 语义创建，stdout 只有一个 machine result，诊断写 stderr。`captured` 或 `unavailable` JSON 都可进入 evidence。如果 setup 在 artifact 创建前失败，不得伪造替代 JSON；让该 source 保持缺失，由 `run prepare` 记录为 `not_attempted`。缺失、不可读或 unavailable 的 secondary source 只会降低交叉核对完整性，不阻断 PDF 分析。Plan-bound strict mode 不使用 legacy 3456 relay，positional diagnostic output 不能进入 evidence。

Zotero `Extra` 中含歧义标点的精确签名链接应写成 `<URL>`：定界符内逐字保留；自然语言中的裸 URL 则大小写不敏感地识别 HTTP(S) scheme，并去除不匹配包装符和句末标点。非 HTTP(S) 文本不会进入 plan。每份被接受的 capture 都精确绑定 `run_id`、`item_key`、`source_snapshot_sha256` 与 `secondary_plan_sha256`。

如果可信的本地 TUN/fake-IP 代理导致系统 DNS 为所有公网域名返回非公网合成地址，不要放行该地址段；应在严格抓取命令中显式增加 `--public-dns-over-https`。该模式通过固定公网 IP endpoint 访问 Cloudflare DNS，在导航前同时校验 A/AAAA 记录，因此会向 Cloudflare 暴露来源域名。默认仍使用系统 DNS 并失败关闭。

本地 PDF run 禁止使用 secondary capture。

### Use `paper_reader_batch`

`paper_reader_batch` 用于一次处理多篇论文。必须同时安装 `paper_reader` 和 `paper_reader_batch`；batch skill 只负责调度与报告，单篇阅读仍由 `$paper_reader` 负责。

典型 grouped batch CLI 形态（每个 mutation 都使用新的 UUID request id）：

```bash
PAPER_READER_ROOT="/path/to/paper_reader"
PAPER_READER_BATCH_ROOT="/path/to/paper_reader_batch"

(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch manifest from-zotero-titles titles.txt --batch-title "my batch" --output manifest.json --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch run init --manifest manifest.json --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch run validate <batch_run_dir>)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch worker claim <batch_run_dir> --worker-id <worker_id> --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch worker prompt <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id>)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch worker finish <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id> --result <result.json> --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch write claim <batch_run_dir> --writer-id <writer_id> --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch write preview <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id>)
(cd "$PAPER_READER_ROOT" && uv run paper_reader zotero authorize <candidate.json> --external-claim-id <claim_id> --write-attempt-id <write_attempt_id>)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch write begin <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id> --authorization <authorization.json> --request-id UUID)
(cd "$PAPER_READER_ROOT" && uv run paper_reader zotero verify <authorization.json> --note-key <note_key>)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch write commit <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id> --result <write-result.json> --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch run report <batch_run_dir>)
```

`write begin` 会先持久化 `write.started`，再返回唯一允许外层 agent 发送的 MCP envelope。完成这一次 MCP `write_note` create 后，用单篇只读 verification 构造严格 write result 并 commit。对于结果变得不确定但仍是 unexpired started claim 的 attempt，active writer 必须带精确且仍有效的 claim identity 标记 uncertain：`(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch write mark-uncertain <batch_run_dir> <item_id> --writer-id <writer_id> --claim-id <claim_id> --lease-token <lease_token> --write-attempt-id <write_attempt_id> --reason <reason> --request-id UUID)`。expired started claim 不得复用 lease token，也不得重新发送 MCP 请求；它的 recovery path 必须只读执行：`(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch run recover <batch_run_dir> --request-id UUID --paper-reader-root "$PAPER_READER_ROOT")`。

重复执行 `worker claim -> prompt -> finish`，直到没有 eligible item。`worker prompt` 是交给外层 agent 或 subagent 的只读 handoff；batch CLI 本身不读论文，也不派发 LLM。

本地 PDF batch 在外层 agent 并行不可用时，可先预抽取：

```bash
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch local-prepare claim <batch_run_dir> --worker-id <worker_id> --request-id UUID)
(cd "$PAPER_READER_BATCH_ROOT" && uv run paper_reader_batch local-prepare run <batch_run_dir> <item_id> --worker-id <worker_id> --claim-id <claim_id> --lease-token <lease_token> --attempt-id <attempt_id> --paper-reader-root "$PAPER_READER_ROOT" --request-id UUID)
```

Zotero-backed items 默认 `write_policy=zotero_write`。需要 dry-run 时，在 manifest builder 中传 `--write-policy prepare_only`。PDF batch items 仍然只输出本地文件，并跳过 Zotero 搜索和去重检查。

## 工作流

paper_reader 支持两类输入：

- **Zotero 标题或标题片段**：通过 Zotero MCP 定位论文，保存完整 discovery inventory，从 `Extra` 派生不可变来源计划，按需只读抓取 eligible 公开链接，准备不可变 evidence，封存 review，并构建固定 Markdown/HTML candidate。只有外层 agent 能在明确写入意图后发送未过期 authorization 的精确 MCP create envelope，而且最多一次。
- **本地 PDF path**：绑定规范化绝对路径、大小、SHA-256、device 和 inode，准备不可变 evidence，封存 review，构建 local candidate，再把最终 Markdown 笔记原子发布到 PDF 同目录且永不覆盖。

本地 PDF path 和目录 path 输入会跳过 Zotero 搜索和去重检查。已存在的本地路径不是 Zotero 标题片段；目录路径交给 `paper_reader_batch manifest from-pdf-folder`，默认不递归，只有显式传 `--recursive` 才递归。

两个工作流默认都会抽取完整 PDF。最终 `evidence_summary` locator 必须使用以下 canonical 格式之一：`context.md page <N>`、`context.md page <N> section <Section Name>`、`context.md page <N> section <Section Name> table_candidate <N>` 或 `figure_context.md <figure_id>`。裸 `context.md` / `figure_context.md`、`page 3 method section` 这类散文式 locator、`section_context.md` 和 secondary context 路径都无效。`section_context.md` 只作为导航辅助。对 Zotero run，严格模式 `capture-secondary-url.mjs --plan ... --source-id ... --output ...` 的结果只能通过 `run prepare --secondary-capture-dir` 进入同一个不可变 evidence bundle；review 随后要求每个 eligible source 恰好一个 `secondary_cross_checks` assessment，并把验证后的 finding 投影到现有笔记字段。外部材料始终只能用于交叉核对，不能支撑 `30 秒结论` 或 `evidence_summary`；链接不可读时，只在现有“适用机会与边界”列表追加确定性说明。没有 eligible `Extra` 链接时，不抓网页也不生成交叉核对文字。本地 PDF run 会在 evidence 分配前拒绝该路径。

paper_reader_batch 支持四类批量输入：Zotero collection inventory、多个 Zotero 标题、本地 PDF 文件夹、多个 PDF path。它归一化为严格 manifest，并以 append-only hash-chain journal 为事实源；`state.json` 只是可重建 snapshot。Worker/local-prepare lease 默认 900 秒，串行 write claim 默认 120 秒。Zotero-backed items 默认 `zotero_write`，PDF items 永远不进入 write queue。`write.started` 持久化后发生 crash 时状态只能 uncertain，不能重发；`run recover --paper-reader-root ...` 会委托单篇只读 reconciliation，再记录 `written`、`retry_confirmation_required` 或 `blocked`。Dry-run 显式传 `--write-policy prepare_only`。纯本地 PDF report 使用 `effective_write_policy=local_only`；每篇结果从单篇 note 的 `30 秒结论` 提取，缺失时依次 fallback 到 `tldr`、`one_sentence_summary`，batch 不重新总结。

## 产物位置

- 单篇 V2 run 拥有 `run.json`、`source/`、`evidence/<evidence_id>/`、`reviews/<review_id>/`、`candidates/<candidate_id>/`、`authorizations/<authorization_id>.json`、`verifications/<authorization_id>/<note_key>.json` 和 `reconciliations/<authorization_id>.json`。新的 Zotero run 还绑定 `source/secondary-plan.json`；接受的 capture、状态清单和 `secondary_context.md` 只存在于不可变 evidence bundle 内。
- 本地 PDF 初始化预留 `<pdf_stem>_analysis/` 与固定 `<pdf_stem>_note.md`；名称占用时分配 `_v2`、`_v3` 等新版本，不修改旧产物。发布时永不覆盖或临时换目标。
- Batch V2 run 拥有 `manifest.json`、`events/<20-digit-sequence>.json`、`state.json`、`results/{worker,local-prepare,write,reconcile}/`、`batch-report.json`、`batch-report.md` 和 `.run.lock`。Report 完全由 journal replay 生成。

## 运行要求

- 安装和运行 CLI：`uv`，以及可由 `uv` 使用的 Python `>=3.13`；如果没有兼容解释器，用 `uv python install 3.13` 补齐。
- 本地 PDF 工作流：不需要 Zotero，也不做 Zotero 去重检查。图表抽取在元数据或 PDF 文件名出现 arXiv ID 时，可能尝试下载 arXiv source；该请求有有界 network timeout，失败后会回退到只基于 PDF 的抽取。
- Zotero 标题工作流：需要 Zotero Desktop，以及 Zotero MCP tools 或本地 MCP endpoint。
- Secondary web context capture：仅在使用该可选路径时需要 Node.js 22+ 和可访问的 Chrome/Chromium raw CDP endpoint。
- Batch workflow：需要已安装的 `paper_reader` 和 `paper_reader_batch`；确定性 child delegation 必须显式传入并校验 `--paper-reader-root`。

## 验证

在安装后的目录或源码 `paper_reader/` 目录中运行：

```bash
uv sync --locked
uv run pytest
uv run paper_reader --version
uv run paper_reader --help
uv run paper_reader maintenance extract-pdf tests/fixtures/minimal.pdf
uv run python scripts/validate-skill.py .
```

维护者在认为 `paper_reader/` 已自包含前，还应按上面的方式在仓库外构建 tracked-file staging 目录，在 `uv sync` 前运行 `uv run --no-project --python 3.13 python scripts/validate-skill.py . --release-bundle`，然后在该目录运行同一组验证。

在安装后的目录或源码 `paper_reader_batch/` 目录中运行：

```bash
uv sync --locked
uv run pytest
uv run paper_reader_batch --version
uv run paper_reader_batch --help
uv run python scripts/validate-skill.py .
```

local-prepare 集成测试会调用真实、单独 staging 的 `paper_reader`。如果该 root 不是仓库 sibling，必须显式绑定，而不是依赖目录发现：

```bash
PAPER_READER_TEST_ROOT="/path/to/separately-staged/paper_reader" uv run pytest
```

维护者在认为 `paper_reader_batch/` 已自包含前，也应按上面的方式在仓库外构建 tracked-file staging 目录，在 `uv sync` 前运行 `uv run --no-project --python 3.13 python scripts/validate-skill.py . --release-bundle`，然后在该目录运行同一组验证。

## 安全边界

- V1、无版本和未知版本 run/manifest/result 只作为历史文件保留，以 `unsupported_run_schema` 只读失败；没有兼容命令、迁移器、双 loader 或 fallback discovery。
- 每次 Zotero authorization 前必须先 preview，并通过 sealed-review 与 immutable-candidate gate；批量 dry-run 显式使用 `--write-policy prepare_only`。
- Zotero 写入只能由外层 agent 通过 Zotero MCP `write_note` 完成，必须有用户明确写入意图，并严格使用未过期 authorization envelope。
- Zotero local API 和 SQLite 只读。
- 本地 PDF path workflow 只能输出本地文件；不能搜索 Zotero，不能做去重检查，不能刷新 live notes、创建 authorization 或写入 Zotero。
- Zotero-backed batch workflow 默认 `zotero_write`，但 batch CLI 永远不调用 MCP `write_note`；它只持久化协调一次外部写入，并在之后 verify 或 reconcile。PDF batch items 始终 local-only。
- 渲染出的笔记正文应中文优先，同时保留论文标题、人名、公式、方法名、单位、证据 locator 和 tag key。
