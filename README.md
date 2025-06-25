# 日本邮政快递追踪与 Bark 通知服务

该项目提供一个基于 Flask + Vue 的简单网页界面，用于追踪日本邮政快递并通过 [Bark](https://github.com/Finb/bark-server) 进行推送通知。界面允许启动/停止追踪脚本和 Bark 服务，并支持在线修改 `.env` 环境变量。

## 安装

1. 安装 Python 依赖：
   ```bash
   pip install -r requirements.txt
   ```
2. （可选）运行 `install_bark.sh` 下载并配置 Bark Server 可执行文件：
   ```bash
   bash install_bark.sh
   ```

## 使用

1. 在项目根目录新建或编辑 `.env` 文件，设置以下变量：
   ```ini
   TRACKING_NUMBER=你的快递单号
   CHECK_INTERVAL=300
   BARK_SERVER=https://你的-bark-地址
   BARK_KEY=你的-bark-key
   BARK_QUERY_PARAMS=?sound=minuet&level=timeSensitive
   ```
2. 启动网页控制台：
   ```bash
   python src/app.py
   ```
3. 在浏览器访问 `http://localhost:6060`，即可通过界面管理追踪脚本和 Bark 服务。

## 目录结构

- `src/app.py`：Flask 应用及 WebSocket 服务
- `src/tracker.py`：查询日本邮政物流并推送 Bark 的脚本
- `src/templates/`：前端页面模板
- `src/static/`：前端静态资源
- `install_bark.sh`：自动下载并安装 Bark Server 的脚本

## 许可

本项目基于 MIT 许可证发布，详情参见仓库中的 LICENSE（如有）。
