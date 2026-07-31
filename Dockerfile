# 钉死在 Debian 12 (bookworm)，不用会随 python:3.11-slim 滚动升级的最新版：
# Playwright 目前只正式支持到 bookworm，装到更新的 Debian 13 (trixie) 上时
# `playwright install --with-deps` 会套用不匹配的依赖包列表（比如把 trixie 已经
# 改名/移除的字体包也列进去），装不到就直接构建失败。
FROM python:3.11-slim-bookworm

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# 交互式登录功能要用到一个真实的无头 Chromium：装浏览器本体 + 它需要的系统依赖
# （字体、libnss3 之类）。这一步只能在 root 下跑（apt 装系统包），装完再把浏览器
# 目录的所有权交给下面创建的低权限用户。镜像会因此大不少（几百 MB），属于这个
# 功能本身的代价，不需要就不用改 docker-compose 里 shm_size 也没关系
# （launch 参数已经带了 --disable-dev-shm-usage）。
RUN PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 -m playwright install --with-deps chromium
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

COPY backend ./backend
COPY static ./static

ENV DATA_DIR=/data

# 以非 root 用户运行：即便攻击者通过某种途径（例如构造恶意生成代码）拿到进程权限，
# 破坏面也仅限于该低权限用户，而不是容器内 root。
RUN useradd --create-home --shell /usr/sbin/nologin scanner \
    && mkdir -p /data \
    && chown -R scanner:scanner /app /data /opt/pw-browsers

VOLUME ["/data"]

EXPOSE 8000

# /api/session 无需鉴权即可访问且响应很轻，适合当健康检查探针
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/session', timeout=3)" || exit 1

USER scanner
WORKDIR /app/backend
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
