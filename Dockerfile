# syntax=docker/dockerfile:1.7
# Quill images. Two targets share one build:
#   app  - API + worker + diarizer (CPU) on the latest stable CPython
#   web  - nginx serving the built frontend and proxying /api to the app
#
#   docker build --target app -t quill:dev .
#   docker build --target web -t quill-web:dev .
#
# The venvs live at the same paths the native install used (/opt/quill/venv and
# /opt/quill/diarizer-venv), so an existing /etc/quill/quill.env keeps working.

ARG PYTHON_IMAGE=python:3.14-slim
ARG NODE_IMAGE=node:24-slim
ARG NGINX_IMAGE=nginx:stable-alpine

# --- frontend ---------------------------------------------------------------
FROM ${NODE_IMAGE} AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build && test -f dist/index.html

# --- python environments ----------------------------------------------------
FROM ${PYTHON_IMAGE} AS venvs
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
# Backend: exactly the [project].dependencies of backend/pyproject.toml (the code
# itself runs from /app/backend via PYTHONPATH, as on the native install).
COPY backend/pyproject.toml /src/backend/pyproject.toml
RUN python - /src/backend/pyproject.toml > /tmp/backend-requirements.txt <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as handle:
    print("\n".join(sorted(tomllib.load(handle)["project"]["dependencies"])))
PY
RUN python -m venv /opt/quill/venv \
 && /opt/quill/venv/bin/pip install --upgrade pip wheel \
 && /opt/quill/venv/bin/pip install -r /tmp/backend-requirements.txt \
 && test -x /opt/quill/venv/bin/uvicorn
# Diarizer: torch CPU + NeMo-free transformers stack from the hashed lock. The heavy
# layer only rebuilds when the lock changes; the package code is installed after it.
COPY diarizer/requirements-lock-linux-x86_64-cpu.txt /src/diarizer/
RUN python -m venv /opt/quill/diarizer-venv \
 && /opt/quill/diarizer-venv/bin/pip install --upgrade pip wheel \
 && /opt/quill/diarizer-venv/bin/pip install --index-url https://pypi.org/simple \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      -r /src/diarizer/requirements-lock-linux-x86_64-cpu.txt
COPY diarizer/ /src/diarizer/
RUN /opt/quill/diarizer-venv/bin/pip install --no-deps /src/diarizer \
 && test -x /opt/quill/diarizer-venv/bin/quill-diarize

# --- app --------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS app
ARG QUILL_UID=999
ARG QUILL_GID=990
ARG QUILL_RELEASE=dev
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid "$QUILL_GID" quill \
 && useradd --system --uid "$QUILL_UID" --gid quill --home-dir /var/lib/quill --shell /usr/sbin/nologin quill
COPY --from=venvs /opt/quill /opt/quill
COPY backend/ /app/backend/
COPY deploy/backup.py /app/deploy/backup.py
ENV PATH=/opt/quill/venv/bin:$PATH \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/var/lib/quill \
    XDG_CACHE_HOME=/var/lib/quill/cache \
    HF_HOME=/var/lib/quill/cache/huggingface \
    TORCH_HOME=/var/lib/quill/cache/torch \
    NUMBA_CACHE_DIR=/var/lib/quill/cache/numba \
    QUILL_DATA_DIR=/var/lib/quill \
    QUILL_DIARIZER_CMD=/opt/quill/diarizer-venv/bin/quill-diarize \
    QUILL_RELEASE=${QUILL_RELEASE}
LABEL org.opencontainers.image.title="quill" org.opencontainers.image.version="${QUILL_RELEASE}"
WORKDIR /app/backend
USER quill
EXPOSE 8020
CMD ["uvicorn", "quill.app:app", "--host", "0.0.0.0", "--port", "8020", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-keep-alive", "75", "--timeout-graceful-shutdown", "15"]

# --- web --------------------------------------------------------------------
FROM ${NGINX_IMAGE} AS web
ARG QUILL_RELEASE=dev
COPY --from=frontend /src/frontend/dist /opt/quill-dist
COPY deploy/nginx-docker.conf /etc/nginx/conf.d/default.conf
COPY deploy/web-entrypoint.sh /docker-entrypoint.d/40-quill-publish.sh
RUN chmod 0755 /docker-entrypoint.d/40-quill-publish.sh \
 && mkdir -p /etc/quill /srv/www \
 && printf '# set_real_ip_from <reverse proxy IP>;\n' > /etc/quill/nginx-real-ip.conf
LABEL org.opencontainers.image.title="quill-web" org.opencontainers.image.version="${QUILL_RELEASE}"
EXPOSE 80
