FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# 安装 curl 和 xvfb (用于虚拟桌面渲染)
RUN apt-get update && apt-get install -y \
    curl \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 预先创建 static 目录，防止 FastAPI 报错
RUN mkdir -p /app/static /app/backend/static

# 复制依赖并安装
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 复制静态资源及后端代码
COPY static/ /app/static/
COPY backend /app/backend

WORKDIR /app/backend

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/ || exit 1

# 用 xvfb-run 包裹启动命令
CMD ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1280x1024x24", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
