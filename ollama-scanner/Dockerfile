FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend ./backend
COPY static ./static

ENV DATA_DIR=/data

# 以非 root 用户运行：即便攻击者通过某种途径（例如构造恶意生成代码）拿到进程权限，
# 破坏面也仅限于该低权限用户，而不是容器内 root。
RUN useradd --create-home --shell /usr/sbin/nologin scanner \
    && mkdir -p /data \
    && chown -R scanner:scanner /app /data

VOLUME ["/data"]

EXPOSE 8000

USER scanner
WORKDIR /app/backend
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
