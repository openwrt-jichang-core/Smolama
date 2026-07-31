FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# 安装 curl 和 xvfb (用于虚拟屏幕渲染)
RUN apt-get update && apt-get install -y \
    curl \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 预创建静态目录
RUN mkdir -p /app/static /app/backend/static

# 复制依赖并安装
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 💥 关键修复：强行重新安装/补全与当前 Playwright 版本匹配的 Chromium
RUN playwright install chromium

# 复制项目代码
COPY static/ /app/static/
COPY backend /app/backend

WORKDIR /app/backend

EXPOSE 8000

# 设置虚拟显示器
ENV DISPLAY=:99

# 健康检查
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/ || exit 1

# 后台启动 Xvfb，并以反向代理模式启动 Uvicorn
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x1024x24 -ac & exec uvicorn main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
