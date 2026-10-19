# 必须在其余 import 之前打补丁：Socket.IO 跑在 gevent 上，不打补丁时 requests / time.sleep /
# 子进程管道读取都是真阻塞，一个慢请求会卡住整个进程；后台线程也不是协程，从里面 emit 不安全。
# 只在直接运行本文件时打补丁（生产入口 `python src/app.py`），测试以模块方式 import 时不打。
if __name__ == "__main__":
    from gevent import monkey

    monkey.patch_all()

import os
import sys
import time
import secrets

import shutil
import subprocess
import threading

from collections import deque
from datetime import timedelta
from functools import wraps

from flask import Flask, abort, g, send_from_directory, render_template, request, jsonify, redirect, session, url_for
from flask_socketio import SocketIO, emit, disconnect
from dotenv import load_dotenv, set_key
import requests
from szyyw_auth import login_url, sso_enabled
from szyyw_auth.flask import current_identity, is_anonymous

import bark
import env_schema
from storage import (
    archive_task,
    build_tracking_url,
    create_account,
    create_task,
    delete_task,
    ensure_storage,
    EVENT_KIND_META,
    get_account,
    get_account_by_username,
    get_task,
    list_accounts,
    list_task_events,
    load_system_env,
    parse_bark_keys,
    register_user,
    resolve_portal_account,
    set_account_portal_sub,
    update_account,
    update_task,
    verify_account_password,
)

# --- 时区固定为日本 ---
# 容器里即使设置了 TZ，有时也不会自动生效；这里强制设置并调用 tzset。
os.environ.setdefault("TZ", "Asia/Tokyo")
if hasattr(time, "tzset"):
    try:
        time.tzset()
    except Exception:
        pass

# 初始化 Flask 应用
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
DOTENV_PATH = os.path.join(BASE_DIR, '.env')
load_dotenv(DOTENV_PATH)
ensure_storage(DOTENV_PATH)

# 系统设置的分组、类型、生效时机都在 env_schema 里定义一份，白名单与保存提示由它派生
SYSTEM_ENV_GROUPS = env_schema.serialize_groups()
SYSTEM_ENV_KEYS = env_schema.ENV_KEYS
SYSTEM_ENV_FIELDS = env_schema.ENV_FIELDS

def _env_enabled(name: str, default: str = "0") -> bool:
    # 显式留空（KEY=）与未设置同样按默认值处理：系统设置里清空一项后，
    # 运行中（os.environ 设为空串）与重启后（容器按原样加载空值）的行为才一致
    value = os.getenv(name, "").strip() or default
    return value.lower() in {"1", "true", "yes", "on"}

def get_bark_internal_url() -> str:
    return bark.internal_url(os.environ)

def get_bark_public_url() -> str:
    return bark.public_url(os.environ)

def get_bark_health_timeout() -> int:
    try:
        timeout = int(os.getenv("BARK_HEALTH_TIMEOUT", "15"))
        return timeout if timeout > 0 else 15
    except Exception:
        return 15

def get_bark_health_path() -> str:
    health_path = os.getenv("BARK_HEALTH_PATH", "/ping").strip() or "/ping"
    return health_path if health_path.startswith("/") else "/" + health_path

def get_bark_bind_address() -> str:
    return os.getenv("BARK_BIND_ADDRESS", "0.0.0.0:8080").strip() or "0.0.0.0:8080"

def get_bark_executable_candidates():
    candidates = [
        os.getenv("BARK_EXECUTABLE", "").strip(),
        os.path.join(BASE_DIR, "bark-server"),
        os.path.join(BASE_DIR, "bark-server_linux_arm64"),
        os.path.join(BASE_DIR, "bark-server_linux_amd64"),
        os.path.join(BASE_DIR, "bark-server_darwin_arm64"),
        os.path.join(BASE_DIR, "bark-server_darwin_amd64"),
        shutil.which("bark-server") or "",
    ]
    seen = set()
    result = []
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result

def get_bark_executable() -> str:
    for candidate in get_bark_executable_candidates():
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    candidates = get_bark_executable_candidates()
    return candidates[0] if candidates else os.path.join(BASE_DIR, "bark-server")

def get_app_port() -> int:
    try:
        return int(os.getenv("APP_PORT", "6060"))
    except Exception:
        return 6060

def get_debug_enabled() -> bool:
    return _env_enabled("FLASK_DEBUG")

def get_secret_key() -> str:
    configured = os.getenv("SECRET_KEY", "").strip()
    return configured or secrets.token_hex(32)

def get_session_cookie_secure() -> bool:
    return _env_enabled("SESSION_COOKIE_SECURE")

def get_admin_username() -> str:
    return os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"

def get_admin_session_hours() -> int:
    try:
        hours = int(os.getenv("ADMIN_SESSION_HOURS", "24"))
        return hours if hours > 0 else 24
    except Exception:
        return 24

def get_login_max_attempts() -> int:
    try:
        attempts = int(os.getenv("ADMIN_LOGIN_MAX_ATTEMPTS", "5"))
        return attempts if attempts > 0 else 5
    except Exception:
        return 5

def get_login_window_seconds() -> int:
    try:
        seconds = int(os.getenv("ADMIN_LOGIN_WINDOW_SECONDS", "600"))
        return seconds if seconds > 0 else 600
    except Exception:
        return 600

# --- portal SSO（SZYYW_SSO=1 时启用）---
# 开启后身份完全来自 Caddy 门卫注入的 X-Portal-Sub / X-User / X-Role，本地密码登录与自助注册关闭；
# 账号按 X-Portal-Sub（portal 账号固定 ID）映射，X-User 只是当前用户名（用户可改）；
# 未开启时下面所有 SSO 分支都不走，行为与接入前一致。

def get_portal_origin() -> str:
    return (os.getenv("PORTAL_ORIGIN", "").strip() or "https://szyyw.xyz").rstrip("/")

# --- 设计包 @szyyw/design：直接引用自托管 CDN（design.szyyw.xyz），仓库里不再放副本 ---
# 版本只在这一处声明；升级 = 改这行（scripts/update-design.sh / upgrade-shared.sh 会改它并验证 CDN 上存在）。
# 每个 tag 发布后不可变，一页里所有设计包文件必须同一版本（模块间相对 import）。禁止用 /latest/。
DESIGN_VERSION = "v0.22.1"
DESIGN_CDN_ORIGIN = "https://design.szyyw.xyz"

def get_design_base() -> str:
    # 环境变量 DESIGN_BASE 可覆盖（本地离线开发指向本机设计包的静态服务，见 README）
    return (os.getenv("DESIGN_BASE", "").strip() or f"{DESIGN_CDN_ORIGIN}/{DESIGN_VERSION}").rstrip("/")

def sso_autocreate_enabled() -> bool:
    # 默认关：未映射的 portal 用户看到"尚未开通"，由管理员手动映射
    return _env_enabled("SZYYW_SSO_AUTOCREATE", "0")

def sso_adopt_by_name_enabled() -> bool:
    # 默认关：按 X-User 同名认领未映射的本地账号。portal 用户名用户自己能改，
    # 开着就等于谁改成某个本地用户名谁就能接管那个账号；只在一次性迁移旧账号时临时打开
    return _env_enabled("SZYYW_SSO_ADOPT_BY_NAME", "0")

def external_url(path: str) -> str:
    """浏览器看到的绝对地址（Caddy 会带 X-Forwarded-Proto/Host），用作 portal 登录的 rd。"""
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    host = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    base = f"{proto or request.scheme}://{host}" if host else request.host_url.rstrip("/")
    return base + (path if path.startswith("/") else "/" + path)

def resolve_sso_account():
    """按门卫身份解析本地账号，返回 (account, reason)，同一请求内只查一次。
    reason: None=正常 / "no_identity"=没有身份头或缺 X-Portal-Sub（绕过了门卫）/
            "not_provisioned"=未映射且未开自动开通 / "disabled"=本地账号被停用登录。"""
    cached = getattr(g, "_sso_resolution", None)
    if cached is not None:
        return cached
    ident = current_identity()
    # sub 直接读头：szyyw_auth 在缺 X-Portal-Sub 时会退回 X-User，而这里绝不能只按用户名匹配
    portal_sub = request.headers.get("X-Portal-Sub", "").strip()
    if ident is None or not portal_sub:
        result = (None, "no_identity")
    else:
        account, action = resolve_portal_account(
            portal_sub,
            ident.user,
            ident.role,
            autocreate=sso_autocreate_enabled(),
            adopt_by_name=sso_adopt_by_name_enabled(),
        )
        if action == "adopted":
            log_tracker(_fmt('[SSO]', f"portal 用户 {ident.user}（sub {portal_sub}）认领同名本地账号 id={account['id']}"))
        elif action == "created":
            log_tracker(_fmt('[SSO]', f"portal 用户 {ident.user}（sub {portal_sub}）自动开通本地账号 {account['username']} id={account['id']}"))
        if account is None:
            result = (None, "not_provisioned")
        elif not account.get("login_enabled"):
            # login_enabled 在 SSO 下仍是本站的停用开关
            result = (None, "disabled")
        else:
            result = (account, None)
    g._sso_resolution = result
    return result

def current_account():
    if sso_enabled():
        return resolve_sso_account()[0]
    account_id = session.get("account_id")
    if not account_id:
        return None
    # 同一请求内缓存查库结果，避免鉴权判断重复访问数据库
    cached = getattr(g, "_current_account", None)
    if cached is not None and cached.get("id") == int(account_id):
        return cached
    account = get_account(int(account_id))
    g._current_account = account
    return account


def is_admin() -> bool:
    account = current_account()
    if sso_enabled():
        # SSO 下授权角色以每个请求的 X-Role 为准（portal 权限矩阵决定），
        # 数据库里的 role 字段不参与授权、也不被改写
        ident = current_identity()
        return bool(account and ident and ident.is_admin and account.get("login_enabled"))
    # 角色以数据库为准；session 里缓存的角色在管理员被降级后不会失效，不可信
    return bool(account and account.get("role") == "admin" and account.get("login_enabled"))

def current_actor_role() -> str:
    """传给 storage 做归属校验的角色。走 is_admin() 而不是直接读 role 字段，
    这样管理员被停用登录后立刻失去越权改他人任务的能力。"""
    return "admin" if is_admin() else "user"

def is_authenticated() -> bool:
    account = current_account()
    return bool(account and account.get("login_enabled"))

def is_anonymous_visitor() -> bool:
    """portal 门卫标记的匿名访客（X-Portal-Anon: 1 且没有身份头）。SSO 未开时恒为 False，
    客户端自带的 X-Portal-Anon 不起作用。门卫会先剥掉客户端带的该头再按 portal 公开名单写入。"""
    return is_anonymous()

# 匿名访客只能看的页面：首页公开落地页。所有数据接口、表单、Socket.IO 仍按未登录处理（401/拒绝）
ANON_PAGE_ENDPOINTS = {'index'}

def _ts() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')

def _ensure_nl(s: str) -> str:
    return s if s.endswith("\n") else s + "\n"

def _fmt(tag: str, message: str) -> str:
    return f"{_ts()} {tag} {message}".rstrip() + "\n"

# --- 日志：文件按大小轮转，内存只留尾部 ---
# 以前三个日志文件无限增长，启动时整份读进内存、每次 Socket 连接整份下发，越跑越慢。
LOG_MAX_BYTES = 2 * 1024 * 1024   # 单个日志文件上限
LOG_BACKUP_COUNT = 3              # 轮转保留的旧文件份数（xxx.log.1 ~ xxx.log.3）
LOG_TAIL_LINES = 500              # 内存缓存与连接时下发的行数，与前端 MAX_LOG_LINES 一致
LOG_TAIL_READ_BYTES = 256 * 1024  # 启动时只读文件末尾这么多字节来恢复尾部

def _rotate_log_file(filepath: str, max_bytes: int = LOG_MAX_BYTES, backup_count: int = LOG_BACKUP_COUNT):
    """文件超过上限就整体后移：xxx.log → .1 → .2 …，最旧的一份被覆盖。"""
    try:
        if os.path.getsize(filepath) < max_bytes:
            return
    except OSError:
        return
    for index in range(backup_count - 1, 0, -1):
        src = f"{filepath}.{index}"
        if os.path.exists(src):
            os.replace(src, f"{filepath}.{index + 1}")
    if backup_count > 0:
        os.replace(filepath, f"{filepath}.1")
    else:
        os.remove(filepath)

def _read_tail_lines(filepath: str, limit: int = LOG_TAIL_LINES) -> list:
    try:
        with open(filepath, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_READ_BYTES))
            data = f.read()
    except OSError:
        return []
    lines = data.decode('utf-8', errors='replace').splitlines(keepends=True)
    if size > LOG_TAIL_READ_BYTES and lines:
        # 从文件中间截断读的，第一行大概率不完整
        lines = lines[1:]
    return lines[-limit:]

class LogChannel:
    """一类日志：追加写文件（带轮转）+ 内存尾部缓存 + 推送给前端。"""

    def __init__(self, filepath: str, event: str, tail_lines: int = LOG_TAIL_LINES):
        self.filepath = filepath
        self.event = event
        self.lines = deque(_read_tail_lines(filepath, tail_lines), maxlen=tail_lines)
        self.lock = threading.Lock()

    def append(self, line: str):
        with self.lock:
            self.lines.append(line)
            try:
                _rotate_log_file(self.filepath, LOG_MAX_BYTES, LOG_BACKUP_COUNT)
                with open(self.filepath, 'a', encoding='utf-8') as f:
                    f.write(line)
            except OSError:
                # 写盘失败不能影响业务流程，内存里仍有这一行
                pass
        try:
            socketio.emit(self.event, {'data': line})
        except Exception:
            pass

    def tail(self) -> str:
        with self.lock:
            return "".join(self.lines)

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'),
            static_folder=os.path.join(os.path.dirname(__file__), 'static'))
app.config.update(
    SECRET_KEY=get_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=get_session_cookie_secure(),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=get_admin_session_hours()),
)
socketio = SocketIO(app, async_mode='gevent') # 保持默认同源限制，避免公开管理页时允许任意来源建立 Socket.IO 连接

# --- Bark 服务相关变量 ---
BARK_DATA_DIR = os.path.join(BASE_DIR, 'bark-data')
TRACKER_SCRIPT = os.path.join(os.path.dirname(__file__), 'tracker.py')
bark_server_process = None
bark_server_thread = None

# --- 脚本运行状态变量 ---
script_process = None
script_thread = None
# 区分"用户主动停止"与"脚本意外退出"，后者在自动模式下会被拉起来
script_stop_requested = False

# --- 分离的日志缓存及文件 ---
TRACKER_LOG_FILE = os.path.join(LOG_DIR, 'tracker.log')
BARK_LOG_FILE = os.path.join(LOG_DIR, 'bark.log')
REMOTE_BARK_LOG_FILE = os.path.join(LOG_DIR, 'remote_bark.log')
tracker_log = LogChannel(TRACKER_LOG_FILE, 'tracker_log')
bark_log = LogChannel(BARK_LOG_FILE, 'bark_log')
remote_bark_log = LogChannel(REMOTE_BARK_LOG_FILE, 'remote_bark_log')
login_attempts = {}
login_attempts_lock = threading.Lock()

def log_remote_bark(line: str):
    """写入远程 Bark 健康检测日志，并推送到前端。"""
    remote_bark_log.append(_ensure_nl(line))

def log_tracker(line: str):
    tracker_log.append(_ensure_nl(line))

def log_bark(line: str):
    bark_log.append(_ensure_nl(line))

def get_trusted_proxy_count() -> int:
    try:
        count = int(os.getenv("TRUSTED_PROXY_COUNT", "0"))
        return count if count > 0 else 0
    except Exception:
        return 0

def get_client_ip() -> str:
    # 客户端可以自带伪造的 X-Forwarded-For，取最左值会让登录限流形同虚设。
    # 只有在明确配置了受信反代层数时才采信转发头，且取右数第 N 个值
    # （右侧 N 个由受信代理追加，无法伪造）；否则一律用直连地址。
    trusted = get_trusted_proxy_count()
    if trusted > 0:
        values = [item.strip() for item in request.headers.get("X-Forwarded-For", "").split(",") if item.strip()]
        if len(values) >= trusted:
            return values[-trusted]
    return request.remote_addr or "unknown"

def _prune_login_attempts(entries, now):
    window = get_login_window_seconds()
    return [ts for ts in entries if now - ts < window]

def record_login_failure(ip: str):
    now = time.time()
    with login_attempts_lock:
        entries = _prune_login_attempts(login_attempts.get(ip, []), now)
        entries.append(now)
        login_attempts[ip] = entries

def clear_login_failures(ip: str):
    with login_attempts_lock:
        login_attempts.pop(ip, None)

def login_is_rate_limited(ip: str) -> bool:
    now = time.time()
    with login_attempts_lock:
        entries = _prune_login_attempts(login_attempts.get(ip, []), now)
        login_attempts[ip] = entries
        return len(entries) >= get_login_max_attempts()

def build_next_target(default: str = "/") -> str:
    candidate = (request.args.get("next") or request.form.get("next") or default).strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return default
    return candidate

def current_request_target() -> str:
    query = request.query_string.decode().strip()
    return request.path + (f"?{query}" if query else "")

def wants_json_response() -> bool:
    accept = request.headers.get("Accept", "")
    return (
        request.is_json
        or request.path.startswith("/api/")
        or request.path.startswith("/update_env")
        or request.path.startswith("/remote_bark_status")
        or "application/json" in accept
    )

def sso_forbidden_response(reason: str):
    """门卫放行了、但本站没有对应账号（或已停用）：403，不再跳 portal，否则会来回跳。"""
    message = (
        "此账号已在 JPPost 停用，请联系管理员。"
        if reason == "disabled"
        else "此账号尚未在 JPPost 开通，请联系管理员。"
    )
    if wants_json_response():
        return jsonify({"status": "error", "message": message}), 403
    ident = current_identity()
    return render_template(
        'sso_denied.html',
        message=message,
        portal_user=ident.user if ident else "",
    ), 403

def unauthorized_response():
    if sso_enabled():
        _, reason = resolve_sso_account()
        if reason in ("not_provisioned", "disabled"):
            return sso_forbidden_response(reason)
        if wants_json_response():
            return jsonify({"status": "error", "message": "未登录或会话已过期。"}), 401
        return redirect(login_url(get_portal_origin(), external_url(current_request_target())))
    if wants_json_response():
        return jsonify({"status": "error", "message": "未登录或会话已过期。"}), 401
    return redirect(url_for('login', next=current_request_target()))

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return unauthorized_response()
        return view(*args, **kwargs)
    return wrapped

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return unauthorized_response()
        if not is_admin():
            if wants_json_response():
                return jsonify({"status": "error", "message": "需要管理员权限。"}), 403
            return redirect(url_for('index'))
        return view(*args, **kwargs)
    return wrapped

def socket_admin_required(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if not is_authenticated() or not is_admin():
            emit('auth_error', {'message': '未登录、会话已过期，或缺少管理员权限。'})
            disconnect()
            return
        return handler(*args, **kwargs)
    return wrapped

@app.before_request
def require_auth():
    if request.endpoint in {'login', 'register', 'logout', 'healthz', 'static', 'favicon_svg', 'favicon_ico', 'apple_touch_icon'}:
        return None
    if request.path.startswith('/socket.io'):
        return None
    if not is_authenticated():
        if request.endpoint in ANON_PAGE_ENDPOINTS and request.method in ('GET', 'HEAD') and is_anonymous_visitor():
            return None
        return unauthorized_response()
    return None

@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path in {'/', '/login', '/register', '/logout', '/update_env', '/remote_bark_status'}:
        response.headers["Cache-Control"] = "no-store"
    return response

@app.context_processor
def inject_sso_flags():
    # 模板据此隐藏注册/改密入口、挂载应用切换器；关闭时模板输出与以前完全一样
    # DESIGN_BASE：模板里设计包 CSS 与 import map 的前缀（见 _design_head.html）
    # EVENT_KIND_META：时间线事件类型的文案与配色，门户模板直接用
    return {
        "sso_on": sso_enabled(),
        "portal_origin": get_portal_origin(),
        "DESIGN_BASE": get_design_base(),
        "EVENT_KIND_META": EVENT_KIND_META,
    }

ICON_DIR = os.path.join(os.path.dirname(__file__), 'static', 'icons')

def _serve_icon(filename, mimetype):
    resp = send_from_directory(ICON_DIR, filename, mimetype=mimetype)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp

@app.route('/favicon.svg', methods=['GET'])
def favicon_svg():
    return _serve_icon('favicon.svg', 'image/svg+xml')

@app.route('/favicon.ico', methods=['GET'])
def favicon_ico():
    return _serve_icon('favicon.ico', 'image/x-icon')

@app.route('/apple-touch-icon.png', methods=['GET'])
def apple_touch_icon():
    return _serve_icon('apple-touch-icon.png', 'image/png')

@app.route('/healthz', methods=['GET'])
def healthz():
    return jsonify({"status": "ok"})

@app.route('/login', methods=['GET', 'POST'])
def login():
    next_target = build_next_target()
    if sso_enabled():
        # 登录归 portal：已带身份就直接进，没开通给 403，否则去 portal 登录后跳回
        account, reason = resolve_sso_account()
        if account is not None:
            return redirect(next_target)
        if reason in ("not_provisioned", "disabled"):
            return sso_forbidden_response(reason)
        return redirect(login_url(get_portal_origin(), external_url(next_target)))
    if is_authenticated():
        return redirect(next_target)

    error = None
    if request.method == 'POST':
        ip = get_client_ip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if login_is_rate_limited(ip):
            error = "登录失败次数过多，请稍后再试。"
        else:
            account = get_account_by_username(username, include_secret=True)
            is_valid = (
                account is not None
                and account.get("login_enabled")
                and verify_account_password(account, password)
            )
            if is_valid:
                clear_login_failures(ip)
                session.clear()
                session.permanent = True
                session['account_id'] = account['id']
                session['account_username'] = account['username']
                return redirect(next_target)
            record_login_failure(ip)
            error = "用户名或密码错误。"

    return render_template(
        'login.html',
        error=error,
        next_target=next_target,
        admin_username=get_admin_username(),
    )

@app.route('/register', methods=['GET', 'POST'])
def register():
    if sso_enabled():
        # 账号由 portal 统一开通，本站自助注册关闭
        abort(404)
    next_target = build_next_target()
    if is_authenticated():
        return redirect(next_target)

    if request.method == 'GET':
        return render_template('register.html', error=None, next_target=next_target)

    form = request.form
    try:
        account = register_user(
            {
                "username": form.get("register_username", ""),
                "display_name": form.get("register_display_name", ""),
                "password": form.get("register_password", ""),
            }
        )
        session.clear()
        session.permanent = True
        session['account_id'] = account['id']
        session['account_username'] = account['username']
        return redirect(next_target)
    except Exception as exc:
        return render_template(
            'register.html',
            error=str(exc),
            next_target=next_target,
        ), 400

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    if sso_enabled():
        # 会话归 portal 管，本地只清 cookie；回 portal 首页
        return redirect(get_portal_origin())
    return redirect(url_for('login'))

def build_viewer_state():
    account = current_account()
    if sso_enabled():
        # 页面展示的角色与授权一致，取 X-Role 而不是库里的 role
        role = ("admin" if is_admin() else "user") if account else ""
        ident = current_identity()
        return {
            "account": account,
            "role": role,
            "is_admin": role == "admin",
            "username": account["username"] if account else "",
            "display_name": account["display_name"] if account else "",
            # portal 当前用户名（X-User，可改）；本地 username 是数据键，不跟着改
            "portal_user": ident.user if account and ident else "",
        }
    return {
        "account": account,
        "role": account["role"] if account else "",
        "is_admin": bool(account and account["role"] == "admin"),
        "username": account["username"] if account else "",
        "display_name": account["display_name"] if account else "",
    }

def build_user_state():
    users = list_accounts()
    return {
        "users": users,
        "task_count": sum(int(user.get("task_count") or 0) for user in users),
        "active_task_count": sum(int(user.get("active_task_count") or 0) for user in users),
        "login_count": len([user for user in users if user.get("login_enabled")]),
    }

def build_account_state():
    """普通用户视角的刷新载荷：重新查库，因为 g 上缓存的账号还带着旧任务列表。"""
    account = current_account()
    return get_account(account["id"]) if account else None

def build_system_env():
    system_env = load_system_env(DOTENV_PATH, SYSTEM_ENV_KEYS)
    return {key: system_env.get(key, "") for key in SYSTEM_ENV_KEYS}

def build_env_apply_note(keys) -> str:
    return env_schema.apply_note(keys)

def build_bark_help_state():
    return {
        "public_url": get_bark_public_url(),
        "internal_url": get_bark_internal_url(),
        "health_path": get_bark_health_path(),
        "local_bark_enabled": local_bark_enabled(),
        "tracker_auto_start": tracker_auto_start_enabled(),
    }

def check_bark_endpoint(bark_url: str, label: str = "BARK") -> dict:
    health_url = f"{bark_url}{get_bark_health_path()}"
    log_remote_bark(_fmt('[REMOTE_BARK]', f"CHECK {label} {health_url}"))
    start = time.time()
    try:
        resp = requests.get(health_url, timeout=get_bark_health_timeout())
        latency_ms = int((time.time() - start) * 1000)
        log_remote_bark(_fmt('[REMOTE_BARK]', f"OK {label} HTTP {resp.status_code} {latency_ms}ms"))
        return {
            "configured": True,
            "url": bark_url,
            "checked_url": bark_url,
            "ok": resp.status_code == 200,
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
            "error": None if resp.status_code == 200 else f"HTTP {resp.status_code}",
            "fallback_used": False,
            "note": None,
        }
    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        log_remote_bark(_fmt('[REMOTE_BARK]', f"ERROR {label} {latency_ms}ms {e}"))
        return {
            "configured": True,
            "url": bark_url,
            "checked_url": bark_url,
            "ok": False,
            "status_code": None,
            "latency_ms": latency_ms,
            "error": str(e),
            "fallback_used": False,
            "note": None,
        }

def _bool_from_input(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

def resolve_test_tracking_url(data, source: dict) -> str:
    """测试推送里 url 参数的来源。Bark 配置挂在账号上、单号挂在任务上，
    所以只有请求显式指定了任务或单号才带链接，否则不带。"""
    task_id = str(data.get("task_id", "") or "").strip()
    if task_id and source.get("id"):
        try:
            task = get_task(int(task_id))
        except (TypeError, ValueError):
            raise ValueError("追踪任务 ID 无效。")
        if not task or int(task["account_id"]) != int(source["id"]):
            raise ValueError("追踪任务不存在或不属于该账号。")
        return task["tracking_url"]

    number = "".join(str(data.get("tracking_number", "") or "").split()).upper()
    return build_tracking_url(number) if number else ""

def build_bark_test_account(source_account: dict | None, data) -> dict:
    source = source_account or {}
    if hasattr(data, "getlist"):
        # 传统表单里未勾选的复选框根本不会提交，只能按键是否存在判断
        bark_url_enabled = 'bark_url_enabled' in data
        bark_keys = data.get("bark_keys", "")
    else:
        bark_url_enabled = _bool_from_input(
            data.get("bark_url_enabled"),
            default=bool(source.get("bark_url_enabled")),
        )
        # 按键是否存在判断：表单里把 Keys 清空后测试，应当提示"没有 Keys"，而不是偷偷用已保存的
        if "bark_keys" in data:
            bark_keys = data.get("bark_keys") or ""
        else:
            bark_keys = source.get("bark_keys", "")

    return {
        "display_name": str(data.get("display_name", source.get("display_name", "")) or "").strip() or source.get("display_name") or "当前用户",
        "bark_keys": bark_keys,
        "bark_query_params": str(data.get("bark_query_params", source.get("bark_query_params", "")) or "").strip(),
        "bark_url_enabled": bark_url_enabled,
        "tracking_url": resolve_test_tracking_url(data, source),
    }

def extract_bark_test_message(data, *, default_title: str, default_body: str) -> tuple[str, str]:
    title = str(data.get("test_title", default_title) or default_title).strip() or default_title
    body = str(data.get("test_body", default_body) or default_body).strip() or default_body
    return title, body

def send_bark_test_push(account: dict, title: str = "测试推送", body: str = "Bark 设置已生效。") -> dict:
    bark_server = get_bark_internal_url()
    bark_keys = parse_bark_keys(account.get("bark_keys"))
    if not bark_server:
        raise ValueError("未配置 Bark 服务地址。")
    if not bark_keys:
        raise ValueError("当前用户还没有配置 Bark Keys。")

    params = bark.build_params(
        account.get("bark_query_params", ""), account.get("bark_url_enabled"), account.get("tracking_url", "")
    )
    resp = bark.send_push(bark_server, bark_keys, title, body, params, get_bark_health_timeout())

    return {
        "ok": 200 <= resp.status_code < 300,
        "status_code": resp.status_code,
        "devices": len(bark_keys),
        "response_text": resp.text[:500],
    }

@app.route('/')
def index():
    if not is_authenticated():
        # SSO 匿名访客：只给页面壳 + 公开介绍；其余（含 SSO 未开、没有任何门卫头）照旧跳登录
        if is_anonymous_visitor():
            return render_template('anon_landing.html')
        return unauthorized_response()
    viewer_state = build_viewer_state()
    account = viewer_state["account"]
    bark_help = build_bark_help_state()
    if viewer_state["is_admin"]:
        system_env = build_system_env()
        user_state = build_user_state()
        initial_tracker_log = tracker_log.tail()
        initial_bark_log = bark_log.tail()
        return render_template(
            'index.html',
            viewer_state=viewer_state,
            env_vars=system_env,
            env_schema=SYSTEM_ENV_GROUPS,
            user_state=user_state,
            bark_help=bark_help,
            initial_tracker_log=initial_tracker_log,
            initial_bark_log=initial_bark_log,
        )

    return render_template(
        'user_portal.html',
        viewer_state=viewer_state,
        account=account,
        task_events=build_portal_task_events(account),
        event_page_size=PORTAL_EVENT_PREVIEW,
        bark_help=bark_help,
        message=request.args.get("message", "").strip(),
        status=request.args.get("status", "").strip() or "success",
    )

@socketio.on('connect')
def test_connect(auth=None):
    """客户端连接时发送当前所有服务的状态。"""
    if not is_authenticated() or not is_admin():
        return False
    print('Client connected', flush=True)
    emit('script_status', {'running': script_process is not None and script_process.poll() is None})
    emit('bark_server_status', {'running': bark_server_process is not None and bark_server_process.poll() is None})
    # 只下发最近 LOG_TAIL_LINES 行，完整历史在日志文件里
    emit('full_tracker_log', {'data': tracker_log.tail()})
    emit('full_bark_log', {'data': bark_log.tail()})
    emit('full_remote_bark_log', {'data': remote_bark_log.tail()})

# --- 追踪脚本控制 ---

def read_script_output(proc):
    """从追踪脚本进程的管道中实时读取输出，并发送到独立的事件。
    只操作启动时捕获的 proc：停止后立刻再启动时，全局 script_process 已经是新进程，
    旧读线程收尾若去引用全局变量，会把新进程的管道关掉、把状态清成"未运行"。"""
    global script_process
    stream = proc.stdout
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ''):
            log_tracker(_fmt('[TRACKER]', line.rstrip()))
    except Exception as e:
        log_tracker(_fmt('[TRACKER]', f"ERROR: 读取脚本输出时发生错误: {e}"))
    finally:
        try:
            stream.close()
        except Exception:
            pass
        return_code = proc.wait()
        log_tracker(_fmt('[TRACKER]', f"脚本已停止，返回码: {return_code}"))
        # 已经有新进程接班时，状态归新进程管，这里只记一笔日志
        if script_process is proc:
            socketio.emit('script_status', {'running': False})
            script_process = None

            # 自动模式下，意外退出（非用户主动停止）10 秒后自动拉起
            if tracker_auto_start_enabled() and not script_stop_requested:
                log_tracker(_fmt('[SYSTEM]', "检测到脚本意外退出，10 秒后自动重启。"))
                threading.Thread(target=_delayed_tracker_restart, daemon=True).start()

def tracker_auto_start_enabled() -> bool:
    return _env_enabled("AUTO_START_TRACKER", "1")

def _delayed_tracker_restart():
    time.sleep(10)
    # 等待期间用户可能手动停止或已重新启动，再核对一次
    if script_stop_requested or (script_process is not None and script_process.poll() is None):
        return
    start_tracker_script(start_reason="auto")

def start_tracker_script(start_reason: str = "manual") -> bool:
    global script_process, script_thread, script_stop_requested
    if script_process is not None and script_process.poll() is None:
        log_tracker(_fmt('[TRACKER]', "脚本已经在运行中。"))
        socketio.emit('script_status', {'running': True})
        return False

    action = "自动启动" if start_reason == "auto" else "正在启动"
    log_tracker(_fmt('[SYSTEM]', f"{action}追踪脚本..."))
    try:
        script_stop_requested = False
        script_process = subprocess.Popen(
            [sys.executable, '-u', TRACKER_SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        log_tracker(_fmt('[TRACKER]', "脚本已启动。"))
        socketio.emit('script_status', {'running': True})

        script_thread = threading.Thread(target=read_script_output, args=(script_process,), daemon=True)
        script_thread.start()
        return True
    except Exception as e:
        log_tracker(_fmt('[TRACKER]', f"启动脚本失败: {e}"))
        socketio.emit('script_status', {'running': False})
        return False

@socketio.on('start_script')
@socket_admin_required
def start_script():
    """启动 Python 追踪脚本。"""
    start_tracker_script()

@socketio.on('stop_script')
@socket_admin_required
def stop_script():
    """终止 Python 追踪脚本。"""
    global script_process, script_stop_requested
    if script_process is not None and script_process.poll() is None:
        # 标记为主动停止，避免自动重启机制把它拉起来
        script_stop_requested = True
        log_tracker(_fmt('[SYSTEM]', "终止追踪脚本信号已发送。"))
        script_process.terminate()
    else:
        log_tracker(_fmt('[TRACKER]', "脚本未运行。"))
        socketio.emit('script_status', {'running': False})

# --- Bark 服务控制 ---

def read_bark_output(proc):
    """从 Bark 服务进程的管道中实时读取输出，并发送到独立的事件。
    与 read_script_output 一样只操作自己捕获的 proc，不碰可能已被替换的全局变量。"""
    global bark_server_process
    stream = proc.stdout
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ''):
            log_bark(_fmt('[BARK]', line.rstrip()))
    except Exception as e:
        log_bark(_fmt('[BARK]', f"ERROR: 读取服务输出时发生错误: {e}"))
    finally:
        try:
            stream.close()
        except Exception:
            pass
        return_code = proc.wait()
        log_bark(_fmt('[BARK]', f"服务已停止，返回码: {return_code}"))
        if bark_server_process is proc:
            socketio.emit('bark_server_status', {'running': False})
            bark_server_process = None

def local_bark_enabled() -> bool:
    # VPS 容器部署下 Bark 由 compose 独立管理，设 LOCAL_BARK_ENABLED=0
    # 可以隐藏控制台里的本地 Bark 启停卡片并禁用子进程管理
    return _env_enabled("LOCAL_BARK_ENABLED", "1")

def start_local_bark_server(start_reason: str = "manual") -> bool:
    global bark_server_process, bark_server_thread
    if not local_bark_enabled():
        log_bark(_fmt('[BARK]', "本地 Bark 管理已禁用（LOCAL_BARK_ENABLED=0），Bark 由部署环境独立运行。"))
        socketio.emit('bark_server_status', {'running': False})
        return False
    if bark_server_process is not None and bark_server_process.poll() is None:
        log_bark(_fmt('[BARK]', "服务已经在运行中。"))
        socketio.emit('bark_server_status', {'running': True})
        return False

    action = "自动启动" if start_reason == "auto" else "正在启动"
    log_bark(_fmt('[SYSTEM]', f"{action} Bark 服务..."))

    bark_executable = get_bark_executable()
    if not (os.path.isfile(bark_executable) and os.access(bark_executable, os.X_OK)):
        tried = ", ".join(get_bark_executable_candidates())
        log_bark(_fmt('[BARK]', f"启动失败: 未找到可执行文件。已尝试: {tried}"))
        socketio.emit('bark_server_status', {'running': False})
        return False
    
    try:
        os.makedirs(BARK_DATA_DIR, exist_ok=True)
        bark_server_process = subprocess.Popen(
            [bark_executable, '-addr', get_bark_bind_address(), '-data', BARK_DATA_DIR],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        log_bark(_fmt('[BARK]', f"服务已启动。监听地址: {get_bark_bind_address()}"))
        socketio.emit('bark_server_status', {'running': True})
        
        bark_server_thread = threading.Thread(target=read_bark_output, args=(bark_server_process,), daemon=True)
        bark_server_thread.start()
        return True
    except Exception as e:
        log_bark(_fmt('[BARK]', f"启动服务失败: {e}"))
        socketio.emit('bark_server_status', {'running': False})
        return False

@socketio.on('start_bark_server')
@socket_admin_required
def start_bark_server():
    """启动 Bark 服务。"""
    start_local_bark_server()

@socketio.on('stop_bark_server')
@socket_admin_required
def stop_bark_server():
    """终止 Bark 服务。"""
    global bark_server_process
    if bark_server_process is not None and bark_server_process.poll() is None:
        log_bark(_fmt('[SYSTEM]', "终止 Bark 服务信号已发送。"))
        bark_server_process.terminate()
    else:
        log_bark(_fmt('[BARK]', "服务未运行。"))
        socketio.emit('bark_server_status', {'running': False})

# --- 环境变量更新 ---

# 无引号写入 .env 时会出问题的字符：空白（截断 / 换行注入）、# （被当成注释）、
# 引号与反斜杠（解析器会去引号 / 转义，与容器原样加载的结果不一致）、$ 与反引号（变量展开）
ENV_UNSAFE_CHARS = set("#'\"\\$`")

def normalize_env_value(key: str, value) -> str:
    """把系统设置提交的值规整成可安全无引号写入 .env 的字符串；字符不安全或不符合字段类型、范围时抛 ValueError。"""
    label = (SYSTEM_ENV_FIELDS.get(key) or {}).get("label", key)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if not isinstance(value, (str, int, float)):
        raise ValueError(f"{label}（{key}）的值格式不正确。")
    text = str(value).strip()
    if any(ch.isspace() for ch in text) or any(ch in ENV_UNSAFE_CHARS for ch in text):
        raise ValueError(f"{label}（{key}）不能包含空白或 # ' \" \\ $ ` 等字符。")
    return env_schema.validate_value(key, text)

@app.route('/update_env', methods=['POST'])
@admin_required
def update_env():
    data = request.get_json()
    if data is None:
        return jsonify({"status": "error", "message": "无效的请求数据"}), 400

    if len(data) == 0:
        return jsonify({"status": "success", "message": "没有检测到需要更新的变量。"})

    if not os.path.exists(DOTENV_PATH):
        open(DOTENV_PATH, 'a').close()

    raw_changes = {key: value for key, value in data.items() if key in SYSTEM_ENV_KEYS}
    if not raw_changes:
        return jsonify({"status": "error", "message": "没有可更新的系统配置。"}), 400

    # 先整体校验再写：有一项不合法就一项都不写，避免写一半
    system_changes = {}
    invalid = []
    for key, value in raw_changes.items():
        try:
            system_changes[key] = normalize_env_value(key, value)
        except ValueError as exc:
            invalid.append(str(exc))
    if invalid:
        return jsonify({"status": "error", "message": "；".join(invalid)}), 400

    updated_keys = []
    errors = []
    for key, value in system_changes.items():
        try:
            # 不加引号：生产容器按原样（raw）把这个文件加载成环境变量，引号会原样留在值里，
            # 写成 KEY='1' 会让 _env_enabled 之类的判断全部失效。能否安全无引号写入已在上面校验过。
            set_key(DOTENV_PATH, key, value, quote_mode="never")
            updated_keys.append(key)
            # 留空也要写进当前进程：否则会继续用启动时的旧环境变量，清空不生效
            os.environ[key] = value
        except Exception as e:
            errors.append(f"更新 {key} 失败: {e}")
    updated_count = len(updated_keys)

    if updated_count > 0:
        message = f"成功更新 {updated_count} 个系统环境变量。"
        if errors: message += " 部分变量更新失败: " + "; ".join(errors)
        apply_note = build_env_apply_note(updated_keys)
        log_tracker(_fmt('[SYSTEM]', message))
        if apply_note:
            log_tracker(_fmt('[SYSTEM]', apply_note))
        return jsonify({
            "status": "success",
            "message": message,
            "apply_note": apply_note,
            "env_vars": build_system_env(),
            "bark_help": build_bark_help_state(),
        })
    return jsonify({"status": "error", "message": "没有变量被更新或发生错误: " + "; ".join(errors)}), 500

PASSWORD_INPUT_KEYS = ("password", "new_password")


def strip_password_fields(data: dict) -> dict:
    """SSO 下本站不再管理密码：请求里带的密码字段一律丢弃（UI 也已隐藏）。"""
    if not sso_enabled():
        return data
    return {key: value for key, value in data.items() if key not in PASSWORD_INPUT_KEYS}


@app.route('/api/users', methods=['GET'])
@admin_required
def api_list_users():
    return jsonify(build_user_state())


@app.route('/api/users', methods=['POST'])
@admin_required
def api_create_user():
    data = request.get_json() or {}
    portal_sub = None
    if sso_enabled():
        data = strip_password_fields(data)
        # 建号沿用"启用登录必须有密码"的校验，SSO 下给一个没人知道的随机密码
        data["password"] = secrets.token_urlsafe(32)
        portal_sub = str(data.get("portal_sub", "") or "").strip() or None
    try:
        user = create_account(data)
        if portal_sub:
            try:
                user = set_account_portal_sub(user["id"], portal_sub)
            except Exception as exc:
                return jsonify({
                    "status": "error",
                    "message": f"账号已创建，但 portal 映射失败：{exc}",
                    "user": user,
                    "user_state": build_user_state(),
                }), 400
        return jsonify({
            "status": "success",
            "message": "用户已创建。",
            "user": user,
            "user_state": build_user_state(),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/users/<int:user_id>', methods=['GET'])
@admin_required
def api_get_user(user_id: int):
    user = get_account(user_id)
    if not user:
        return jsonify({"status": "error", "message": "用户不存在。"}), 404
    return jsonify({"status": "success", "user": user})


@app.route('/api/users/<int:user_id>', methods=['PUT'])
@admin_required
def api_update_user(user_id: int):
    data = strip_password_fields(request.get_json() or {})
    if sso_enabled():
        # SSO 下授权角色以 X-Role 为准，库里的 role 不参与授权；改它不会生效，干脆不接受
        data.pop("role", None)
    current = current_account()
    if current and int(current["id"]) == int(user_id):
        # 防止管理员在用户管理里把自己锁在门外
        if "login_enabled" in data and not _bool_from_input(data.get("login_enabled"), default=True):
            return jsonify({"status": "error", "message": "不能关闭当前登录账号自己的登录。"}), 400
        if "role" in data and str(data.get("role") or "").strip().lower() != "admin":
            return jsonify({"status": "error", "message": "不能降低当前登录账号自己的权限。"}), 400
    try:
        user = update_account(user_id, data, actor_role="admin")
        return jsonify({
            "status": "success",
            "message": "用户已更新。",
            "user": user,
            "user_state": build_user_state(),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/api/admin/accounts/<int:account_id>/portal-user', methods=['POST'])
@admin_required
def api_set_account_portal_sub(account_id: int):
    """设置/清除账号的 portal 映射：{"portal_sub": "<portal 账号 ID>"}；空串或 null 表示清除。"""
    data = request.get_json(silent=True) or {}
    if "portal_sub" not in data:
        return jsonify({"status": "error", "message": "缺少 portal_sub 字段。"}), 400
    try:
        user = set_account_portal_sub(account_id, data.get("portal_sub"))
    except ValueError as exc:
        status = 404 if str(exc) == "用户不存在。" else 400
        return jsonify({"status": "error", "message": str(exc)}), status
    log_tracker(_fmt('[SSO]', f"账号 {user['username']} id={user['id']} 的 portal 映射设为 {user.get('portal_sub') or '（清除）'}"))
    return jsonify({
        "status": "success",
        "message": "portal 映射已更新。" if user.get("portal_sub") else "portal 映射已清除。",
        "user": user,
        "user_state": build_user_state(),
    })


@app.route('/api/users/<int:user_id>/test_push', methods=['POST'])
@admin_required
def api_user_test_push(user_id: int):
    user = get_account(user_id)
    if not user:
        return jsonify({"status": "error", "message": "用户不存在。"}), 404
    try:
        payload = request.get_json() or {}
        test_account = build_bark_test_account(user, payload)
        title, body = extract_bark_test_message(
            payload,
            default_title="测试推送",
            default_body=f"{user['display_name']} 的 Bark 设置已生效。",
        )
        result = send_bark_test_push(test_account, title=title, body=body)
        if result["ok"]:
            return jsonify({"status": "success", "message": f"测试推送已发送到 {result['devices']} 个设备。"})
        return jsonify({"status": "error", "message": f"推送失败，HTTP {result['status_code']}"}), 400
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


def task_created_message(task: dict) -> str:
    # 同单号在追踪中会被 storage 拒绝；能走到这里要么是新建，要么是从归档里恢复
    if task.get("revived"):
        return "该单号曾归档，已恢复追踪并更新设置。"
    return "追踪任务已添加。"


TASK_INPUT_KEYS = ("tracking_number", "label", "check_interval", "enabled", "archived")


def build_task_payload(data: dict) -> dict:
    """只挑出任务字段：请求里混进来的账号字段不能顺着这个入口改到账号上。"""
    return {key: data[key] for key in TASK_INPUT_KEYS if key in data}


def build_task_success(message: str, task: dict | None = None):
    """成功响应带上刷新后的状态，前端不用再多打一次接口。
    管理员拿全量 user_state，普通用户只拿自己那份。"""
    payload = {"status": "success", "message": message}
    if task is not None:
        payload["task"] = task
    if is_admin():
        payload["user_state"] = build_user_state()
    else:
        payload["user"] = build_account_state()
    return jsonify(payload)


def guard_task_access(task_id: int, account: dict):
    """先自己判一次归属，只为返回准确的 404/403；
    storage 侧的归属校验仍然是最终防线，不能省。"""
    task = get_task(task_id)
    if not task:
        return None, (jsonify({"status": "error", "message": "追踪任务不存在。"}), 404)
    if not is_admin() and int(task["account_id"]) != int(account["id"]):
        return None, (jsonify({"status": "error", "message": "无权操作其他用户的追踪任务。"}), 403)
    return task, None


@app.route('/api/users/<int:user_id>/tasks', methods=['POST'])
@admin_required
def api_create_task(user_id: int):
    data = request.get_json() or {}
    try:
        task = create_task(user_id, build_task_payload(data))
        return build_task_success(task_created_message(task), task)
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/api/tasks/<int:task_id>', methods=['PUT'])
@login_required
def api_update_task(task_id: int):
    account = current_account()
    _, error = guard_task_access(task_id, account)
    if error:
        return error
    data = request.get_json() or {}
    try:
        task = update_task(
            task_id,
            build_task_payload(data),
            actor_role=current_actor_role(),
            actor_id=account["id"],
        )
        return build_task_success("追踪任务已更新。", task)
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/api/tasks/<int:task_id>/archive', methods=['POST'])
@login_required
def api_archive_task(task_id: int):
    account = current_account()
    _, error = guard_task_access(task_id, account)
    if error:
        return error
    try:
        task = archive_task(task_id, actor_role=current_actor_role(), actor_id=account["id"])
        return build_task_success("追踪任务已归档。", task)
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/api/tasks/<int:task_id>', methods=['DELETE'])
@login_required
def api_delete_task(task_id: int):
    account = current_account()
    _, error = guard_task_access(task_id, account)
    if error:
        return error
    try:
        delete_task(task_id, actor_role=current_actor_role(), actor_id=account["id"])
        return build_task_success("追踪任务已删除。")
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


TASK_EVENTS_DEFAULT_LIMIT = 20
# 普通用户门户每个任务直接渲染的最近事件条数，更多的走「加载更多」
PORTAL_EVENT_PREVIEW = 5


def build_portal_task_events(account: dict) -> dict:
    """门户是服务端渲染：每个任务（含归档）先带上最近几条事件。"""
    result = {}
    for task in (account.get("tasks") or []) + (account.get("archived_tasks") or []):
        events, has_more = list_task_events(task["id"], limit=PORTAL_EVENT_PREVIEW)
        result[task["id"]] = {"events": events, "has_more": has_more}
    return result


def _parse_positive_int(value, name: str):
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(str(value).strip())
    except ValueError:
        raise ValueError(f"{name} 必须是正整数。") from None
    if parsed <= 0:
        raise ValueError(f"{name} 必须是正整数。")
    return parsed


@app.route('/api/tasks/<int:task_id>/events', methods=['GET'])
@login_required
def api_task_events(task_id: int):
    """任务时间线，按时间倒序分页：limit 每页条数（上限见 storage.EVENT_PAGE_MAX），
    before 传上一页返回的 next_before。本人或管理员可看。"""
    account = current_account()
    _, error = guard_task_access(task_id, account)
    if error:
        return error
    try:
        limit = _parse_positive_int(request.args.get("limit"), "limit") or TASK_EVENTS_DEFAULT_LIMIT
        before = _parse_positive_int(request.args.get("before"), "before")
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    events, has_more = list_task_events(task_id, limit=limit, before=before)
    return jsonify({
        "status": "success",
        "events": events,
        "has_more": has_more,
        "next_before": events[-1]["id"] if has_more and events else None,
    })


@app.route('/api/me', methods=['GET'])
@login_required
def api_me():
    account = current_account()
    return jsonify({"status": "success", "user": account})


@app.route('/api/me', methods=['PUT'])
@login_required
def api_update_me():
    account = current_account()
    data = strip_password_fields(request.get_json() or {})
    # SSO 下角色以 X-Role 为准；未开启时保持原来用库里 role 的行为
    actor_role = current_actor_role() if sso_enabled() else account["role"]
    try:
        user = update_account(account["id"], data, actor_role=actor_role, actor_id=account["id"])
        return jsonify({
            "status": "success",
            "message": "个人资料已更新。",
            "user": user,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/me/update', methods=['POST'])
@login_required
def me_update_form():
    account = current_account()
    actor_role = current_actor_role() if sso_enabled() else account["role"]
    try:
        update_account(
            account["id"],
            strip_password_fields({
                "display_name": request.form.get("display_name", ""),
                "note": request.form.get("note", ""),
                "bark_keys": request.form.get("bark_keys", ""),
                "bark_query_params": request.form.get("bark_query_params", ""),
                "bark_url_enabled": 'bark_url_enabled' in request.form,
                "new_password": request.form.get("new_password", ""),
            }),
            actor_role=actor_role,
            actor_id=account["id"],
        )
        return redirect(url_for('index', status='success', message='资料已保存。'))
    except Exception as exc:
        return redirect(url_for('index', status='error', message=str(exc)))


def task_form_payload() -> dict:
    """门户页是传统表单：未勾选的复选框不会提交，所以 enabled 按键是否存在判断。"""
    return {
        "tracking_number": request.form.get("tracking_number", ""),
        "label": request.form.get("label", ""),
        "check_interval": request.form.get("check_interval", "300"),
        "enabled": 'enabled' in request.form,
    }


@app.route('/me/tasks', methods=['POST'])
@login_required
def me_create_task_form():
    account = current_account()
    try:
        task = create_task(account["id"], task_form_payload())
        return redirect(url_for('index', status='success', message=task_created_message(task)))
    except Exception as exc:
        return redirect(url_for('index', status='error', message=str(exc)))


@app.route('/me/tasks/<int:task_id>/update', methods=['POST'])
@login_required
def me_update_task_form(task_id: int):
    account = current_account()
    try:
        update_task(
            task_id,
            task_form_payload(),
            actor_role=current_actor_role(),
            actor_id=account["id"],
        )
        return redirect(url_for('index', status='success', message='追踪任务已更新。'))
    except Exception as exc:
        return redirect(url_for('index', status='error', message=str(exc)))


@app.route('/me/tasks/<int:task_id>/archive', methods=['POST'])
@login_required
def me_archive_task_form(task_id: int):
    account = current_account()
    try:
        archive_task(task_id, actor_role=current_actor_role(), actor_id=account["id"])
        return redirect(url_for('index', status='success', message='追踪任务已归档。'))
    except Exception as exc:
        return redirect(url_for('index', status='error', message=str(exc)))


@app.route('/me/tasks/<int:task_id>/delete', methods=['POST'])
@login_required
def me_delete_task_form(task_id: int):
    account = current_account()
    try:
        delete_task(task_id, actor_role=current_actor_role(), actor_id=account["id"])
        return redirect(url_for('index', status='success', message='追踪任务已删除。'))
    except Exception as exc:
        return redirect(url_for('index', status='error', message=str(exc)))


@app.route('/me/test-push', methods=['POST'])
@login_required
def me_test_push():
    account = current_account()
    try:
        test_account = build_bark_test_account(account, request.form)
        title, body = extract_bark_test_message(
            request.form,
            default_title="测试推送",
            default_body="你的 Bark 设置已生效。",
        )
        result = send_bark_test_push(test_account, title=title, body=body)
        if result["ok"]:
            return redirect(url_for('index', status='success', message=f"测试推送已发送到 {result['devices']} 个设备。"))
        return redirect(url_for('index', status='error', message=f"推送失败，HTTP {result['status_code']}"))
    except Exception as exc:
        return redirect(url_for('index', status='error', message=str(exc)))

# --- 远程 Bark 服务状态 ---

@app.route('/remote_bark_status', methods=['GET'])
@admin_required
def remote_bark_status():
    """
    检查 Bark 地址是否可访问。
    返回:
      configured: 是否配置了 Bark 地址
      url: 当前检测地址
      ok: 是否成功访问(HTTP 200)
      status_code: 响应码(如有)
      latency_ms: 请求耗时
      error: 错误信息(如有)
    """
    public_bark_url = get_bark_public_url()
    if not public_bark_url:
        return jsonify({
            "configured": False,
            "url": "",
            "checked_url": "",
            "ok": False,
            "status_code": None,
            "latency_ms": None,
            "error": "未配置 BARK_SERVER_PUBLIC / BARK_SERVER / BARK_SERVER_INTERNAL",
            "fallback_used": False,
            "note": None,
        })
    result = check_bark_endpoint(public_bark_url, label="PUBLIC")
    internal_bark_url = get_bark_internal_url()
    if result["ok"] or result["status_code"] is not None or not internal_bark_url or internal_bark_url == public_bark_url:
        return jsonify(result)

    log_remote_bark(_fmt('[REMOTE_BARK]', f"PUBLIC 自检失败，回退到 INTERNAL {internal_bark_url}"))
    fallback_result = check_bark_endpoint(internal_bark_url, label="INTERNAL")
    fallback_result["url"] = public_bark_url
    fallback_result["fallback_used"] = True
    if fallback_result["ok"]:
        fallback_result["note"] = f"公网地址在本机自检失败，已回退到本地地址 {internal_bark_url}"
        fallback_result["error"] = result["error"]
    else:
        fallback_result["note"] = f"公网地址和本地地址自检都失败了。公网错误: {result['error']}"
        if fallback_result["error"]:
            fallback_result["error"] = f"{result['error']} | INTERNAL: {fallback_result['error']}"
    return jsonify(fallback_result)

def auto_start_configured_services():
    if local_bark_enabled() and _env_enabled("AUTO_START_BARK_SERVER"):
        log_bark(_fmt('[SYSTEM]', "检测到 AUTO_START_BARK_SERVER=1，准备启动 Bark 服务。"))
        start_local_bark_server(start_reason="auto")
    # 默认开启：Web 启动即拉起追踪脚本，消除"填好单号却没人点启动"的隐形门闸；
    # 设 AUTO_START_TRACKER=0 恢复纯手动模式
    if tracker_auto_start_enabled():
        log_tracker(_fmt('[SYSTEM]', "AUTO_START_TRACKER 已启用，Web 启动后自动拉起追踪脚本，意外退出会自动重启。"))
        start_tracker_script(start_reason="auto")

if __name__ == '__main__':
    auto_start_configured_services()
    try:
        socketio.run(app, host='0.0.0.0', port=get_app_port(), debug=get_debug_enabled(), use_reloader=False)
    except KeyboardInterrupt:
        pass
