# 日本邮政快递追踪与 Bark 通知

基于 Flask + Flask-SocketIO 的日本邮政（Japan Post）快递追踪服务：后台定时查询物流状态，有变化时通过 [Bark](https://github.com/Finb/bark-server) 推送到手机，并提供网页管理后台与用户自助门户。

## 功能

- 多用户：本地 SQLite 账号，管理员后台 + 普通用户自助注册；每个账号可追踪多个单号、绑定多台设备的 Bark Key
- 追踪脚本并行处理所有启用的任务，推送失败自动重试；可随 Web 启动自动运行、意外退出自动重启
- 推送与查询记录（任务时间线）：每个任务记录物流状态变化（含两次查询之间的中间状态）、推送成功/失败、查询出错与恢复、签收自动归档，后台与个人页的任务卡片上均可查看
- 签收后自动归档：物流记录出现签收状态（`お届け先にお届け済み` / `お届け済み` / `窓口でお渡し`）时，推送成功后自动归档、停止轮询；推送失败按退避重试，成功后再归档；账号没配 Bark Keys 时只记录状态不推送，签收即归档（可在系统设置关闭）
- 管理后台：启动/停止追踪脚本与本地 Bark Server、实时查看日志（Socket.IO）、在线编辑系统配置（`.env` 白名单键）
- Bark 地址内外分离：脚本走内网地址推送，手机使用公网 HTTPS 地址
- 安全：登录限流、`/healthz` 无鉴权健康检查；可选对接上游反代注入身份头的统一登录（`SZYYW_SSO=1`，仅在反代已剥离并重写身份头时启用）

## 网页结构

网页以单号为中心：打开就是要看的单号，不用再点进任何设置页。

**任务卡片**（管理后台与个人页同一套结构）：头部是备注（没有备注时用单号）、单号、渠道与状态胶囊；
下面一行醒目的摘要「当前步骤 · 第 N 天 · 预计明天上午签收」，写法与推送正文一致（见「合并履历与推送」，网页上总是带预计签收）；
「到达预测」默认展开（步骤进度、合并履历、17TRACK 外链），「推送与查询记录」默认折叠；
操作有编辑（就地展开：单号、备注、查询间隔、渠道、非常规件、发货前节点）、停用 / 启用、归档、删除，已归档的卡片是取消归档与删除。

**管理后台**（标签页 首页 / 设置 / 用户 / 日志）：

| 标签页 | 内容 |
|---|---|
| 首页 | 顶部「添加单号」（单号 + 备注 + 渠道，查询间隔、发货前节点、补录历史单号收在「更多」里）→ 我的单号（异常的排最前）→ 其他账号按账号分组（分组里也能替该账号添加）→ 页面底部「已归档」折叠区（展开时才加载卡片） |
| 设置 | 我的资料与 Bark 设备（含 Bark 预览）、服务控制（追踪脚本 / 本地 Bark / 公网健康状态）、17TRACK、状态规则、系统设置 |
| 用户 | 账号列表 + 账号详情（资料、Bark 设备、可登录、门户 ID / 重置密码、Bark 预览）；任务数与「在首页看这个账号的单号」入口 |
| 日志 | Remote Bark / Tracker / Bark 三路日志 |

**个人页**（普通用户）：添加单号 → 我的单号 → 已归档（折叠）→ 设置（我的资料与 Bark 设备、Bark 设置手顺）。

## 安装

需要 Python 3.10+（镜像使用 3.13）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 可选：下载本机 Bark Server（无官方二进制的平台会退回 Docker 包装器）
bash install_bark.sh
```

## 运行

### 本地

1. 在项目根目录创建 `.env`，最少配置：

   ```ini
   BARK_SERVER_INTERNAL=http://127.0.0.1:8080
   BARK_SERVER_PUBLIC=https://你的-bark-地址
   SECRET_KEY=一段足够长的随机字符串
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD_HASH=生成后的密码哈希
   ```

   生成密码哈希：`python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('你的强密码'))"`

2. 启动：`.venv/bin/python src/app.py`，访问 `http://localhost:6060/login`。管理员进入后台，普通用户注册后进入个人页。
3. 手机 Bark App 把服务器指向 `BARK_SERVER_PUBLIC`，复制 Key（或完整推送 URL）填到个人页的 Bark Keys，一行一个设备。换 Bark Server 后必须重新复制 Key。

测试：`.venv/bin/pip install pytest && .venv/bin/python -m pytest tests -q`

### Docker / 服务器

- `Dockerfile` 构建 Web 控制台镜像（`python src/app.py`，端口 6060，运行数据在 `/app/data`、`/app/logs`）。
- 对外建议经反向代理提供 HTTPS，容器不直接 publish 端口。最小示例（Web + 官方 `finab/bark-server`，`app.env` 内容见上方 `.env` 配置）：

  ```yaml
  services:
    jppost-tracker:
      build: .
      restart: unless-stopped
      env_file:
        - path: ./app.env
          format: raw   # 禁用插值，避免密码哈希里的 $ 被吃掉
      volumes:
        - ./data:/app/data
        - ./logs:/app/logs
        - ./app.env:/app/.env   # 供网页"在线编辑环境变量"读写
      expose: ["6060"]
    bark:
      image: finab/bark-server
      restart: unless-stopped
      volumes:
        - ./bark-data:/data
      expose: ["8080"]
  ```

  在该部署形态下 Bark 独立运行，建议设 `LOCAL_BARK_ENABLED=0`。
- 树莓派 + Tailscale Funnel + systemd 的本地部署见 [docs/raspberry-pi-funnel.md](docs/raspberry-pi-funnel.md)。
- 没有自建 Bark Server 时，也可以把 `bark-server` 以 `./bark-server -serverless true` 部署到任意 PaaS，再把地址填进 `BARK_SERVER_PUBLIC`。

## 接口说明

### HTTP

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/healthz` | 无 | 健康检查，返回 `{"status":"ok"}` |
| GET/POST | `/login`、`/register` | 无 | 登录、自助注册（启用统一登录时关闭） |
| POST | `/logout` | 无 | 退出登录 |
| GET | `/` | 登录 | 管理员显示后台，普通用户显示个人页 |
| POST | `/update_env` | 管理员 | 更新系统配置（仅白名单键） |
| GET | `/remote_bark_status` | 管理员 | 检查 Bark 地址连通性 |
| GET/POST | `/api/users` | 管理员 | 列出 / 创建账号 |
| GET/PUT | `/api/users/<id>` | 管理员 | 查看 / 修改账号 |
| POST | `/api/users/<id>/test_push` | 管理员 | 向该账号的设备发测试推送 |
| POST | `/api/users/<id>/tasks` | 管理员 | 为账号添加追踪任务 |
| POST | `/api/admin/accounts/<id>/portal-user` | 管理员 | 设置 / 清除账号的统一登录映射 |
| PUT/DELETE | `/api/tasks/<id>` | 登录（本人或管理员） | 修改 / 删除任务；PUT 只改带了的字段，`atypical`（布尔）标记非常规件 |
| POST | `/api/tasks/<id>/archive` | 登录（本人或管理员） | 归档任务 |
| GET | `/api/tasks/<id>/forecast` | 登录（本人或管理员） | 到达预测 `{"forecast", "summary", "history", "task", "links", "track17"}`：`forecast` 结构见「到达预测」；`summary` 是任务卡片的摘要行（「当前步骤 · 推送正文」，没有任何节点时为「还没有物流记录」）；`history` 是合并履历节点 `[{"key", "at", "segment", "stage", "name", "sources", "original", "detail"}]`（日本时间、倒序，见「合并履历与推送」）；`task` 是 `{"archived", "atypical"}`；`links` 是 `{"jp_post", "track17"}` 外链（17TRACK 只给 CN 单号）；`track17` 是登记状态 `{"status", "registered_at", "last_synced_at", "error"}`（`status`：`unregistered` 未登记 / `pending` 已登记待数据 / `synced` 已同步 / `error` 出错），只在 CN 单号且配了密钥时有，否则 `null` |
| POST | `/api/tasks/backfill` | 登录（管理员可带 `account_id` 替别人补录） | 补录历史单号：`{"lines": "单号 渠道 [下单日期]\n…"}`，最多 30 行；CN 单号在配了 17TRACK 时顺带登记，结果里带 `cn_note`（如「国内段稍后补上」） |
| GET | `/api/admin/track17` | 管理员 | 17TRACK 卡片：`{"configured", "console_url", "quota", "quota_ok", "numbers"}`；额度在进程内缓存 5 分钟（只缓存成功结果），请求失败 `quota_ok=false`；`numbers` 是已登记单号数及其中待数据 / 已同步 / 出错个数、最近同步时间、最近一条错误。不回显密钥 |
| GET | `/api/admin/status-rules` | 管理员 | 状态规则列表 `{"rules", "options"}`（`options` 是来源、事件类型、阶段的可选值），见「没收录的状态」 |
| PUT/DELETE | `/api/admin/status-rules/<id>` | 管理员 | 修改规则 `{"name_zh", "kind", "stage", "push"}`（校验后立即生效，来源记为手工）/ 删除规则 |
| POST | `/api/admin/status-rules/<id>/reclassify` | 管理员 | 重置为待归类，由追踪脚本重新归类 |
| GET/PUT | `/api/me` | 登录 | 查看 / 修改自己的资料与 Bark 设置 |
| POST | `/me/update`、`/me/tasks`、`/me/tasks/<id>/update`、`/me/tasks/<id>/archive`、`/me/tasks/<id>/delete`、`/me/test-push` | 登录 | 个人页的表单提交版本 |

JSON 接口统一返回 `{"status": "success" | "error", "message": …}`，未登录返回 401。以上接口靠登录会话或统一登录鉴权；机器接口 `/api/v1` 见下节。

### 机器接口（/api/v1）

给外部调用方（脚本、其他服务）用的令牌鉴权接口，只能操作令牌所绑定账号下的追踪任务。

**签发令牌**（在仓库根目录；Docker 部署在容器里 `/app` 下执行同样的命令）：

```bash
python src/manage.py issue-caller <调用方名称> <本地账号用户名>   # 明文令牌只打印这一次
python src/manage.py rotate-caller <调用方名称>                 # 换新令牌，旧令牌立即失效
python src/manage.py revoke-caller <调用方名称>
python src/manage.py list-callers                              # 不显示令牌
```

令牌形如 `jpt_…`，库里只存 SHA-256。已吊销的调用方可以用 `issue-caller` 以同名、同账号重新签发；换账号请换名称。

**鉴权**：每个请求带 `Authorization: Bearer <令牌>`。`/api/v1` 只认令牌，不看会话 cookie，也不看统一登录的身份头；
缺少、错误、已轮换或已吊销的令牌一律 401。所有响应都是 JSON，带 `Cache-Control: no-store`。

**接口**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/ping` | 自检：返回 `{"caller": 调用方名称, "account": 绑定的账号用户名}` |
| PUT | `/api/v1/tracking/<单号>` | 幂等登记：没有就新建（201），已有就关联或返回现状（200） |
| GET | `/api/v1/tracking/<单号>?events=20` | 任务状态 + 最近 N 条时间线事件（默认 20，0–100） |
| GET | `/api/v1/tracking/<单号>/forecast` | 到达预测：`{"tracking_number", "forecast": {…}}`，结构见「到达预测」 |
| POST | `/api/v1/tracking/<单号>/archive` | 幂等归档（已归档也返回 200） |

单号先去空白、转大写，再要求只含字母、数字、短横线，长度 6–40，否则 400。

PUT 请求体可省略；给了必须是 JSON 对象，只接受下列字段（`null` 视为未给，多余字段 400）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `label` | 字符串，≤100 | 备注，**只在新建任务时使用**；关联或恢复已有任务时不改它的备注、查询间隔、暂停状态 |
| `external_ref` | 字符串，≤200 | 调用方自己的关联编号；给了就更新，不给保留原值 |
| `revive` | 布尔 | 任务被用户在网页里删除或归档后，是否仍要重新追踪（见下） |
| `channel` | 字符串，≤50 | 渠道名（到达预测按渠道加权）。**只在任务渠道为空时写入**，不覆盖用户填的 |
| `milestones` | 对象 | 发货前节点 `{"ordered" \| "packed" \| "handed_over": 时间}`，时间接受 `YYYY-MM-DD`、`YYYY-MM-DD HH:MM[:SS]`（日本时间）或带时区的 ISO 8601（换算成日本时间）。不覆盖用户在网页里手填的节点 |

`channel` 与 `milestones` 在任务存在的各分支（`created` / `existing` / `linked` / `revived` / `delivered`）都生效；被 409 挡住时不写。

PUT 响应 `{"result": …, "tracking": {…}}`，`result` 取值：`created`（新建，201）、`existing`（本调用方已登记且在追踪）、
`linked`（关联到账号下已有的任务）、`revived`（从归档恢复）、`delivered`（已签收并被自动归档，原样返回、不恢复追踪）。

`tracking` 对象字段：`tracking_number`、`label`、`channel`（未标注为 `null`）、`external_ref`、`source_state`（本调用方的来源状态：`active`、`archived_by_api`、
`user_archived`、`delivered_archived`、`user_deleted` 之一，未登记为 `null`）、
`enabled`、`archived`、`last_tracking_info`、`last_checked_at`、`last_error`、`last_push_at`、`tracking_url`、`created_at`、`updated_at`。
没有值的 `last_*` 与 `external_ref` 为 `null`；时间格式 `YYYY-MM-DD HH:MM:SS`（日本时间）。

GET 响应 `{"tracking": {…}, "events": [{"id", "kind", "status", "detail", "tracking_number", "created_at"}, …]}`，事件按时间倒序，
`kind` 取值同网页时间线：`status_change`、`push_ok`、`push_fail`、`query_error`、`query_recovered`、`auto_archived`（签收自动归档）。

archive 响应 `{"result": "archived" | "already_archived", "tracking": {…}}`。

**错误**统一为 `{"error": {"code": …, "message": …}}`：

| HTTP | code | 场景 |
|---|---|---|
| 400 | `invalid_tracking_number` | 单号格式不合法 |
| 400 | `invalid_body` | 请求体不是 JSON 对象、字段类型不对、超长、有未知字段 |
| 400 | `invalid_query` | `events` 不是 0–100 的整数 |
| 401 | `unauthorized` | 缺少令牌、令牌无效或已吊销 |
| 404 | `not_found` | 本账号下没有这个单号，或接口不存在 |
| 404 | `user_deleted` | GET / archive 时：本调用方登记过的任务已被用户删除 |
| 405 | `method_not_allowed` | 请求方法不对 |
| 409 | `user_deleted` | PUT 时：任务已被用户在网页里删除（或改成了别的单号），且未带 `revive: true` |
| 409 | `user_archived` | PUT 时：任务已被用户在网页里归档，且未带 `revive: true` |

**来源与墓碑语义**

- 每个调用方登记过的单号都有一条来源记录。用户在网页里**删除**任务（或把它改成别的单号）后，记录保留为墓碑；
  调用方再 PUT 同单号得到 409 `user_deleted`，不会自动建回。带 `revive: true` 才重新建任务（201，`label` 生效）。
  如果用户自己又在网页里加回了这个单号，PUT 直接关联（200 `linked`）。
- 用户在网页里**归档**的任务，PUT 得到 409 `user_archived`；带 `revive: true` 恢复追踪（200 `revived`），不改用户的备注与间隔。
  用户在网页里自己取消归档后，PUT 照常返回 200。
- 调用方**自己**用 archive 接口归档的任务，再 PUT 视为新一轮，不需要 `revive` 就会恢复追踪（200 `revived`）。
- 追踪脚本检测到**签收**后自动归档的任务，来源状态为 `delivered_archived`：再 PUT 返回 200 `delivered`（`tracking.archived` 为 `true`），
  不恢复追踪；带 `revive: true` 才恢复（200 `revived`）。GET 照常返回，时间线最新一条为 `auto_archived`；archive 返回 200 `already_archived`，
  来源状态保持 `delivered_archived`。用户在网页里取消归档后来源状态回到 `active`。
- 账号下已有用户手动添加的同单号任务时，PUT 只做关联，不改它的备注、间隔、暂停状态，也不报错。
- archive 对任何人归档的任务都幂等返回 200，但不会把「用户归档」改记成「调用方归档」。

### 到达预测

追踪脚本每次抓到日本邮政履历都把全部记录存进承运方履历表（长期保留），读取时映射成阶段：
发货前（下单寄出 `ordered`、打包完成 `packed`、出库交运 `handed_over`，来自网页手填或调用方）→ 始发国收寄 `accepted` →
离开揽收网点 `left_collection` → 到达始发国国际互换局 `origin_exchange` → 始发国出口海关 `origin_customs` → 离开始发国（出境）`departed_origin` →
交航 `handed_to_airline` → 航班起飞 `flight_departed` → 到达中转地 `transit_arrived` → 离开中转地 `transit_departed` →
航班到达日本 `landed_destination` → 到达日本互换局（入境）`arrived_japan` →
日本进口通关 `japan_customs` → 离开日本互换局 `departed_exchange` → 到达派送局 `local_arrived` → 派送中 `out_for_delivery` → 签收 `delivered`（显示名里的「始发国」按单号国家码换成国名，如 CN → 中国）。
同名状态（国際交換局に到着 等）按详情最后一段是否以都 / 道 / 府 / 県结尾区分日本与始发国；认不出的状态不算阶段。
日本邮政页面上始发国那几行的时刻是始发国当地时间，按单号后两位国家码换算成日本时间（目前只有 `CN`）。

配置了 `TRACK17_API_KEY` 时，单号以 `CN` 结尾的件另经 17TRACK 取中国邮政的始发国段轨迹（每个单号只登记一次，按登记扣额度；
查轨迹不扣额度），离开揽收网点 / 交航 / 航班起飞 / 中转段 / 航班到达日本这几个阶段只有它才有；与日本邮政同含义的阶段两边取最早。
经第三地中转的两段航班：第一条「飞机进港」= 到达中转地，第二条「航空公司启运」= 离开中转地；只有一段航班的单没有这两个阶段。

每个阶段归属一个段：仓库段 `warehouse`（下单 / 打包 / 出库）、国内段 `domestic`（引受…航班起飞）、中转段 `transit`、日本段 `japan`（航班到达日本及之后）。
网页按段分组显示；中转段的显示名取系统设置 `TRANSIT_REGION_LABEL`（未设置按「韩国」，显示「韩国段」「到达韩国」「离开韩国」；清空则显示「中转段」「到达中转地」「离开中转地」）。
始发国段与日本邮政一起参与推送，见下节。

### 合并履历与推送

日本邮政与中国邮政两边的履历合成一条按段分组的「合并履历」（任务详情的预测区块里，标有两个外链胶囊）：
- 节点名带方向与地点，例如「提交中国出口海关查验（威海）」「离开威海国际互换局（出境）」「中国→韩国 航班起飞」「到达韩国中转」
  「到达東京国際郵便局（入境）」「日本进口通关中（東京国際郵便局）」「板橋北郵便局 派送中」。日本邮政状态译成中文（库里仍存原文），
  地名保留原文；没收录的状态显示原文并在日志里记一次。detail 里的「配達予定日」译成「预计投递日」。
- 两边含义相同、换算到日本时间后相差 ≤2 分钟的事件合并为一个节点，标「中邮·日邮 一致」。每段以主来源为准
  （仓库段：发货前节点；国内段、中转段：中国邮政；日本段：日本邮政），另一来源的共有节点在主来源报到之前也先显示，
  另一来源的独有事件不显示。没有中国邮政数据的单整条按日本邮政显示。
- 推送：每轮先同步两边，只推关键节点（白名单）：始发国收寄、始发国出口海关扣留、离开始发国（出境）、航班到达日本
  （「日本邮政接收（寄达地）」与「到达東京国際郵便局（入境）」先到的那个，只推一次）、日本进口海关扣留、离开日本互换局、
  到达派送局、派送中、已妥投 / 窗口领取，以及日本邮政的异常类状态（无人在家带回、局内保管、退回、调查中、转寄、再次投递等）。
  其余节点照样显示、记为已处理，但不推送。同一轮有多个新节点时只推最新的一个，其余同样记为已处理。
  哪边先报就由那一轮推送，另一边晚到的同一节点不再推送；推送失败时本轮节点都不记，下轮按退避重推。
  早于已处理节点的晚到事件（如 17TRACK 数据晚到）不补发。
- 推送标题是「节点名 · 备注」（没有备注时用单号后 4 位），正文一行：「第 N 天 · 预计 11/14（周六）上午签收」
  （今天 / 明天写「预计今天上午签收」「预计明天下午签收」，时段：凌晨 0–6 / 上午 6–12 / 下午 12–18 / 晚上 18–24；
  没有可参考历史时只写「第 N 天」，比以往都慢时写「第 N 天 · 比以往都慢」，已签收时写「全程 X 天」，精确到 0.5 天）。
  系统设置 `PUSH_FORECAST` 关闭时不写预计签收；预测算不出来时正文用节点时间，照常推送。
- 网页上任务卡片的摘要行与最新状态、推送与查询记录里的日本邮政状态也显示同样的中文名（接口另带 `last_status_display` / `status_display`）。

### 没收录的状态

承运方的状态是外部数据，代码内置表（日本邮政汉化表、中国邮政事件命名、阶段映射）收不全。查找顺序：代码内置表 →
已并入代码的规则（`src/status_rules.py` 的 `CODE_RULES`）→ 数据库 `status_rules` 表里已生效的规则 → 未识别。
- 未识别的状态在追踪时登记为待归类（`pending`，每个「来源 + 原文」一行，带一条样例详情）。在有结果之前：
  日本邮政的按原文推送，标题是「原文（未识别）· 备注」；中国邮政的不推送。
- 追踪脚本每分钟顺带处理几条待归类的状态：交给兼容 Anthropic Messages 的接口（`CLAUDE_BRIDGE_URL` / `CLAUDE_BRIDGE_API_KEY`，
  模型 `AI_MODEL`），要求只返回 `{"name_zh", "kind", "stage", "push"}`；`kind`（能与另一来源合并的事件类型）与 `stage`（阶段）
  只认现有枚举，中文名限 30 字、去掉换行。校验通过即生效（`done`），不需要人工确认；失败记下原因，3 次后标为 `failed`，按未识别处理。
  原文只作为数据交给接口，接口返回不合格的一律不采用。接口没配置或 `AI_CLASSIFY=0` 时不调用。
- 规则生效后：命名、阶段、合并与推送都按规则走；已经推送过的节点不补推、不重推。学到新规则时在追踪日志里记一行。
- 设置页的「状态规则」区块列出所有规则，可手工修改（立即生效，标为「手工」）、删除（以后再出现时重新登记）或重新归类。
- `python scripts/export_status_rules.py [--db data/app.db] [--write]`：把已生效的规则导出成 `CODE_RULES` 的条目
  （默认打印，`--write` 直接写进 `src/status_rules.py` 的标记之间），用来把学到的规则并入源码。

预测用全部账号的历史单号（按单号去重、排除当前单号）里「当前阶段 → 后续各阶段」的耗时，只跟离开当前阶段比当前已等时间更久的历史比，
权重按剩余阶段所在的段：仓库段同渠道 1.0、其他 0.3；国内 / 中转 / 日本段同收寄网点 1.0、其他 0.3（同一揽收网点之后走同一条邮政线路，与发货渠道无关；
当前单或样本缺收寄网点时退回按渠道比）；任务标了「非常规件」（如大件电器）的样本再乘 0.3（同单号任一任务标了就算）。
给出加权中位数与区间（≥3 单用 20%–80% 分位，否则最小–最大）。只输出聚合统计，不暴露其他单号。
历史太少时用「补录历史单号」（首页「添加单号 › 更多」里的按钮或 `POST /api/tasks/backfill`）补几单过去的单号与渠道。

`forecast` 对象：

| 字段 | 说明 |
|---|---|
| `origin` | 「第 N 天」的起点 `{"stage", "at"}`：下单 → 出库 → 引受中最先有的那个；都没有为 `null` |
| `day` | 从起点算第几天（起点当天为第 1 天，按日历跨午夜加 1）；已签收或无起点为 `null` |
| `delivered` / `total_seconds` | 是否已签收；已签收时起点到签收的全程秒数 |
| `current` | 当前阶段 `{"stage", "at", "waited_seconds"}`；还没有任何记录为 `null` |
| `arrived` | 已到达阶段 `[{"stage", "at"}]`，按时间先后 |
| `remaining` | 剩余阶段预测 `[{"stage", "segment", "basis", "median_seconds", "low_seconds", "high_seconds", "eta", "eta_low", "eta_high", "occurrence", "maybe", "samples"}]`；`basis` 是该段的比较依据（`channel` 同渠道 / `office` 同收寄网点），`samples` 为 `{"total", "similar", "atypical"}`；`maybe` 表示出现率低于 0.5 |
| `basis` | 可信度用的比较依据：剩余阶段里第一个阶段所在段的依据（仓库段 `channel`，邮政各段 `office`，当前单缺收寄网点时 `channel`） |
| `confidence` | 按 `basis` 数「同类」合格样本：`none`（没有可参考样本）/ `borrowed`（同类 0 单，只借鉴其他单）/ `rough`（同类 1–4 单）/ `solid`（同类 ≥5 单） |
| `samples` | 参考样本数 `{"total", "same_channel", "same_office", "similar", "atypical", "similar_atypical"}`：合格样本总数、同渠道、同收寄网点、按 `basis` 的同类、非常规件、同类里的非常规件 |
| `transit_region` | 中转地显示名（系统设置 `TRANSIT_REGION_LABEL`），空串表示用通用名 |
| `overdue` | 已比历史上离开当前阶段最慢的还久（这时按全部样本估计，剩余时长多为 0） |
| `channel` / `generated_at` | 当前单的渠道、计算时间 |
| `collection_office` | 收寄网点（从中国邮政「邮件离开【X】，正在发往…」「邮件已在【X】完成分拣」提取，互换局除外）；没有始发国段时为 `null` |
| `suggested_channel` | 渠道推断：本单渠道为空、且收寄网点在其他已标渠道的单号里只对应过一个渠道时给出那个渠道（不写库）；否则 `null` |
| `office_multi_channel` | 本单渠道为空、收寄网点在历史里对应过多个渠道（无法推断）时为 `true` |

时间格式 `YYYY-MM-DD HH:MM`（日本时间），时长单位秒。

### Socket.IO（仅管理员可连接）

- 客户端发送：`start_script`、`stop_script`、`start_bark_server`、`stop_bark_server`
- 服务端推送：`script_status`、`bark_server_status`（`{running: bool}`）；日志增量 `tracker_log`、`bark_log`、`remote_bark_log` 与连接时的最近 500 行 `full_tracker_log`、`full_bark_log`、`full_remote_bark_log`（`{data: str}`）；鉴权失败时 `auth_error`

### 环境变量（`.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BARK_SERVER_INTERNAL` / `BARK_SERVER_PUBLIC` | 空 | 推送用内网地址 / 手机与健康检查用公网地址，互为回退 |
| `BARK_SERVER` | 空 | 旧版单一地址，仅作兼容 |
| `BARK_HEALTH_PATH` / `BARK_HEALTH_TIMEOUT` | `/ping` / `15` | Bark 健康检查路径；探测与测试推送的超时（秒） |
| `REQUEST_TIMEOUT` | `15` | 追踪脚本抓取官网、向 Bark 推送时单次请求的超时（秒） |
| `AUTO_ARCHIVE_DELIVERED` | `1` | 签收后自动归档；`0` 时签收的任务照旧轮询 |
| `TRANSIT_REGION_LABEL` | `韩国` | 中转段的显示名（预测、合并履历、推送）；未设置按「韩国」，设为空则显示「中转段」 |
| `PUSH_FORECAST` | `1` | 推送正文写预计签收（「第 N 天 · 预计 11/14（周六）上午签收」）；`0` 时只写「第 N 天」 |
| `AI_CLASSIFY` | `1` | 自动归类没收录的状态（接口没配置时不调用），见「没收录的状态」 |
| `CLAUDE_BRIDGE_URL` / `CLAUDE_BRIDGE_API_KEY` | 空 | 归类接口：兼容 Anthropic Messages 的根地址（请求 `<地址>/v1/messages`）与 `x-api-key` 密钥 |
| `AI_MODEL` | `claude-haiku-5-5` | 归类用的模型 |
| `TRACK17_API_KEY` | 空 | 物流聚合查询服务 17TRACK 的 API 密钥，用于补充始发国段轨迹（只对中国寄出、单号以 `CN` 结尾的件）；留空则关闭该功能，其余不受影响 |
| `BARK_BIND_ADDRESS` / `BARK_EXECUTABLE` | `0.0.0.0:8080` / 自动查找 | 本地 Bark Server 监听地址与可执行文件 |
| `LOCAL_BARK_ENABLED` | `1` | `0` 时不管理本地 Bark 子进程（Bark 独立部署时） |
| `AUTO_START_BARK_SERVER` | `0` | Web 启动时自动拉起本地 Bark |
| `AUTO_START_TRACKER` | `1` | Web 启动时自动运行追踪脚本 |
| `APP_PORT` / `FLASK_DEBUG` | `6060` / `0` | Web 端口、调试模式 |
| `SECRET_KEY` | 随机 | 会话签名密钥，不设则每次重启失效 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH` | `admin` / 空 | 首次启动时引导出的管理员账号 |
| `SESSION_COOKIE_SECURE` / `ADMIN_SESSION_HOURS` | `0` / `24` | HTTPS-only cookie、登录有效期（小时） |
| `ADMIN_LOGIN_MAX_ATTEMPTS` / `ADMIN_LOGIN_WINDOW_SECONDS` | `5` / `600` | 登录限流 |
| `TRUSTED_PROXY_COUNT` | `0` | 受信反代层数；大于 0 才采信 `X-Forwarded-For`（取右数第 N 个） |
| `SZYYW_SSO` / `SZYYW_SSO_AUTOCREATE` / `PORTAL_ORIGIN` | `0` / `0` / — | 统一登录开关与上游地址；开启后信任反代注入的 `X-User` / `X-Role` / `X-Portal-Sub` 头。账号只按 `X-Portal-Sub` 映射；自动开通遇到同名未映射本地账号时另起带数字后缀的用户名，不会并入旧账号 |
| `SZYYW_SSO_ADOPT_BY_NAME` | `0` | `1` 时未映射的本地账号可被同名（`X-User`）门户用户认领。门户用户名可由用户自行修改，开启有被冒领风险，仅在一次性迁移旧账号时临时打开；平时请在「用户」页里填写门户 ID 或用 `manage.py map-account` 映射 |
| `DESIGN_BASE` | CDN | 覆盖前端设计包的加载地址（本地离线开发用） |

后台「设置 › 系统设置」按使用场景分组，只能编辑下表这些键（其余变量只能改 `.env` 后重启）。每个字段旁标着生效时机，保存后页面也会按实际改动的字段说明哪些已生效、哪些还要等：

| 分组 | 键 | 生效时机 |
|---|---|---|
| Bark 推送 | `BARK_SERVER_INTERNAL`、`BARK_SERVER_PUBLIC`、`BARK_SERVER` | 控制台保存即生效，追踪脚本下次查询时生效 |
| Bark 健康检查 | `BARK_HEALTH_PATH`、`BARK_HEALTH_TIMEOUT` | 保存即生效 |
| 追踪脚本 | `REQUEST_TIMEOUT`、`TRACK17_API_KEY`、`PUSH_FORECAST`、`AUTO_ARCHIVE_DELIVERED` | 追踪脚本下次查询时生效 |
| 追踪脚本 | `AUTO_START_TRACKER` | 重启 Web 后生效（脚本意外退出后是否自动重启读取当前值） |
| 到达预测 | `TRANSIT_REGION_LABEL` | 控制台保存即生效，追踪脚本下次查询时生效 |
| 新状态自动归类 | `AI_CLASSIFY`、`CLAUDE_BRIDGE_URL`、`CLAUDE_BRIDGE_API_KEY`、`AI_MODEL` | 追踪脚本下次查询时生效 |
| 本地 Bark 服务 | `LOCAL_BARK_ENABLED` | 保存即生效（不会停掉已运行的子进程） |
| 本地 Bark 服务 | `BARK_BIND_ADDRESS` | 下次启动本地 Bark 时生效 |
| 本地 Bark 服务 | `AUTO_START_BARK_SERVER` | 重启 Web 后生效 |
| Web 服务 | `APP_PORT` | 重启 Web 后生效 |

提交时按字段类型校验（开关、整数及范围、`http(s)://` 地址），不合法的一项都不写。密钥字段（`TRACK17_API_KEY`、`CLAUDE_BRIDGE_API_KEY`）不回显明文：页面上只显示是否已配置（设置页另有 17TRACK 区块：额度用量、登记单号统计与控制台跳转），原样提交占位符表示不改，清空保存即删除。写回 `.env` 用不加引号的 `KEY=value` 形式（兼容按原样加载 env 文件的容器运行时），含空白、`#`、引号、反斜杠、`$`、反引号的值会被拒绝；留空即清空，`.env` 里出现过的键以文件为准，不回退到启动时的环境变量。

日志写在 `logs/tracker.log`、`logs/bark.log`、`logs/remote_bark.log`，单个文件超过 2 MB 自动轮转，保留 `.1`～`.3` 三份旧文件。

单号、检查间隔（30 秒～7 天）、Bark Keys 等按账号保存在 SQLite（`data/app.db`，表结构见 [docs/data-model.md](docs/data-model.md)），不在 `.env` 里。账号映射也可用命令行维护：`python src/manage.py list-accounts | map-account | unmap-account`；机器接口的调用方令牌见上文「机器接口」。备份请一并保存 `.env`、`data/`、`bark-data/`。

## 许可

MIT
