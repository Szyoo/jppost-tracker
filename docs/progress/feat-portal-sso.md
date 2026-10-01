# feat/portal-sso 分支进度

基于本地 `main`（`159aeeb`，比 origin/main 多一个 v0.6.2 设计包提交）。

## 2026-10-02：匿名访客 + 账户菜单（分支 `feat/anon-account`，未部署、未合并）

- 基于 `main` 7418576。设计包 v0.8.0（新增 account.js，`mountAccountMenu` 在切换器旁挂载，仅 SSO）；
  szyyw-auth v0.2.0（`is_anonymous`；缺 X-Portal-Sub 即未登录——本项目本来就这么要求，原 38 项测试不改即过）。
- `SZYYW_SSO=1` 且门卫给 `X-Portal-Anon: 1`：`GET /` 渲染公开落地页（工具介绍 + 登录），无任务/设置/管理；
  所有数据接口、表单、Socket.IO 照旧 401/跳登录/拒绝。规则见 [../portal-sso.md](../portal-sso.md#匿名访客x-portal-anon-1)。
- SSO 下去掉页内「退出」按钮（账户菜单负责 portal 登出），`/logout` 路由保留。
- SSO 未开：登录/注册/用户门户/管理台/未登录跳转与 main 逐页比对一致（只归一化了时间戳）。
- pytest：50 项通过（新增 `tests/test_anon_account.py` 12 项）。
- 上线前提：portal 把 jppost 放进公开名单、Caddy 注入 `X-Portal-Anon`（契约已就绪）；不放进名单时行为与现在一样。
- 未做浏览器走查（本地无法模拟门卫注入头），账户菜单的弹窗/登出依赖 portal CORS，需上线后看一眼。

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

## 2026-10-02：映射改按 portal 固定 ID（未部署、未合并）

- portal 用户名改为可由用户自己修改，portal 同时发 `X-Portal-Sub`（固定 uuid）。
  列 `portal_user` → `portal_sub`（索引 `idx_accounts_portal_sub`）；该列只在本分支上存在，
  直接改了 ADD COLUMN，没有线上库需要迁移。
- 解析：`portal_sub = X-Portal-Sub` → 同名（`username = X-User`）且未映射则认领并写入 sub →
  可选自动开通（`portal_sub = X-Portal-Sub`）→ 403；缺 `X-Portal-Sub` = 未登录。
- 本地 username 不随 portal 改名；页头显示本次请求的 X-User。
- CLI `map-account <username> <portal_sub>`，HTTP body `{"portal_sub": …}`，UI 标签「门户 ID」。
- pytest：38 项通过（新增 sub 优先于同名、缺 sub 即未登录；认领/同名不同 sub/改名已在原用例里覆盖）。

## 上线顺序（待办）

- [ ] 合并 + VPS 重建镜像（需要 pip 能访问 GitHub 归档）。
- [ ] 用 CLI 按批准的映射表执行 `map-account <username> <portal_sub>`（`admin` → portal 管理员的 sub 等；
      同名且未映射的账号也可以留给首次登录自动认领）。
- [ ] szyyw-platform Caddyfile 给 jppost.szyyw.xyz 加 `import sso`，部署 Caddy。
- [ ] 之后才在 `deploy/vps/.env` 设 `SZYYW_SSO=1` 并重建容器。
