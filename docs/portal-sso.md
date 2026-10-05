# portal SSO 接入（`SZYYW_SSO`）

jppost.szyyw.xyz 接入 szyyw.xyz 门户的统一登录。契约见
[szyyw-auth](https://github.com/Szyoo/szyyw-auth)（本项目固定 tag 见 `requirements.txt`，由自动升级工作流跟进最新正式版）：Caddy 的 `(sso)` 门卫
先剥掉客户端自带的 `X-User` / `X-Role` / `X-Portal-Sub` / `X-Portal-Anon`，再由 portal `/api/auth/verify`
给已登录且有权访问本站的浏览器请求写入真实值；本站在 portal 公开名单里时，未登录访客改为只带
`X-Portal-Anon: 1`（不带任何身份头）放行，见下文「匿名访客」。`/healthz` 什么都不注入。

## 开关

| 变量 | 默认 | 作用 |
|---|---|---|
| `SZYYW_SSO` | `0` | `1` 才信任身份头。**必须在 Caddy 对本站 `import sso` 之后才能开**，否则任何人都能伪造 `X-User`。 |
| `SZYYW_SSO_AUTOCREATE` | `0` | 未映射的 portal 用户是否自动开通本地账号。 |
| `PORTAL_ORIGIN` | `https://szyyw.xyz` | portal 地址：登录跳转、退出落点、应用切换器数据源。 |

三项都在 `deploy/vps/docker-compose.yml` 的 `environment:` 里（从同目录 `.env` 插值），
不在网页"系统设置"白名单里，网页改不了。`SZYYW_SSO` 未设置时行为与接入前一致
（唯一可见差异：账号 JSON 多了一个 `portal_sub: null` 字段，因为迁移总会加列）。

## 数据：`accounts.portal_sub`

映射存的是 **portal 账号的固定 ID**（`X-Portal-Sub`，uuid 字符串），不是用户名：portal 用户名
（`X-User`）用户自己可以改，sub 不会变。

启动时幂等迁移：`ALTER TABLE accounts ADD COLUMN portal_sub TEXT` +
`CREATE UNIQUE INDEX idx_accounts_portal_sub ON accounts(portal_sub)`（可空、非空值唯一）。
不改任何已有行、不动账号 id，任务归属（`tracking_tasks.account_id`）不变。

## 账号解析顺序（每个请求）

0. 没有 `X-User` **或没有 `X-Portal-Sub`** → 视为未登录（浏览器跳 portal、API 401），
   绝不退回只按用户名匹配。
1. `accounts.portal_sub = X-Portal-Sub`（精确）→ 该账号（不管 X-User 现在叫什么）。
2. 否则 `accounts.username = X-User` 且该行 `portal_sub IS NULL` → **认领**：把
   `portal_sub` 填成 `X-Portal-Sub`，日志 `[SSO] … 认领同名本地账号`；之后 portal 改名也还是这个账号。本地用户名一律小写，所以 portal 的
   `Alice` 不会认领本地 `alice`；同名但已映射到别的 sub 的行不会被认领。
3. 否则 `SZYYW_SSO_AUTOCREATE=1` → 新建账号：`portal_sub = X-Portal-Sub`，`display_name = X-User`，`role = X-Role`，
   `login_enabled = 1`，随机密码哈希（没人知道明文）。本地用户名优先等于 X-User；
   不满足本地规则（小写、3–32 位、字母数字开头）或已被占用时规整为小写并加后缀
   （`Xy` → `xy-portal`，`carol` 被占 → `carol-2`）。
4. 否则 403「此账号尚未在 JPPost 开通，请联系管理员」。

解析到的账号若 `login_enabled = 0` → 403「此账号已在 JPPost 停用」：该开关在 SSO 下
仍是本站的停用开关。没有身份头（绕过门卫）→ 浏览器跳 portal 登录、API 401。
SSO 下本地 Flask session 里的 `account_id` **不被采信**。

**用户名与显示**：本地 `username` 是数据键，portal 改名时**不跟着改**（避免撞名与本地格式校验），
需要的话管理员手动改。页面右上角的「显示名 · 用户名」在 SSO 下第二段显示本次请求的 `X-User`
（portal 当前用户名，`viewer_state.portal_user`），没有就退回本地 username。

## 匿名访客（`X-Portal-Anon: 1`）

仅在 `SZYYW_SSO=1` 时生效（`szyyw_auth.flask.is_anonymous()`：头值必须正好是 `1`，且同时带了
身份头时身份优先）。SSO 未开时客户端自带的 `X-Portal-Anon` 被忽略，行为与以前一致。

| 请求 | 行为 |
|---|---|
| `GET`/`HEAD /` | 200，`anon_landing.html`：页面壳（标题、点阵背景、应用切换器、账户菜单「登录」）+ 一段空状态介绍（日本邮政单号追踪 + Bark 推送）和「登录」按钮（→ `/login` → portal 登录）。**不含任何任务、设置、管理入口或账号数据。** |
| `/api/*`、`/update_env`、`/remote_bark_status` | 401（与无身份相同，不放宽） |
| `/me/*` 表单 POST、`/login` | 302 到 portal 登录 |
| Socket.IO connect | 拒绝（`connect` 要求已登录 admin） |

实现：`require_auth` 只对 `ANON_PAGE_ENDPOINTS = {'index'}` 的 GET/HEAD 放行匿名；`index()` 未登录时
匿名渲染落地页，否则照旧 `unauthorized_response()`。既没有身份头也没有匿名头（绕过门卫或本站不在公开名单）
→ 仍然跳 portal 登录 / API 401。

## 账户菜单与登出

`boot.js` 在 `data-sso="1"` 时挂 `mountAppSwitcher` + `mountAccountMenu({portal})`（设计包 v0.8.0）。
账户菜单自己跨源查 portal `/api/me`：未登录显示「登录」（portal 登录小窗，被拦则整页跳转），登录后
显示首字头像、用户名、角色、账户设置、登出（portal `/api/logout`，随后刷新 → 本站落地页）。
因此 SSO 下页头里原来的「退出」按钮去掉了（它只清本地 session 再跳 portal，并不登出 portal）；
`/logout` 路由保留。SSO 未开时「退出」按钮照旧。

## 角色优先级

SSO 下授权角色 = **本次请求的 `X-Role`**（portal 权限矩阵决定），且账号须 `login_enabled`。
库里的 `role` 字段不参与授权、也不被改写（只在自动开通时按 X-Role 写入初值，
以及 SSO 关闭时恢复作用）。页面展示的角色同样取 X-Role。

## 路由行为（SSO 开启）

| 路由 | 行为 |
|---|---|
| `/login` | 已有身份 → 跳 `next`；未开通/停用 → 403；否则 302 到 `PORTAL_ORIGIN/login?rd=<X-Forwarded-Proto>://<X-Forwarded-Host><next>` |
| `/register` | 404（登录页本身不再渲染，注册链接随之消失） |
| `/logout` | 清本地 session，302 到 `PORTAL_ORIGIN`（页面上已无入口，登出走账户菜单） |
| 改密 | 所有密码输入框隐藏；`/api/me`、`/me/update`、`/api/users/<id>` 里的 `password`/`new_password` 字段被丢弃 |
| 管理员新建账号 | 不要求密码（服务端给随机密码），可同时填门户 ID（`portal_sub`） |

## 映射维护

- 网页：管理台「用户管理」列表显示 `门户 ID …`，账号详情里「门户 ID」+「保存映射」。
- API（admin）：`POST /api/admin/accounts/<id>/portal-user`，body `{"portal_sub": "<portal 账号 ID>"}`；
  空串或 `null` 清除。映射给第二个账号会 400。SSO 未开时也可用（便于先映射再开开关）。
- CLI（不经网页，容器内）：

```bash
docker exec jppost-tracker python src/manage.py list-accounts
docker exec jppost-tracker python src/manage.py map-account <username> <portal_sub>
docker exec jppost-tracker python src/manage.py unmap-account <username>
```

## 门卫排除路径

本应用没有任何机器端点：tracker 是 Web 进程拉起的子进程，只直连 Japan Post 与容器网络内的
`http://bark:8080`，不回调本站；Bark 推送的点击链接指向 Japan Post 官网；bark.szyyw.xyz 是独立站点。

| 路径 | 调用方 | 带 `Authorization` |
|---|---|---|
| `/healthz` | 健康探针、旧 Render 保活线程（`PUBLIC_URL` 留空即不跑） | 否（已在 `(sso)` 默认排除里） |

所以 Caddy 写 `import sso`（无额外路径）即可。`/socket.io/*`（Flask-SocketIO，polling + WebSocket）
是浏览器带 cookie 发起的，**要经过门卫**；connect 及各事件处理器读的是握手请求的环境，
身份同样来自 `X-Portal-Sub`/`X-User`/`X-Role`（见 `tests/test_portal_sso.py::test_socketio_identity_from_headers`）。
