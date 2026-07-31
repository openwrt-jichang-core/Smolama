FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# 安装 curl 用于健康检查
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1. 预先创建 static 目录，防止 FastAPI 加载静态资源路由时抛出 Directory does not exist 异常
RUN mkdir -p /app/static /app/backend/static

# 复制 Python 依赖文件并安装
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 复制前端静态文件（如果项目根目录下有 static 或 dist 文件夹）
COPY static/ /app/static/
# 复制后端代码
COPY backend /app/backend

WORKDIR /app/backend

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/ || exit 1

# 启动服务
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
