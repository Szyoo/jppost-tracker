import io
import os
import sys
import time
import secrets
import urllib.parse

import shutil
import subprocess
import threading

from datetime import timedelta
from functools import wraps

from flask import Flask, g, render_template, request, jsonify, redirect, session, url_for
from flask_socketio import SocketIO, emit, disconnect
from dotenv import load_dotenv, set_key
import requests

from storage import (
    account_to_profile_env,
    archive_task,
    build_tracking_url,
    create_account,
    create_task,
    delete_task,
    ensure_storage,
    get_account,
    get_account_by_username,
    get_task,
    list_accounts,
    load_system_env,
    parse_bark_keys,
    register_user,
    update_account,
    update_task,
    verify_account_password,
)

# --- 时区固定为日本 ---
# Render/容器里即使设置了 TZ，有时也不会自动生效；这里强制设置并调用 tzset。
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

SYSTEM_ENV_KEYS = [
    "BARK_SERVER_INTERNAL",
    "BARK_SERVER_PUBLIC",
    "BARK_SERVER",
    "BARK_HEALTH_PATH",
    "BARK_HEALTH_TIMEOUT",
    "BARK_BIND_ADDRESS",
    "PUBLIC_URL",
    "APP_PORT",
    "AUTO_START_BARK_SERVER",
    "AUTO_START_TRACKER",
    "LOCAL_BARK_ENABLED",
]

def _first_non_empty(*values: str) -> str:
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return ""

def _env_enabled(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}

def get_bark_internal_url() -> str:
    return _first_non_empty(
        os.getenv("BARK_SERVER_INTERNAL"),
        os.getenv("BARK_SERVER"),
        os.getenv("BARK_SERVER_PUBLIC"),
    ).rstrip("/")

def get_bark_public_url() -> str:
    return _first_non_empty(
        os.getenv("BARK_SERVER_PUBLIC"),
        os.getenv("BARK_SERVER"),
        os.getenv("BARK_SERVER_INTERNAL"),
    ).rstrip("/")

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

def current_account():
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
    # 角色以数据库为准；session 里缓存的角色在管理员被降级后不会失效，不可信
    account = current_account()
    return bool(account and account.get("role") == "admin" and account.get("login_enabled"))

def current_actor_role() -> str:
    """传给 storage 做归属校验的角色。走 is_admin() 而不是直接读 role 字段，
    这样管理员被停用登录后立刻失去越权改他人任务的能力。"""
    return "admin" if is_admin() else "user"

def is_authenticated() -> bool:
    account = current_account()
    return bool(account and account.get("login_enabled"))

def _ts() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')

def _ensure_nl(s: str) -> str:
    return s if s.endswith("\n") else s + "\n"

def _fmt(tag: str, message: str) -> str:
    return f"{_ts()} {tag} {message}".rstrip() + "\n"

def _append_log(buffer: io.StringIO, filepath: str, event: str, line: str):
    buffer.write(line)
    with open(filepath, 'a') as f:
        f.write(line)
    try:
        socketio.emit(event, {'data': line})
    except Exception:
        pass

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

# --- Render free 保活 ---
keepalive_thread = None
keepalive_stop_event = threading.Event()
keepalive_last_code = None
keepalive_last_error = None
keepalive_last_at = None
keepalive_state = "idle"  # idle | pinging | waiting | ok | error | disabled

def get_public_url() -> str:
    return os.getenv("PUBLIC_URL", "").strip().rstrip("/")

def get_keepalive_interval() -> int:
    try:
        return int(os.getenv("KEEPALIVE_INTERVAL", "600"))
    except Exception:
        return 600

def keepalive_is_running() -> bool:
    return keepalive_thread is not None and keepalive_thread.is_alive()

def emit_keepalive_status(running: bool, configured: bool, url: str):
    socketio.emit('keepalive_status', {
        'running': running,
        'configured': configured,
        'url': url,
        'state': keepalive_state,
        'last_code': keepalive_last_code,
        'last_error': keepalive_last_error,
        'last_at': keepalive_last_at
    })

# --- 分离的日志缓存及文件 ---
tracker_log_buffer = io.StringIO()
bark_log_buffer = io.StringIO()
remote_bark_log_buffer = io.StringIO()
TRACKER_LOG_FILE = os.path.join(LOG_DIR, 'tracker.log')
BARK_LOG_FILE = os.path.join(LOG_DIR, 'bark.log')
REMOTE_BARK_LOG_FILE = os.path.join(LOG_DIR, 'remote_bark.log')
login_attempts = {}
login_attempts_lock = threading.Lock()

if os.path.exists(TRACKER_LOG_FILE):
    with open(TRACKER_LOG_FILE, 'r') as f:
        tracker_log_buffer.write(f.read())
if os.path.exists(BARK_LOG_FILE):
    with open(BARK_LOG_FILE, 'r') as f:
        bark_log_buffer.write(f.read())
if os.path.exists(REMOTE_BARK_LOG_FILE):
    with open(REMOTE_BARK_LOG_FILE, 'r') as f:
        remote_bark_log_buffer.write(f.read())

def log_remote_bark(line: str):
    """写入远程 Bark 健康检测日志，并推送到前端。"""
    _append_log(remote_bark_log_buffer, REMOTE_BARK_LOG_FILE, 'remote_bark_log', _ensure_nl(line))

def log_tracker(line: str):
    _append_log(tracker_log_buffer, TRACKER_LOG_FILE, 'tracker_log', _ensure_nl(line))

def log_bark(line: str):
    _append_log(bark_log_buffer, BARK_LOG_FILE, 'bark_log', _ensure_nl(line))

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

def unauthorized_response():
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
    if request.endpoint in {'login', 'register', 'logout', 'healthz', 'static'}:
        return None
    if request.path.startswith('/socket.io'):
        return None
    if not is_authenticated():
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

@app.route('/healthz', methods=['GET'])
def healthz():
    return jsonify({"status": "ok"})

@app.route('/login', methods=['GET', 'POST'])
def login():
    next_target = build_next_target()
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
    return redirect(url_for('login'))

def build_viewer_state():
    account = current_account()
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

def _parse_bark_query_params(query_string: str) -> dict:
    query_string = str(query_string or "").strip()
    if not query_string:
        return {}
    result = {}
    for part in query_string.lstrip("?").split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        result[urllib.parse.unquote(key)] = urllib.parse.unquote(value)
    return result

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
        bark_keys = data.get("bark_keys") or data.get("bark_key", "")

    return {
        "display_name": str(data.get("display_name", source.get("display_name", "")) or "").strip() or source.get("display_name") or "当前用户",
        "bark_keys": bark_keys or source.get("bark_keys") or source.get("bark_key", ""),
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
    bark_keys = parse_bark_keys(account.get("bark_keys") or account.get("bark_key"))
    if not bark_server:
        raise ValueError("未配置 Bark 服务地址。")
    if not bark_keys:
        raise ValueError("当前用户还没有配置 Bark Keys。")

    params = _parse_bark_query_params(account.get("bark_query_params", ""))
    tracking_url = str(account.get("tracking_url", "") or "").strip()
    if account.get("bark_url_enabled") and tracking_url:
        params["url"] = tracking_url

    timeout = get_bark_health_timeout()
    if len(bark_keys) == 1:
        title_enc = urllib.parse.quote(title, safe="")
        body_enc = urllib.parse.quote(body, safe="")
        query = urllib.parse.urlencode(params, doseq=True)
        suffix = f"?{query}" if query else ""
        resp = requests.get(f"{bark_server}/{bark_keys[0]}/{title_enc}/{body_enc}{suffix}", timeout=timeout)
    else:
        payload = {"title": title, "body": body, "device_keys": bark_keys, **params}
        resp = requests.post(f"{bark_server}/push", json=payload, timeout=timeout)

    return {
        "ok": 200 <= resp.status_code < 300,
        "status_code": resp.status_code,
        "devices": len(bark_keys),
        "response_text": resp.text[:500],
    }

@app.route('/')
@login_required
def index():
    viewer_state = build_viewer_state()
    account = viewer_state["account"]
    bark_help = build_bark_help_state()
    if viewer_state["is_admin"]:
        system_env = build_system_env()
        user_state = build_user_state()
        initial_tracker_log = tracker_log_buffer.getvalue()
        initial_bark_log = bark_log_buffer.getvalue()
        return render_template(
            'index.html',
            viewer_state=viewer_state,
            env_vars=system_env,
            user_state=user_state,
            bark_help=bark_help,
            initial_tracker_log=initial_tracker_log,
            initial_bark_log=initial_bark_log,
        )

    profile_env = account_to_profile_env(account)
    return render_template(
        'user_portal.html',
        viewer_state=viewer_state,
        account=account,
        profile_env=profile_env,
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
    public_url = get_public_url()
    emit('keepalive_status', {
        'running': keepalive_is_running(),
        'configured': bool(public_url),
        'url': public_url,
        'state': keepalive_state,
        'last_code': keepalive_last_code,
        'last_error': keepalive_last_error,
        'last_at': keepalive_last_at
    })
    emit('bark_server_status', {'running': bark_server_process is not None and bark_server_process.poll() is None})
    emit('full_tracker_log', {'data': tracker_log_buffer.getvalue()})
    emit('full_bark_log', {'data': bark_log_buffer.getvalue()})
    emit('full_remote_bark_log', {'data': remote_bark_log_buffer.getvalue()})

# --- 追踪脚本控制 ---

def read_script_output():
    """从追踪脚本进程的管道中实时读取输出，并发送到独立的事件。"""
    global script_process
    if script_process is None: return
    stream = script_process.stdout
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ''):
            log_tracker(_fmt('[TRACKER]', line.rstrip()))
    except Exception as e:
        log_tracker(_fmt('[TRACKER]', f"ERROR: 读取脚本输出时发生错误: {e}"))
    finally:
        # 停止保活线程
        stop_keepalive()
        if script_process and script_process.stdout:
            script_process.stdout.close()

        return_code = script_process.wait() if script_process else 'N/A'
        log_tracker(_fmt('[TRACKER]', f"脚本已停止，返回码: {return_code}"))
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
        start_keepalive()

        script_thread = threading.Thread(target=read_script_output, daemon=True)
        script_thread.start()
        return True
    except Exception as e:
        log_tracker(_fmt('[TRACKER]', f"启动脚本失败: {e}"))
        socketio.emit('script_status', {'running': False})
        return False

def keepalive_loop():
    """追踪脚本运行期间定时自 ping，避免 Render free 休眠。"""
    global keepalive_last_code, keepalive_last_error, keepalive_last_at, keepalive_state
    while True:
        interval = get_keepalive_interval()
        public_url = get_public_url()
        if not public_url:
            keepalive_state = "disabled"
            emit_keepalive_status(True, False, "")
            break

        # 等待到下一次 ping
        keepalive_state = "waiting"
        emit_keepalive_status(True, True, public_url)
        if keepalive_stop_event.wait(interval):
            break

        # 开始 ping
        keepalive_state = "pinging"
        emit_keepalive_status(True, True, public_url)
        url = f"{public_url}/healthz"
        try:
            resp = requests.get(url, timeout=5)
            keepalive_last_code = resp.status_code
            keepalive_last_error = None if resp.ok else f"HTTP {resp.status_code}"
            keepalive_state = "ok" if resp.ok else "error"
        except Exception as e:
            keepalive_last_code = None
            keepalive_last_error = str(e)
            keepalive_state = "error"
        keepalive_last_at = time.strftime('%Y-%m-%d %H:%M:%S')
        emit_keepalive_status(True, True, public_url)

def start_keepalive():
    global keepalive_thread, keepalive_last_code, keepalive_last_error, keepalive_last_at, keepalive_state
    public_url = get_public_url()
    interval = get_keepalive_interval()
    if not public_url or interval <= 0:
        keepalive_state = "disabled"
        return
    if keepalive_thread and keepalive_thread.is_alive():
        return
    keepalive_stop_event.clear()
    keepalive_state = "pinging"
    emit_keepalive_status(True, True, public_url)
    # 启动时立即 ping 一次
    try:
        resp = requests.get(f"{public_url}/healthz", timeout=5)
        keepalive_last_code = resp.status_code
        keepalive_last_error = None if resp.ok else f"HTTP {resp.status_code}"
        keepalive_state = "ok" if resp.ok else "error"
    except Exception as e:
        keepalive_last_code = None
        keepalive_last_error = str(e)
        keepalive_state = "error"
    keepalive_last_at = time.strftime('%Y-%m-%d %H:%M:%S')
    emit_keepalive_status(True, True, public_url)

    keepalive_thread = threading.Thread(target=keepalive_loop, daemon=True)
    keepalive_thread.start()
    log_tracker(_fmt('[SYSTEM]', f"Render 保活已启用，每 {interval}s ping {public_url}"))

def stop_keepalive():
    global keepalive_thread, keepalive_state
    if keepalive_thread and keepalive_thread.is_alive():
        keepalive_stop_event.set()
        keepalive_thread = None
        public_url = get_public_url()
        keepalive_state = "idle" if public_url else "disabled"
        emit_keepalive_status(False, bool(public_url), public_url)

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
        stop_keepalive()
    else:
        log_tracker(_fmt('[TRACKER]', "脚本未运行。"))
        socketio.emit('script_status', {'running': False})

# --- Bark 服务控制 ---

def read_bark_output():
    """从 Bark 服务进程的管道中实时读取输出，并发送到独立的事件。"""
    global bark_server_process
    if bark_server_process is None: return
    stream = bark_server_process.stdout
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ''):
            log_bark(_fmt('[BARK]', line.rstrip()))
    except Exception as e:
        log_bark(_fmt('[BARK]', f"ERROR: 读取服务输出时发生错误: {e}"))
    finally:
        if bark_server_process and bark_server_process.stdout:
            bark_server_process.stdout.close()
            
        return_code = bark_server_process.wait() if bark_server_process else 'N/A'
        log_bark(_fmt('[BARK]', f"服务已停止，返回码: {return_code}"))
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
        
        bark_server_thread = threading.Thread(target=read_bark_output, daemon=True)
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

    system_changes = {key: value for key, value in data.items() if key in SYSTEM_ENV_KEYS}
    if not system_changes:
        return jsonify({"status": "error", "message": "没有可更新的系统配置。"}), 400

    updated_count = 0
    errors = []
    for key, value in system_changes.items():
        try:
            set_key(DOTENV_PATH, key, value)
            updated_count += 1
        except Exception as e:
            errors.append(f"更新 {key} 失败: {e}")

    for key, value in system_changes.items():
        if isinstance(value, str) and value.strip() == "":
            continue
        os.environ[key] = str(value)

    # 若更新了保活相关参数，刷新前端状态显示
    if "PUBLIC_URL" in system_changes or "KEEPALIVE_INTERVAL" in system_changes:
        public_url = get_public_url()
        emit_keepalive_status(keepalive_is_running(), bool(public_url), public_url)

    if updated_count > 0:
        message = f"成功更新 {updated_count} 个系统环境变量。"
        if errors: message += " 部分变量更新失败: " + "; ".join(errors)
        log_tracker(_fmt('[SYSTEM]', message))
        log_tracker(_fmt('[SYSTEM]', "生效说明：Bark 地址/健康检查类配置对追踪脚本即时生效；APP_PORT、AUTO_START_*、LOCAL_BARK_ENABLED 等启动参数需重启 Web 服务。"))
        return jsonify({"status": "success", "message": message, "env_vars": build_system_env(), "bark_help": build_bark_help_state()})
    return jsonify({"status": "error", "message": "没有变量被更新或发生错误: " + "; ".join(errors)}), 500

@app.route('/api/users', methods=['GET'])
@admin_required
def api_list_users():
    return jsonify(build_user_state())


@app.route('/api/users', methods=['POST'])
@admin_required
def api_create_user():
    data = request.get_json() or {}
    try:
        user = create_account(data)
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
    data = request.get_json() or {}
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
        return build_task_success("追踪任务已添加。", task)
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


@app.route('/api/me', methods=['GET'])
@login_required
def api_me():
    account = current_account()
    return jsonify({"status": "success", "user": account})


@app.route('/api/me', methods=['PUT'])
@login_required
def api_update_me():
    account = current_account()
    data = request.get_json() or {}
    try:
        user = update_account(account["id"], data, actor_role=account["role"], actor_id=account["id"])
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
    try:
        update_account(
            account["id"],
            {
                "display_name": request.form.get("display_name", ""),
                "note": request.form.get("note", ""),
                "bark_keys": request.form.get("bark_keys", ""),
                "bark_query_params": request.form.get("bark_query_params", ""),
                "bark_url_enabled": 'bark_url_enabled' in request.form,
                "new_password": request.form.get("new_password", ""),
            },
            actor_role=account["role"],
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
        create_task(account["id"], task_form_payload())
        return redirect(url_for('index', status='success', message='追踪任务已添加。'))
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
