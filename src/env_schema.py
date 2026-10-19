"""系统设置（.env 白名单键）的唯一定义：分组、文案、类型、范围与生效时机。
白名单、前端表单、保存提示、追踪脚本要重读的键全部由这里派生，避免多处各记一份。
本模块不依赖 Flask，追踪脚本也直接引用。"""
import urllib.parse

# 生效时机（穷举）：这里新增一档，下面所有按时机取值的地方都必须同时补上，漏写会在导入时 KeyError。
# live：只有 Web 进程读，且每次用到时才读 os.environ，保存即生效；
# tracker：Web 与追踪脚本都读，Web 保存即生效，追踪脚本每轮查询前重读 .env，下次查询时生效；
# bark_start：只在启动本地 Bark 子进程时读，下次（重新）启动它时生效；
# restart：只在 Web 进程启动时读一次，必须重启 Web 服务。
APPLY_META = {
    "live": {
        "label": "即时生效",
        "tone": "green",
        "note": "已即时生效：",
    },
    "tracker": {
        "label": "脚本下次查询时生效",
        "tone": "cyan",
        "note": "控制台已即时生效，追踪脚本在下次查询时读取新值：",
    },
    "bark_start": {
        "label": "下次启动 Bark 时生效",
        "tone": "violet",
        "note": "下次启动本地 Bark 服务时生效（已在运行的不受影响）：",
    },
    "restart": {
        "label": "需重启 Web",
        "tone": "amber",
        "note": "需重启 Web 服务后生效：",
    },
}

# 字段类型（穷举）：校验逻辑见 validate_value
FIELD_TYPES = ("text", "bool", "int", "url")

BOOL_TRUE = {"1", "true", "yes", "on"}
BOOL_FALSE = {"0", "false", "no", "off"}

GROUPS = [
    {
        "key": "bark_push",
        "title": "Bark 推送",
        "desc": "决定推送发往哪台 Bark。脚本走内网地址直连，手机客户端用公网 HTTPS 地址注册设备；两者都留空时才会回退到兼容地址。",
        "fields": [
            {
                "key": "BARK_SERVER_INTERNAL",
                "label": "脚本推送地址（内网）",
                "desc": "追踪脚本发推送时访问的地址，同机或同容器网络直连即可，例如 http://127.0.0.1:8080。",
                "type": "url",
                "placeholder": "http://127.0.0.1:8080",
                "apply": "tracker",
                "wide": True,
            },
            {
                "key": "BARK_SERVER_PUBLIC",
                "label": "手机接入地址（公网）",
                "desc": "手机 Bark 客户端注册设备用的 HTTPS 地址，会显示在用户门户的接入指引里。",
                "type": "url",
                "placeholder": "https://bark.example.com",
                "apply": "tracker",
                "wide": True,
            },
            {
                "key": "BARK_SERVER",
                "label": "兼容地址（旧版单地址）",
                "desc": "上面两项留空时的回退值，只为兼容早期「一个地址通用」的配置保留，新部署留空即可。",
                "type": "url",
                "placeholder": "留空",
                "apply": "tracker",
                "legacy": True,
                "wide": True,
            },
        ],
    },
    {
        "key": "bark_health",
        "title": "Bark 健康检查",
        "desc": "控制台探测 Bark 是否在线（首页健康状态）以及发送测试推送时用的请求参数，不影响追踪脚本。",
        "fields": [
            {
                "key": "BARK_HEALTH_PATH",
                "label": "健康检查路径",
                "desc": "拼在 Bark 地址后面的探测路径，留空按 /ping 处理。",
                "type": "text",
                "placeholder": "/ping",
                "apply": "live",
            },
            {
                "key": "BARK_HEALTH_TIMEOUT",
                "label": "探测与测试推送超时（秒）",
                "desc": "单次探测或测试推送的等待上限，留空按 15 秒处理。",
                "type": "int",
                "default": "15",
                "min": 1,
                "max": 120,
                "placeholder": "15",
                "apply": "live",
            },
        ],
    },
    {
        "key": "tracker",
        "title": "追踪脚本",
        "desc": "控制台拉起的追踪脚本如何运行：随 Web 自动启动，以及请求邮政官网与 Bark 时的等待上限。",
        "fields": [
            {
                "key": "AUTO_START_TRACKER",
                "label": "启动时自动运行追踪脚本",
                "desc": "开启后 Web 一起来就拉起追踪脚本，省掉每次手动点启动。脚本意外退出后是否自动重启则读取当前值，保存即生效。",
                "type": "bool",
                "default": "1",
                "apply": "restart",
            },
            {
                "key": "REQUEST_TIMEOUT",
                "label": "请求超时（秒）",
                "desc": "追踪脚本抓取邮政官网、向 Bark 推送时单次请求的等待上限，留空按 15 秒处理。",
                "type": "int",
                "default": "15",
                "min": 1,
                "max": 120,
                "placeholder": "15",
                "apply": "tracker",
            },
        ],
    },
    {
        "key": "local_bark",
        "title": "本地 Bark 服务",
        "badge": "仅控制台托管 Bark 时有效",
        "badge_tone": "amber",
        "desc": "只有让控制台自己以子进程方式跑 Bark Server 时，这一组才有意义。线上 VPS 用的是独立 Bark 容器，整组保持关闭即可。",
        "fields": [
            {
                "key": "LOCAL_BARK_ENABLED",
                "label": "由控制台托管 Bark 子进程",
                "desc": "关闭后首页不再出现本地 Bark 启停按钮，也不能再启动子进程（已在运行的不会被停掉）。独立容器部署应关闭。",
                "type": "bool",
                "default": "1",
                "apply": "live",
            },
            {
                "key": "AUTO_START_BARK_SERVER",
                "label": "Web 启动时自动拉起 Bark",
                "desc": "需要上一项先打开才有效果。",
                "type": "bool",
                "default": "0",
                "apply": "restart",
                # 上一项关掉时这项完全不起作用，置灰避免"以为设了却没生效"
                "requires": "LOCAL_BARK_ENABLED",
            },
            {
                "key": "BARK_BIND_ADDRESS",
                "label": "本地 Bark 监听地址",
                "desc": "托管的 Bark 子进程监听的地址与端口，留空按 0.0.0.0:8080 处理。",
                "type": "text",
                "placeholder": "0.0.0.0:8080",
                "apply": "bark_start",
            },
        ],
    },
    {
        "key": "web_service",
        "title": "Web 服务",
        "desc": "控制台自身的监听参数，改完要重启 Web 服务才会读到。",
        "fields": [
            {
                "key": "APP_PORT",
                "label": "Web 控制台端口",
                "desc": "控制台监听端口，留空按 6060 处理；改了要同步改反向代理的转发目标。",
                "type": "int",
                "default": "6060",
                "min": 1,
                "max": 65535,
                "placeholder": "6060",
                "apply": "restart",
            },
        ],
    },
]

# 白名单：update_env 只认这里出现过的键，防止请求写入任意 .env 键
ENV_FIELDS = {field["key"]: field for group in GROUPS for field in group["fields"]}
ENV_KEYS = list(ENV_FIELDS)
# 追踪脚本每轮查询前要重读的键（读取时机为 tracker 的字段）
TRACKER_ENV_KEYS = [key for key, field in ENV_FIELDS.items() if field["apply"] == "tracker"]


def _check_schema():
    seen = set()
    for key, field in ENV_FIELDS.items():
        if field["apply"] not in APPLY_META:
            raise ValueError(f"{key}: 未知的生效时机 {field['apply']}")
        if field["type"] not in FIELD_TYPES:
            raise ValueError(f"{key}: 未知的字段类型 {field['type']}")
        if field["type"] != "int" and ("min" in field or "max" in field):
            raise ValueError(f"{key}: 只有整数字段能设范围")
        if "requires" in field and ENV_FIELDS[field["requires"]]["type"] != "bool":
            raise ValueError(f"{key}: requires 必须指向开关字段")
        seen.add(key)
    if len(seen) != sum(len(group["fields"]) for group in GROUPS):
        raise ValueError("系统设置里有重复的键")


_check_schema()


def serialize_groups() -> list:
    """下发给前端的分组：生效时机的标签与配色在这里取好，前端只管渲染。"""
    result = []
    for group in GROUPS:
        fields = []
        for field in group["fields"]:
            meta = APPLY_META[field["apply"]]
            fields.append({**field, "apply_label": meta["label"], "apply_tone": meta["tone"]})
        result.append({**group, "fields": fields})
    return result


def apply_note(keys) -> str:
    """只讲这次真改动的字段的生效时机——笼统一句"部分即时部分要重启"没人看得懂。"""
    parts = []
    for apply, meta in APPLY_META.items():
        labels = [
            f"{ENV_FIELDS[key]['label']}（{key}）"
            for key in keys
            if ENV_FIELDS[key]["apply"] == apply
        ]
        if labels:
            parts.append(meta["note"] + "、".join(labels))
    return "；".join(parts)


def validate_value(key: str, text: str) -> str:
    """按字段类型与范围校验并规整一个已通过字符安全检查的值，不合法抛 ValueError。留空表示清空，始终允许。"""
    field = ENV_FIELDS[key]
    name = f"{field['label']}（{key}）"
    if text == "":
        return ""
    kind = field["type"]
    if kind == "bool":
        lowered = text.lower()
        if lowered in BOOL_TRUE:
            return "1"
        if lowered in BOOL_FALSE:
            return "0"
        raise ValueError(f"{name}只能是开启或关闭。")
    if kind == "int":
        try:
            number = int(text)
        except ValueError:
            raise ValueError(f"{name}必须是整数。") from None
        low, high = field.get("min"), field.get("max")
        if (low is not None and number < low) or (high is not None and number > high):
            raise ValueError(f"{name}必须在 {low}～{high} 之间。")
        return str(number)
    if kind == "url":
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"{name}必须是以 http:// 或 https:// 开头的完整地址。")
        return text
    # text：不再做格式限制，字符安全已由调用方检查
    return text
