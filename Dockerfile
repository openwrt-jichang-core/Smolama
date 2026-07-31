FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# 安装 curl 和 xvfb
RUN apt-get update && apt-get install -y \
    curl \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 预先创建 static 目录
RUN mkdir -p /app/static /app/backend/static

# 安装依赖
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 复制项目代码
COPY static/ /app/static/
COPY backend /app/backend

WORKDIR /app/backend

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/ || exit 1

# 设置默认虚拟显示器环境变量
ENV DISPLAY=:99

# 使用 shell 脚本形式在后台启动 Xvfb，然后再启动 uvicorn
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x1024x24 -ac & uvicorn main:app --host 0.0.0.0 --port 8000"]
