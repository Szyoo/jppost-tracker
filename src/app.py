import os
import subprocess
import threading
import sys
import io
import time

import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv, set_key, dotenv_values
import requests

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
    socketio.emit(event, {'data': line})

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'),
            static_folder=os.path.join(os.path.dirname(__file__), 'static'))
app.config['SECRET_KEY'] = 'your_secret_key_here' # 生产环境中请使用更安全的密钥
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*") # 允许所有CORS源，生产环境请限制

# --- Bark 服务相关变量 ---
BARK_EXECUTABLE = os.path.join(BASE_DIR, 'bark-server_linux_amd64')
BARK_DATA_DIR = os.path.join(BASE_DIR, 'bark-data')
TRACKER_SCRIPT = os.path.join(os.path.dirname(__file__), 'tracker.py')
bark_server_process = None
bark_server_thread = None

# --- 脚本运行状态变量 ---
script_process = None
script_thread = None

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

# --- 环境变量文件路径 ---
DOTENV_PATH = os.path.join(BASE_DIR, '.env')
load_dotenv(DOTENV_PATH)


@app.route('/')
def index():
    """渲染主页面，并加载当前的环境变量和分离的历史日志。"""
    env_vars = dotenv_values(DOTENV_PATH)
    default_keys = [
        "TRACKING_NUMBER",
        "CHECK_INTERVAL",
        "BARK_SERVER",
        "BARK_KEY",
        "BARK_HEALTH_PATH",
        "BARK_QUERY_PARAMS",
        "BARK_URL_ENABLED",
        "PUBLIC_URL",
    ]
    # 以 .env 的非空值为准；若 .env 未包含或为空，则回退到进程环境变量（Render Dashboard 设置）
    display_env = {}
    for k in default_keys:
        val = env_vars.get(k)
        if val is not None and str(val).strip() != "":
            display_env[k] = val
        else:
            display_env[k] = os.getenv(k, "")
    initial_tracker_log = tracker_log_buffer.getvalue()
    initial_bark_log = bark_log_buffer.getvalue()
    return render_template('index.html', env_vars=display_env, initial_tracker_log=initial_tracker_log, initial_bark_log=initial_bark_log)

@socketio.on('connect')
def test_connect():
    """客户端连接时发送当前所有服务的状态。"""
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
            _append_log(tracker_log_buffer, TRACKER_LOG_FILE, 'tracker_log', _fmt('[TRACKER]', line.rstrip()))
    except Exception as e:
        _append_log(tracker_log_buffer, TRACKER_LOG_FILE, 'tracker_log', _fmt('[TRACKER]', f"ERROR: 读取脚本输出时发生错误: {e}"))
    finally:
        # 停止保活线程
        stop_keepalive()
        if script_process and script_process.stdout:
            script_process.stdout.close()
        
        return_code = script_process.wait() if script_process else 'N/A'
        _append_log(tracker_log_buffer, TRACKER_LOG_FILE, 'tracker_log', _fmt('[TRACKER]', f"脚本已停止，返回码: {return_code}"))
        socketio.emit('script_status', {'running': False})
        script_process = None

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
        url = f"{public_url}/remote_bark_status"
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
        resp = requests.get(f"{public_url}/remote_bark_status", timeout=5)
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
    _append_log(tracker_log_buffer, TRACKER_LOG_FILE, 'tracker_log', _fmt('[SYSTEM]', f"Render 保活已启用，每 {interval}s ping {public_url}"))

def stop_keepalive():
    global keepalive_thread, keepalive_state
    if keepalive_thread and keepalive_thread.is_alive():
        keepalive_stop_event.set()
        keepalive_thread = None
        public_url = get_public_url()
        keepalive_state = "idle" if public_url else "disabled"
        emit_keepalive_status(False, bool(public_url), public_url)

@socketio.on('start_script')
def start_script():
    """启动 Python 追踪脚本。"""
    global script_process, script_thread
    if script_process is not None and script_process.poll() is None:
        emit('tracker_log', {'data': _fmt('[TRACKER]', "脚本已经在运行中。")})
        emit('script_status', {'running': True})
        return

    emit('tracker_log', {'data': _fmt('[SYSTEM]', "正在启动追踪脚本...")})
    try:
        script_process = subprocess.Popen(
            [sys.executable, '-u', TRACKER_SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        emit('tracker_log', {'data': _fmt('[TRACKER]', "脚本已启动。")})
        emit('script_status', {'running': True})
        start_keepalive()
        
        script_thread = threading.Thread(target=read_script_output, daemon=True)
        script_thread.start()
    except Exception as e:
        emit('tracker_log', {'data': _fmt('[TRACKER]', f"启动脚本失败: {e}")})
        emit('script_status', {'running': False})

@socketio.on('stop_script')
def stop_script():
    """终止 Python 追踪脚本。"""
    global script_process
    if script_process is not None and script_process.poll() is None:
        emit('tracker_log', {'data': _fmt('[SYSTEM]', "终止追踪脚本信号已发送。")})
        script_process.terminate()
        stop_keepalive()
    else:
        emit('tracker_log', {'data': _fmt('[TRACKER]', "脚本未运行。")})
        emit('script_status', {'running': False})

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
            _append_log(bark_log_buffer, BARK_LOG_FILE, 'bark_log', _fmt('[BARK]', line.rstrip()))
    except Exception as e:
        _append_log(bark_log_buffer, BARK_LOG_FILE, 'bark_log', _fmt('[BARK]', f"ERROR: 读取服务输出时发生错误: {e}"))
    finally:
        if bark_server_process and bark_server_process.stdout:
            bark_server_process.stdout.close()
            
        return_code = bark_server_process.wait() if bark_server_process else 'N/A'
        _append_log(bark_log_buffer, BARK_LOG_FILE, 'bark_log', _fmt('[BARK]', f"服务已停止，返回码: {return_code}"))
        socketio.emit('bark_server_status', {'running': False})
        bark_server_process = None

@socketio.on('start_bark_server')
def start_bark_server():
    """启动 Bark 服务。"""
    global bark_server_process, bark_server_thread
    if bark_server_process is not None and bark_server_process.poll() is None:
        emit('bark_log', {'data': _fmt('[BARK]', "服务已经在运行中。")})
        emit('bark_server_status', {'running': True})
        return
    
    emit('bark_log', {'data': _fmt('[SYSTEM]', "正在启动 Bark 服务...")})
    try:
        os.makedirs(BARK_DATA_DIR, exist_ok=True)
        bark_server_process = subprocess.Popen(
            [BARK_EXECUTABLE, '-addr', '0.0.0.0:8080', '-data', BARK_DATA_DIR],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        emit('bark_log', {'data': _fmt('[BARK]', "服务已启动。")})
        emit('bark_server_status', {'running': True})
        
        bark_server_thread = threading.Thread(target=read_bark_output, daemon=True)
        bark_server_thread.start()
    except FileNotFoundError:
        emit('bark_log', {'data': _fmt('[BARK]', f"启动失败: 未找到可执行文件 '{BARK_EXECUTABLE}'。")})
        emit('bark_server_status', {'running': False})
    except Exception as e:
        emit('bark_log', {'data': _fmt('[BARK]', f"启动服务失败: {e}")})
        emit('bark_server_status', {'running': False})

@socketio.on('stop_bark_server')
def stop_bark_server():
    """终止 Bark 服务。"""
    global bark_server_process
    if bark_server_process is not None and bark_server_process.poll() is None:
        emit('bark_log', {'data': _fmt('[SYSTEM]', "终止 Bark 服务信号已发送。")})
        bark_server_process.terminate()
    else:
        emit('bark_log', {'data': _fmt('[BARK]', "服务未运行。")})
        emit('bark_server_status', {'running': False})

# --- 环境变量更新 ---

@app.route('/update_env', methods=['POST'])
def update_env():
    """更新 .env 文件中的变量。"""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "无效的请求数据"}), 400

    if len(data) == 0:
        return jsonify({"status": "success", "message": "没有检测到需要更新的变量。"})

    if not os.path.exists(DOTENV_PATH):
        open(DOTENV_PATH, 'a').close()

    updated_count = 0; errors = []
    for key, value in data.items():
        try:
            set_key(DOTENV_PATH, key, value)
            updated_count += 1
        except Exception as e:
            errors.append(f"更新 {key} 失败: {e}")

    # 将本次更新的非空值同步到当前进程环境变量；
    # 空值不覆盖已有进程环境变量，避免与 Render/本地 shell 配置冲突。
    for key, value in data.items():
        if isinstance(value, str) and value.strip() == "":
            continue
        os.environ[key] = str(value)

    # 若更新了保活相关参数，刷新前端状态显示
    if "PUBLIC_URL" in data or "KEEPALIVE_INTERVAL" in data:
        public_url = get_public_url()
        emit_keepalive_status(keepalive_is_running(), bool(public_url), public_url)

    if updated_count > 0:
        message = f"成功更新 {updated_count} 个环境变量。"
        if errors: message += " 部分变量更新失败: " + "; ".join(errors)
        socketio.emit('tracker_log', {'data': _fmt('[SYSTEM]', message) + _fmt('[SYSTEM]', "请注意：更新的环境变量将在脚本下次启动时生效。")})
        return jsonify({"status": "success", "message": message})
    else:
        return jsonify({"status": "error", "message": "没有变量被更新或发生错误: " + "; ".join(errors)}), 500

# --- 远程 Bark 服务状态 ---

@app.route('/remote_bark_status', methods=['GET'])
def remote_bark_status():
    """
    检查远程 Bark Server 是否可访问。
    返回:
      configured: 是否配置了 BARK_SERVER
      url: 当前 BARK_SERVER
      ok: 是否成功访问(HTTP 200)
      status_code: 响应码(如有)
      latency_ms: 请求耗时
      error: 错误信息(如有)
    """
    bark_url = os.getenv("BARK_SERVER", "").strip()
    if not bark_url:
        return jsonify({
            "configured": False,
            "url": "",
            "ok": False,
            "status_code": None,
            "latency_ms": None,
            "error": "未配置 BARK_SERVER"
        })

    # 去掉末尾 /，避免双斜杠，并按配置健康路径检测
    bark_url = bark_url.rstrip("/")
    health_path = os.getenv("BARK_HEALTH_PATH", "/").strip() or "/"
    if not health_path.startswith("/"):
        health_path = "/" + health_path
    health_url = f"{bark_url}{health_path}"
    log_remote_bark(_fmt('[REMOTE_BARK]', f"CHECK {health_url}"))
    start = time.time()
    try:
        resp = requests.get(health_url, timeout=5)
        latency_ms = int((time.time() - start) * 1000)
        log_remote_bark(_fmt('[REMOTE_BARK]', f"OK HTTP {resp.status_code} {latency_ms}ms"))
        return jsonify({
            "configured": True,
            "url": bark_url,
            "ok": resp.status_code == 200,
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
            "error": None if resp.status_code == 200 else f"HTTP {resp.status_code}"
        })
    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        log_remote_bark(_fmt('[REMOTE_BARK]', f"ERROR {latency_ms}ms {e}"))
        return jsonify({
            "configured": True,
            "url": bark_url,
            "ok": False,
            "status_code": None,
            "latency_ms": latency_ms,
            "error": str(e)
        })

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=6060, debug=True, use_reloader=False)
