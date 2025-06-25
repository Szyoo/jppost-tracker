import os
import time
import urllib.parse
from bs4 import BeautifulSoup
import requests
from dotenv import load_dotenv # 导入 load_dotenv

# Load environment variables from .env file
load_dotenv()

# ------------------ 配置区域 ------------------
# 从 .env 文件中读取配置
TRACKING_NUMBER = os.getenv("TRACKING_NUMBER")
TRACKING_URL = f"https://trackings.post.japanpost.jp/services/srv/search/direct?reqCodeNo1={TRACKING_NUMBER}&searchKind=S002&locale=ja"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))  # 默认值300秒

# Bark 推送配置
BARK_SERVER = os.getenv("BARK_SERVER")
BARK_KEY = os.getenv("BARK_KEY")
BARK_QUERY_PARAMS = os.getenv("BARK_QUERY_PARAMS", "?sound=minuet&level=timeSensitive") # 默认值
# ----------------------------------------------

def send_bark_notification(title, message):
    """
    通过 Bark 推送通知
    URL 格式: {BARK_SERVER}/{BARK_KEY}/{title}/{message}{BARK_QUERY_PARAMS}
    如果返回 502，则等待30秒后重新发送
    """
    # 将 safe 参数设为空字符串，确保所有字符都被编码
    title_enc = urllib.parse.quote(title, safe="")
    message_enc = urllib.parse.quote(message, safe="")
    url = f"{BARK_SERVER}/{BARK_KEY}/{title_enc}/{message_enc}{BARK_QUERY_PARAMS}"
    try:
        resp = requests.get(url)
        if resp.status_code == 200:
            print("Bark 通知已发送。", flush=True) # 添加 flush=True
        else:
            print(f"Bark 通知发送失败，状态码：{resp.status_code}，等待30秒后重试...", flush=True) # 添加 flush=True
            time.sleep(30)
            send_bark_notification(title, message)
    except Exception as e:
        print("发送 Bark 通知时出错：", e, flush=True) # 添加 flush=True

def get_latest_tracking_info(): # 不再需要 driver 参数
    """
    直接使用 requests 获取页面内容并解析最新的物流记录。
    """
    try:
        # 模拟浏览器头，提高请求成功率
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'zh-CN,zh;q=0.9,ja;q=0.8',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'sec-ch-ua': '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"macOS"'
            # 您可以尝试加上 Cookie，但通常首次GET请求不需要，或者网站会设置新的Cookie
            # 'Cookie': 'JSESSIONID=...; TS01b09507=...'
        }
        response = requests.get(TRACKING_URL, headers=headers)
        response.raise_for_status()  # 检查 HTTP 错误

        soup = BeautifulSoup(response.text, "html.parser")

        # 定位 summary 为 "履歴情報" 的物流信息表格
        table = soup.find("table", {"class": "tableType01 txt_c m_b5", "summary": "履歴情報"})
        if not table:
            print("未能找到物流信息表格。", flush=True) # 添加 flush=True
            return None

        # 查找所有日期单元格
        date_cells = table.find_all("td", class_="w_120")
        if not date_cells:
            print("未能提取到任何物流记录。", flush=True) # 添加 flush=True
            return None

        # 取最后一个记录作为最新进展
        latest_date = date_cells[-1].get_text(strip=True)
        latest_row = date_cells[-1].parent
        status_cell = latest_row.find("td", class_="w_150")
        latest_status = status_cell.get_text(strip=True) if status_cell else ""
        latest_info = f"{latest_date} {latest_status}"
        return latest_info
    except requests.exceptions.RequestException as e:
        print(f"请求快递信息失败: {e}", flush=True) # 添加 flush=True
        return None
    except Exception as e:
        print(f"解析快递信息时出错: {e}", flush=True) # 添加 flush=True
        return None

def main():
    print("快递监控程序启动...", flush=True) # 添加 flush=True

    last_info = None
    try:
        while True:
            current_info = get_latest_tracking_info()
            if current_info:
                print("最新物流记录：", current_info, flush=True) # 添加 flush=True
                # 首次获取到物流信息时直接发送通知
                if last_info is None:
                    send_bark_notification("快递更新通知", f"{current_info}\n查看详情：{TRACKING_URL}")
                    last_info = current_info
                elif current_info != last_info:
                    print("检测到快递进展更新！", flush=True) # 添加 flush=True
                    notify_message = f"{current_info}\n查看详情：{TRACKING_URL}"
                    send_bark_notification("快递更新通知", notify_message)
                    last_info = current_info
                else:
                    print(f"暂无更新。 当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True) # 添加 flush=True
            else:
                print("无法获取最新快递信息。", flush=True) # 添加 flush=True
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        print("程序终止。", flush=True) # 添加 flush=True
    finally:
        pass

if __name__ == "__main__":
    main()
