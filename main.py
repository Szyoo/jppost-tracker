import time
import urllib.parse
from bs4 import BeautifulSoup
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ------------------ 配置区域 ------------------
TRACKING_URL = "https://trackings.post.japanpost.jp/services/srv/search/direct?reqCodeNo1=EF000497545CN&searchKind=S002&locale=ja"
CHECK_INTERVAL = 60 * 5  # 每5分钟检查一次

# Bark 推送配置
BARK_SERVER = "https://bark-server-necg.onrender.com"
BARK_KEY = "szyyw"
BARK_QUERY_PARAMS = "?sound=minuet&level=critical&volume=5"
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
            print("Bark 通知已发送。")
        else:
            print(f"Bark 通知发送失败，状态码：{resp.status_code}，等待30秒后重试...")
            time.sleep(30)
            send_bark_notification(title, message)
    except Exception as e:
        print("发送 Bark 通知时出错：", e)

def get_latest_tracking_info(driver):
    """
    使用 Selenium 加载页面并解析最新的物流记录。
    这里仅选择 summary 属性为 "履歴情報" 的表格，
    并查找最新一条记录的日期（td.w_120）和状态描述（td.w_150）。
    """
    driver.get(TRACKING_URL)
    try:
        # 等待物流信息表格加载完成，最多等待10秒
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table.tableType01.txt_c.m_b5[summary='履歴情報']"))
        )
    except Exception as e:
        print("等待物流信息表格超时或出错：", e)
        return None

    html = driver.page_source
    soup = BeautifulSoup(html, "html.parser")
    
    # 定位 summary 为 "履歴情報" 的物流信息表格
    table = soup.find("table", {"class": "tableType01 txt_c m_b5", "summary": "履歴情報"})
    if not table:
        print("未能找到物流信息表格。")
        return None

    # 查找所有日期单元格
    date_cells = table.find_all("td", class_="w_120")
    if not date_cells:
        print("未能提取到任何物流记录。")
        return None

    # 取最后一个记录作为最新进展
    latest_date = date_cells[-1].get_text(strip=True)
    latest_row = date_cells[-1].parent
    status_cell = latest_row.find("td", class_="w_150")
    latest_status = status_cell.get_text(strip=True) if status_cell else ""
    latest_info = f"{latest_date} {latest_status}"
    return latest_info

def main():
    print("快递监控程序启动...")

    # 设置 Chrome 配置选项
    chrome_options = Options()
    # 若需显示浏览器窗口，请注释掉下面这行 headless 参数
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])

    
    # 初始化 Chrome 驱动，使用 Service 对象管理驱动
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)

    last_info = None
    try:
        while True:
            current_info = get_latest_tracking_info(driver)
            if current_info:
                print("最新物流记录：", current_info)
                # 首次获取到物流信息时直接发送通知
                if last_info is None:
                    send_bark_notification("快递更新通知", f"{current_info}\n查看详情：{TRACKING_URL}")
                    last_info = current_info
                elif current_info != last_info:
                    print("检测到快递进展更新！")
                    notify_message = f"{current_info}\n查看详情：{TRACKING_URL}"
                    send_bark_notification("快递更新通知", notify_message)
                    last_info = current_info
                else:
                    print("暂无更新。")
            else:
                print("无法获取最新快递信息。")
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        print("程序终止。")
    finally:
        driver.quit()

if __name__ == "__main__":
    main()
