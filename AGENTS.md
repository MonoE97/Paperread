# AGENTS.md

## 项目目标

本项目实现 Zotero-first 文献总结工作流：输入 Zotero 中的文章标题，Codex 通过 Zotero MCP 定位条目，使用本地 Python 工具抽取 PDF 内容，生成中文结构化论文总结，并在用户明确要求写入时创建 Zotero 子笔记。

## 目录约定

- `src/zotero_paperread/`：Python 包代码，只放确定性工具逻辑。
- `tests/`：pytest 测试，禁止真实写入 Zotero。
- `templates/`：Jinja2 note 模板。
- `skills/`：Codex skill 定义。
- `docs/references/`：外部项目参考与设计取舍记录。
- `docs/superpowers/plans/`：实施计划。

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
- 真实写入 Zotero 前，必须展示 note 预览和目标 Zotero item 标题。
- 重复运行不覆盖旧 note，使用 `[Codex Summary] <paper title> - YYYY-MM-DD` 标题创建新版本。
