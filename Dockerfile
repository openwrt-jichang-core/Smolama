# 1. 改用微软官方预装了 Playwright + Chromium + 系统依赖的 Python 镜像
#    解决在 slim 镜像上用 apt/playwright 安装依赖时内存暴涨导致的 Coolify 255 退出问题
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# 2. 安装 Python 依赖
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# 3. 复制前端与后端代码
COPY backend ./backend
COPY static ./static

ENV DATA_DIR=/data

# 4. 保留非 root 低权限用户安全机制
RUN useradd --create-home --shell /usr/sbin/nologin scanner \
    && mkdir -p /data \
    && chown -R scanner:scanner /app /data

VOLUME ["/data"]

EXPOSE 8000

# 5. 保留健康检查逻辑
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/session')" || exit 1

# 6. 以低权限 scanner 用户运行服务
USER scanner
WORKDIR /app/backend
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
