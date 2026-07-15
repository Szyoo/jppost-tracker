# jppost-tracker Web 控制台镜像
# 部署上下文见 docs/vultr-vps-deploy.md：容器不对外 publish 端口，
# 只挂 ingress 网络，由 Caddy 按子域名反代。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Tokyo

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ src/

# 运行期数据（SQLite、日志）都在挂载卷上，见 deploy/vps/docker-compose.yml
EXPOSE 6060

CMD ["python", "src/app.py"]
