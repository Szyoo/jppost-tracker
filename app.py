import os
import subprocess
import threading
import sys
import io # 导入 io 模块
import time # 用于 time.sleep

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv, set_key, dotenv_values

# 初始化 Flask 应用
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key_here' # 生产环境中请使用更安全的密钥
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*") # 允许所有CORS源，生产环境请限制

# 脚本运行状态
script_process = None
script_thread = None
log_buffer = io.StringIO() # 用于捕获脚本输出，并实现持久化直到脚本终止

# 环境变量文件路径
DOTENV_PATH = os.path.join(os.path.dirname(__file__), '.env')

# 加载环境变量
load_dotenv(DOTENV_PATH)

@app.route('/')
def index():
    """渲染主页面，并加载当前的环境变量和历史日志。"""
    env_vars = dotenv_values(DOTENV_PATH)
    display_env = {key: value for key, value in env_vars.items() if key in ["TRACKING_NUMBER", "CHECK_INTERVAL", "BARK_SERVER", "BARK_KEY", "BARK_QUERY_PARAMS"]}

    # 页面加载时，从 log_buffer 中获取现有日志
    initial_log = log_buffer.getvalue()

    return render_template('index.html', env_vars=display_env, initial_log=initial_log)

@socketio.on('connect')
def test_connect():
    """客户端连接时发送当前日志和脚本状态。"""
    print('Client connected', flush=True) # 调试信息
    emit('log_message', {'data': log_buffer.getvalue()}) # 发送现有日志
    emit('script_status', {'running': script_process is not None and script_process.poll() is None})


@socketio.on('start_script')
def start_script():
    """启动 Python 脚本。"""
    global script_process, script_thread
    if script_process is not None and script_process.poll() is None:
        emit('log_message', {'data': "脚本已经在运行中。\n"})
        emit('script_status', {'running': True})
        return

    # 每次启动时清空内存中的日志缓冲区
    log_buffer.truncate(0)
    log_buffer.seek(0)
    emit('log_message', {'data': "已清空旧的日志记录。\n"}) # 通知前端日志已清空

    # 以非阻塞方式启动脚本，stdout/stderr 重定向到 PIPE
    try:
        script_process = subprocess.Popen(
            [sys.executable, 'main.py'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, # 将错误输出也重定向到标准输出
            text=True, # 确保输出是文本而不是字节
            bufsize=1 # 行缓冲，重要！
        )
        emit('log_message', {'data': "脚本已启动。\n"})
        emit('script_status', {'running': True})

        # 启动一个线程来实时读取脚本的输出
        script_thread = threading.Thread(target=read_script_output, daemon=True)
        script_thread.start()
    except Exception as e:
        emit('log_message', {'data': f"启动脚本失败: {e}\n"})
        emit('script_status', {'running': False})

def read_script_output():
    """从脚本进程的管道中实时读取输出并发送到 WebSocket。"""
    global script_process
    if script_process is None:
        return

    # 使用 iter 和 readline 来实时读取行，直到遇到空字符串 (EOF)
    # 结合 socketio.sleep 确保不会阻塞 eventlet
    try:
        for line in iter(script_process.stdout.readline, ''):
            log_buffer.write(line) # 写入到内存缓冲区
            socketio.emit('log_message', {'data': line}) # 实时发送到前端
            # 可以在这里添加一个非常小的睡眠，让出CPU，但对于行缓冲通常不是严格必须的
            # socketio.sleep(0.01) # 可选，如果 still 遇到延迟可以尝试
    except Exception as e:
        # 捕获读取管道时可能发生的错误
        log_buffer.write(f"ERROR: 读取脚本输出时发生错误: {e}\n")
        socketio.emit('log_message', {'data': f"ERROR: 读取脚本输出时发生错误: {e}\n"})
    finally:
        if script_process and script_process.stdout:
            script_process.stdout.close() # 关闭管道

        # 等待进程完全结束并获取返回码
        if script_process:
            return_code = script_process.wait()
            final_message = f"脚本已停止，返回码: {return_code}\n"
        else:
            final_message = "脚本已停止。\n" # 如果 script_process 已经 None

        log_buffer.write(final_message)
        socketio.emit('log_message', {'data': final_message})
        socketio.emit('script_status', {'running': False})
        script_process = None # 清理进程对象

@socketio.on('stop_script')
def stop_script():
    """终止 Python 脚本。"""
    global script_process
    if script_process is not None and script_process.poll() is None:
        try:
            script_process.terminate() # 发送 SIGTERM 信号
            # 等待一小段时间，让脚本有机会优雅退出
            # read_script_output 线程会捕获到进程结束并更新状态
            # 这里不需要 script_process.wait()，否则会阻塞 Flask 主线程
            emit('log_message', {'data': "终止脚本信号已发送。\n"})
        except Exception as e:
            emit('log_message', {'data': f"终止脚本失败: {e}\n"})
    else:
        emit('log_message', {'data': "脚本未运行。\n"})
        emit('script_status', {'running': False})


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

    load_dotenv(DOTENV_PATH, override=True) # override=True 确保覆盖现有变量

    if updated_count > 0:
        message = f"成功更新 {updated_count} 个环境变量。"
        if errors:
            message += " 部分变量更新失败: " + "; ".join(errors)
        socketio.emit('log_message', {'data': f"{message}\n请注意：更新的环境变量将在脚本下次启动时生效。\n"})
        return jsonify({"status": "success", "message": message})
    else:
        return jsonify({"status": "error", "message": "没有变量被更新或发生错误: " + "; ".join(errors)}), 500

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=6060, debug=True)
