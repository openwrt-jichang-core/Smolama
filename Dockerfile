FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# 安装 xvfb (用于提供虚拟图形显示)
RUN apt-get update && apt-get install -y \
    xvfb \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制依赖文件并安装
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 安装 Playwright 浏览器
RUN playwright install chromium

# 复制后端项目代码
COPY backend /app/backend

WORKDIR /app/backend

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/session')" || exit 1

# 使用 Xvfb 启动 FastAPI，让 Playwright 在虚拟屏下稳定运行
CMD ["xvfb-run", "--server-args=-screen 0 1280x800x24", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
