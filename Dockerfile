FROM python:3.12-slim-bookworm

ARG SOURCE_REVISION=unknown
ARG SOURCE_REPOSITORY=https://github.com/sqkkyzx/pve-webui-plugins
LABEL org.opencontainers.image.source="${SOURCE_REPOSITORY}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="0.3.0" \
      org.opencontainers.image.description="Non-invasive, selectable WebUI plugins for Proxmox VE" \
      org.opencontainers.image.licenses="GPL-3.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PVE_RUNTIME_DIR=/run/pve-webui-plugins
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && groupadd --gid 10001 extension \
    && useradd --uid 10001 --gid extension --no-create-home --shell /usr/sbin/nologin extension \
    && install -d -o 10001 -g 10001 -m 700 /run/pve-webui-plugins
COPY app/ ./app/
COPY plugins/ ./plugins/
COPY templates/ ./templates/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/pve-plugins/health', timeout=3).read()"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]
