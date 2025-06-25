import os
import subprocess
import threading
import sys
import io
import time

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv, set_key, dotenv_values

# 初始化 Flask 应用
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key_here' # 生产环境中请使用更安全的密钥
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*") # 允许所有CORS源，生产环境请限制

# --- Bark 服务相关变量 ---
# 请确保您下载的可执行文件名与此匹配，并放在项目根目录
BARK_EXECUTABLE = './bark-server_linux_amd64' 
bark_server_process = None
bark_server_thread = None

# --- 脚本运行状态变量 ---
script_process = None
script_thread = None
log_buffer = io.StringIO() # 用于捕获所有输出

# --- 环境变量文件路径 ---
DOTENV_PATH = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(DOTENV_PATH)


@app.route('/')
def index():
    """渲染主页面，并加载当前的环境变量和历史日志。"""
    env_vars = dotenv_values(DOTENV_PATH)
    display_env = {key: value for key, value in env_vars.items() if key in ["TRACKING_NUMBER", "CHECK_INTERVAL", "BARK_SERVER", "BARK_KEY", "BARK_QUERY_PARAMS"]}
    initial_log = log_buffer.getvalue()
    return render_template('index.html', env_vars=display_env, initial_log=initial_log)

@socketio.on('connect')
def test_connect():
    """客户端连接时发送当前所有服务的状态。"""
    print('Client connected', flush=True)
    # 不再于连接时发送历史日志，因为这已由 initial_log 在页面加载时完成，避免重复
    emit('script_status', {'running': script_process is not None and script_process.poll() is None})
    emit('bark_server_status', {'running': bark_server_process is not None and bark_server_process.poll() is None})

# --- 追踪脚本控制 ---

def read_script_output():
    """从追踪脚本进程的管道中实时读取输出。"""
    global script_process
    if script_process is None:
        return
    try:
        for line in iter(script_process.stdout.readline, ''):
            log_line = f"[TRACKER] {line}" # 添加日志来源前缀
            log_buffer.write(log_line)
            socketio.emit('log_message', {'data': log_line})
    except Exception as e:
        error_line = f"[TRACKER] ERROR: 读取脚本输出时发生错误: {e}\n"
        log_buffer.write(error_line)
        socketio.emit('log_message', {'data': error_line})
    finally:
        if script_process and script_process.stdout:
            script_process.stdout.close()
        
        if script_process:
            return_code = script_process.wait()
            final_message = f"[TRACKER] 脚本已停止，返回码: {return_code}\n"
        else:
            final_message = "[TRACKER] 脚本已停止。\n"
        
        log_buffer.write(final_message)
        socketio.emit('log_message', {'data': final_message})
        socketio.emit('script_status', {'running': False})
        script_process = None

@socketio.on('start_script')
def start_script():
    """启动 Python 追踪脚本。"""
    global script_process, script_thread
    if script_process is not None and script_process.poll() is None:
        emit('log_message', {'data': "[TRACKER] 脚本已经在运行中。\n"})
        emit('script_status', {'running': True})
        return

    emit('log_message', {'data': "[SYSTEM] 正在启动追踪脚本...\n"})
    try:
        script_process = subprocess.Popen(
            [sys.executable, '-u', 'main.py'], # 使用 -u 标志确保实时输出
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        emit('log_message', {'data': "[TRACKER] 脚本已启动。\n"})
        emit('script_status', {'running': True})
        
        script_thread = threading.Thread(target=read_script_output, daemon=True)
        script_thread.start()
    except Exception as e:
        emit('log_message', {'data': f"[TRACKER] 启动脚本失败: {e}\n"})
        emit('script_status', {'running': False})

@socketio.on('stop_script')
def stop_script():
    """终止 Python 追踪脚本。"""
    global script_process
    if script_process is not None and script_process.poll() is None:
        try:
            script_process.terminate()
            emit('log_message', {'data': "[SYSTEM] 终止追踪脚本信号已发送。\n"})
        except Exception as e:
            emit('log_message', {'data': f"[TRACKER] 终止脚本失败: {e}\n"})
    else:
        emit('log_message', {'data': "[TRACKER] 脚本未运行。\n"})
        emit('script_status', {'running': False})


# --- Bark 服务控制 ---

def read_bark_output():
    """从 Bark 服务进程的管道中实时读取输出。"""
    global bark_server_process
    if bark_server_process is None:
        return
    try:
        for line in iter(bark_server_process.stdout.readline, ''):
            log_line = f"[BARK] {line}" # 添加日志来源前缀
            log_buffer.write(log_line)
            socketio.emit('log_message', {'data': log_line})
    except Exception as e:
        error_line = f"[BARK] ERROR: 读取服务输出时发生错误: {e}\n"
        log_buffer.write(error_line)
        socketio.emit('log_message', {'data': error_line})
    finally:
        if bark_server_process and bark_server_process.stdout:
            bark_server_process.stdout.close()
            
        if bark_server_process:
            return_code = bark_server_process.wait()
            final_message = f"[BARK] 服务已停止，返回码: {return_code}\n"
        else:
            final_message = "[BARK] 服务已停止。\n"
            
        log_buffer.write(final_message)
        socketio.emit('log_message', {'data': final_message})
        socketio.emit('bark_server_status', {'running': False})
        bark_server_process = None

@socketio.on('start_bark_server')
def start_bark_server():
    """启动 Bark 服务。"""
    global bark_server_process, bark_server_thread
    if bark_server_process is not None and bark_server_process.poll() is None:
        emit('log_message', {'data': "[BARK] 服务已经在运行中。\n"})
        emit('bark_server_status', {'running': True})
        return
    
    emit('log_message', {'data': "[SYSTEM] 正在启动 Bark 服务...\n"})
    try:
        # 确保数据目录存在
        os.makedirs('./bark-data', exist_ok=True)

        bark_server_process = subprocess.Popen(
            [BARK_EXECUTABLE, '-addr', '0.0.0.0:8080', '-data', './bark-data'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        emit('log_message', {'data': "[BARK] 服务已启动。\n"})
        emit('bark_server_status', {'running': True})
        
        bark_server_thread = threading.Thread(target=read_bark_output, daemon=True)
        bark_server_thread.start()
    except FileNotFoundError:
        emit('log_message', {'data': f"[BARK] 启动失败: 未找到可执行文件 '{BARK_EXECUTABLE}'。请确认已下载并放置在项目根目录，且文件名正确。\n"})
        emit('bark_server_status', {'running': False})
    except Exception as e:
        emit('log_message', {'data': f"[BARK] 启动服务失败: {e}\n"})
        emit('bark_server_status', {'running': False})

@socketio.on('stop_bark_server')
def stop_bark_server():
    """终止 Bark 服务。"""
    global bark_server_process
    if bark_server_process is not None and bark_server_process.poll() is None:
        try:
            bark_server_process.terminate()
            emit('log_message', {'data': "[SYSTEM] 终止 Bark 服务信号已发送。\n"})
        except Exception as e:
            emit('log_message', {'data': f"[BARK] 终止服务失败: {e}\n"})
    else:
        emit('log_message', {'data': "[BARK] 服务未运行。\n"})
        emit('bark_server_status', {'running': False})

# --- 环境变量更新 ---

@app.route('/update_env', methods=['POST'])
def update_env():
    """更新 .env 文件中的变量。"""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "无效的请求数据"}), 400

    updated_count = 0
    errors = []
    for key, value in data.items():
        try:
            set_key(DOTENV_PATH, key, value)
            updated_count += 1
        except Exception as e:
            errors.append(f"更新 {key} 失败: {e}")

    load_dotenv(DOTENV_PATH, override=True)

    if updated_count > 0:
        message = f"成功更新 {updated_count} 个环境变量。"
        if errors:
            message += " 部分变量更新失败: " + "; ".join(errors)
        socketio.emit('log_message', {'data': f"[SYSTEM] {message}\n[SYSTEM] 请注意：更新的环境变量将在脚本下次启动时生效。\n"})
        return jsonify({"status": "success", "message": message})
    else:
        return jsonify({"status": "error", "message": "没有变量被更新或发生错误: " + "; ".join(errors)}), 500

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=6060, debug=True)
