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

# 初始化 Flask 应用
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

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
    remote_bark_log_buffer.write(line)
    with open(REMOTE_BARK_LOG_FILE, 'a') as f:
        f.write(line)
    socketio.emit('remote_bark_log', {'data': line})

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
    ]
    display_env = {k: env_vars.get(k, "") for k in default_keys}
    initial_tracker_log = tracker_log_buffer.getvalue()
    initial_bark_log = bark_log_buffer.getvalue()
    return render_template('index.html', env_vars=display_env, initial_tracker_log=initial_tracker_log, initial_bark_log=initial_bark_log)

@socketio.on('connect')
def test_connect():
    """客户端连接时发送当前所有服务的状态。"""
    print('Client connected', flush=True)
    emit('script_status', {'running': script_process is not None and script_process.poll() is None})
    emit('bark_server_status', {'running': bark_server_process is not None and bark_server_process.poll() is None})
    emit('full_tracker_log', {'data': tracker_log_buffer.getvalue()})
    emit('full_bark_log', {'data': bark_log_buffer.getvalue()})
    emit('full_remote_bark_log', {'data': remote_bark_log_buffer.getvalue()})

# --- 追踪脚本控制 ---

def read_script_output():
    """从追踪脚本进程的管道中实时读取输出，并发送到独立的事件。"""
    global script_process
    if script_process is None: return
    try:
        for line in iter(script_process.stdout.readline, ''):
            log_line = f"[TRACKER] {line}"
            tracker_log_buffer.write(log_line)
            with open(TRACKER_LOG_FILE, 'a') as f:
                f.write(log_line)
            socketio.emit('tracker_log', {'data': log_line})
    except Exception as e:
        error_line = f"[TRACKER] ERROR: 读取脚本输出时发生错误: {e}\n"
        tracker_log_buffer.write(error_line)
        with open(TRACKER_LOG_FILE, 'a') as f:
            f.write(error_line)
        socketio.emit('tracker_log', {'data': error_line})
    finally:
        if script_process and script_process.stdout:
            script_process.stdout.close()
        
        return_code = script_process.wait() if script_process else 'N/A'
        final_message = f"[TRACKER] 脚本已停止，返回码: {return_code}\n"
        tracker_log_buffer.write(final_message)
        with open(TRACKER_LOG_FILE, 'a') as f:
            f.write(final_message)
        socketio.emit('tracker_log', {'data': final_message})
        socketio.emit('script_status', {'running': False})
        script_process = None

@socketio.on('start_script')
def start_script():
    """启动 Python 追踪脚本。"""
    global script_process, script_thread
    if script_process is not None and script_process.poll() is None:
        emit('tracker_log', {'data': "[TRACKER] 脚本已经在运行中。\n"})
        emit('script_status', {'running': True})
        return

    emit('tracker_log', {'data': "[SYSTEM] 正在启动追踪脚本...\n"})
    try:
        script_process = subprocess.Popen(
            [sys.executable, '-u', TRACKER_SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        emit('tracker_log', {'data': "[TRACKER] 脚本已启动。\n"})
        emit('script_status', {'running': True})
        
        script_thread = threading.Thread(target=read_script_output, daemon=True)
        script_thread.start()
    except Exception as e:
        emit('tracker_log', {'data': f"[TRACKER] 启动脚本失败: {e}\n"})
        emit('script_status', {'running': False})

@socketio.on('stop_script')
def stop_script():
    """终止 Python 追踪脚本。"""
    global script_process
    if script_process is not None and script_process.poll() is None:
        emit('tracker_log', {'data': "[SYSTEM] 终止追踪脚本信号已发送。\n"})
        script_process.terminate()
    else:
        emit('tracker_log', {'data': "[TRACKER] 脚本未运行。\n"})
        emit('script_status', {'running': False})

# --- Bark 服务控制 ---

def read_bark_output():
    """从 Bark 服务进程的管道中实时读取输出，并发送到独立的事件。"""
    global bark_server_process
    if bark_server_process is None: return
    try:
        for line in iter(bark_server_process.stdout.readline, ''):
            log_line = f"[BARK] {line}"
            bark_log_buffer.write(log_line)
            with open(BARK_LOG_FILE, 'a') as f:
                f.write(log_line)
            socketio.emit('bark_log', {'data': log_line})
    except Exception as e:
        error_line = f"[BARK] ERROR: 读取服务输出时发生错误: {e}\n"
        bark_log_buffer.write(error_line)
        with open(BARK_LOG_FILE, 'a') as f:
            f.write(error_line)
        socketio.emit('bark_log', {'data': error_line})
    finally:
        if bark_server_process and bark_server_process.stdout:
            bark_server_process.stdout.close()
            
        return_code = bark_server_process.wait() if bark_server_process else 'N/A'
        final_message = f"[BARK] 服务已停止，返回码: {return_code}\n"
        bark_log_buffer.write(final_message)
        with open(BARK_LOG_FILE, 'a') as f:
            f.write(final_message)
        socketio.emit('bark_log', {'data': final_message})
        socketio.emit('bark_server_status', {'running': False})
        bark_server_process = None

@socketio.on('start_bark_server')
def start_bark_server():
    """启动 Bark 服务。"""
    global bark_server_process, bark_server_thread
    if bark_server_process is not None and bark_server_process.poll() is None:
        emit('bark_log', {'data': "[BARK] 服务已经在运行中。\n"})
        emit('bark_server_status', {'running': True})
        return
    
    emit('bark_log', {'data': "[SYSTEM] 正在启动 Bark 服务...\n"})
    try:
        os.makedirs(BARK_DATA_DIR, exist_ok=True)
        bark_server_process = subprocess.Popen(
            [BARK_EXECUTABLE, '-addr', '0.0.0.0:8080', '-data', BARK_DATA_DIR],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        emit('bark_log', {'data': "[BARK] 服务已启动。\n"})
        emit('bark_server_status', {'running': True})
        
        bark_server_thread = threading.Thread(target=read_bark_output, daemon=True)
        bark_server_thread.start()
    except FileNotFoundError:
        msg = f"[BARK] 启动失败: 未找到可执行文件 '{BARK_EXECUTABLE}'。\n"
        emit('bark_log', {'data': msg})
        emit('bark_server_status', {'running': False})
    except Exception as e:
        emit('bark_log', {'data': f"[BARK] 启动服务失败: {e}\n"})
        emit('bark_server_status', {'running': False})

@socketio.on('stop_bark_server')
def stop_bark_server():
    """终止 Bark 服务。"""
    global bark_server_process
    if bark_server_process is not None and bark_server_process.poll() is None:
        emit('bark_log', {'data': "[SYSTEM] 终止 Bark 服务信号已发送。\n"})
        bark_server_process.terminate()
    else:
        emit('bark_log', {'data': "[BARK] 服务未运行。\n"})
        emit('bark_server_status', {'running': False})

# --- 环境变量更新 ---

@app.route('/update_env', methods=['POST'])
def update_env():
    """更新 .env 文件中的变量。"""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "无效的请求数据"}), 400

    if not os.path.exists(DOTENV_PATH):
        open(DOTENV_PATH, 'a').close()

    updated_count = 0; errors = []
    for key, value in data.items():
        try:
            set_key(DOTENV_PATH, key, value)
            updated_count += 1
        except Exception as e:
            errors.append(f"更新 {key} 失败: {e}")

    load_dotenv(DOTENV_PATH, override=True)

    if updated_count > 0:
        message = f"成功更新 {updated_count} 个环境变量。"
        if errors: message += " 部分变量更新失败: " + "; ".join(errors)
        socketio.emit('tracker_log', {'data': f"[SYSTEM] {message}\n[SYSTEM] 请注意：更新的环境变量将在脚本下次启动时生效。\n"})
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
    log_remote_bark(f"[REMOTE_BARK] {time.strftime('%Y-%m-%d %H:%M:%S')} CHECK {health_url}\n")
    start = time.time()
    try:
        resp = requests.get(health_url, timeout=5)
        latency_ms = int((time.time() - start) * 1000)
        log_remote_bark(f"[REMOTE_BARK] {time.strftime('%Y-%m-%d %H:%M:%S')} OK HTTP {resp.status_code} {latency_ms}ms\n")
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
        log_remote_bark(f"[REMOTE_BARK] {time.strftime('%Y-%m-%d %H:%M:%S')} ERROR {latency_ms}ms {e}\n")
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
