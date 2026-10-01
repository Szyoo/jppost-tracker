# feat/portal-sso 分支进度

基于本地 `main`（`159aeeb`，比 origin/main 多一个 v0.6.2 设计包提交）。

## 2026-10-01：SSO 模式完成（未部署、未合并）

- 设计与规则见 [../portal-sso.md](../portal-sso.md)：`SZYYW_SSO=1` 时按 `X-User` 解析账号
  （`portal_user` 映射 → 同名未映射认领 → 可选自动开通 → 403），授权角色取 `X-Role`。
- 迁移：`accounts.portal_user TEXT` + 唯一索引，幂等，不动 id。
- 新增 `src/manage.py`（list-accounts / map-account / unmap-account）与
  `POST /api/admin/accounts/<id>/portal-user`。
- 设计包升到 v0.7.0：`scripts/update-design.sh` 改为委托上游 `sync.sh`；
  应用切换器只在 SSO 开启时挂载（`<html data-sso data-portal>`）。
- compose 加 `SZYYW_SSO` / `SZYYW_SSO_AUTOCREATE` / `PORTAL_ORIGIN`（默认 0/0/https://szyyw.xyz）；
  requirements 加 szyyw-auth v0.1.0 归档 URL（python:slim 无 git）。
- 新增 pytest（`tests/`，临时 SQLite）：36 项通过。SSO 未开时登录/注册/用户门户页面
  与改动前逐字节一致，管理台只多了 JSON 里的 `portal_user: null`。

## 上线顺序（待办）

- [ ] 合并 + VPS 重建镜像（需要 pip 能访问 GitHub 归档）。
- [ ] 用 CLI 按批准的映射表执行 `map-account`（`admin` → portal 管理员用户名等）。
- [ ] szyyw-platform Caddyfile 给 jppost.szyyw.xyz 加 `import sso`，部署 Caddy。
- [ ] 之后才在 `deploy/vps/.env` 设 `SZYYW_SSO=1` 并重建容器。
