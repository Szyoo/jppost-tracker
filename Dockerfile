# jppost-tracker Web 控制台镜像
# 容器不对外 publish 端口，建议由反向代理提供 HTTPS。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Tokyo

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ src/

# 运行期数据（SQLite、日志）都在挂载卷上，对应 /app/data 与 /app/logs
EXPOSE 6060

CMD ["python", "src/app.py"]
