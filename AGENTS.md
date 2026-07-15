# AGENTS.md — jppost-tracker 项目说明

本文件是 agent 在本仓库工作时的权威指引，开工前先读一遍。

## 项目速览

日本邮政快递追踪 + Bark 推送通知的网页控制台，Flask + Flask-SocketIO 后端，Vue 风格前端。

- [src/app.py](src/app.py) — Flask/SocketIO 服务，管理追踪脚本、Bark Server 子进程、保活线程，
  WebSocket 实时推日志，`/update_env` 改 `.env`。
- [src/tracker.py](src/tracker.py) — 轮询日本邮政官网，解析最新物流记录，变化时推送 Bark。
- [src/templates/](src/templates/)、[src/static/](src/static/) — 控制台前端。

**当前分支现状**：
- `main` — 单用户版，面向 Render 云端部署，无登录鉴权。
- `codex/raspi-local-funnel` — 大改分支，改造为树莓派本地部署：SQLite 多用户账号体系
  （[src/storage.py](src/storage.py)）、登录鉴权、内外网 Bark 地址分离、Tailscale Funnel 部署文档。

如果不确定当前工作对应哪个方向，先确认 `git branch --show-current` 再动手。

## 语言规范

- **对话**：与用户交流一律使用中文。
- **代码注释**：新增/修改的注释使用中文（标识符、变量名仍用英文，遵循既有代码风格）。
- **git**：commit message、PR 标题/描述都使用中文。

## 计划与进度记录规则

- **会话内**：非平凡的多步任务用 TaskCreate/TaskUpdate 跟踪，完成即时标记，不要攒到最后。
- **跨会话的持久进展**：记到 `docs/progress/`，按分支拆分文件：
  - [docs/progress/README.md](docs/progress/README.md) — 索引 + 各分支进度文件的简介，
    以及跨分支才需要说明的事项（例如两个分支各自的定位、何时打算合并）。
  - `docs/progress/<branch-slug>.md` — 单个分支的详细进度、决策、未消化 TODO、踩过的坑。
    分支名里的 `/` 用 `-` 替换（例如 `codex/raspi-local-funnel` → `codex-raspi-local-funnel.md`）。
- **更新时机**：完成一个模块、做出一个技术决策、遇到阻塞、或分支切换前，立刻更新对应文件
  （最新内容放最上面）。不要等到会话结束才补记。
- 日期一律写绝对日期（如 2026-07-15），不要写"今天/昨天"。
- 不要把其他分支的进度文件当自己分支的草稿改；跨分支才相关的内容写回 README。

## 安全注意

- `.env`、`data/app.db`、`bark-data/`、`logs/` 等运行时数据不得出现在 commit 或进度文档里
  （已在 `.gitignore` 里，新增类似文件前先确认已忽略）。
- 涉及 Bark Key、密码哈希、`SECRET_KEY`、`ADMIN_PASSWORD_HASH` 等敏感值时，进度文档里只写
  "已配置/已生成"这类事实，不要贴真实值。
