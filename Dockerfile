FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# 安装 curl 用于 Docker 健康检查
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 复制依赖文件并安装 Python 依赖库
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 复制后端项目代码
COPY backend /app/backend

WORKDIR /app/backend

EXPOSE 8000

# 健康检查：检查 FastAPI 是否已成功监听 8000 端口
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/ || exit 1

# 直接启动 FastAPI 服务
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
