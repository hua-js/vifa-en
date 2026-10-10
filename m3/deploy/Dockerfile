FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NUMBA_CACHE_DIR=/tmp/numba-cache

RUN --mount=type=tmpfs,target=/dev/mqueue apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 m3 \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /app --shell /usr/sbin/nologin m3

WORKDIR /app

COPY m3/deploy/requirements.lock.txt /tmp/m3-requirements.lock.txt
COPY m4/deploy/backend/requirements.lock.txt /tmp/m4-requirements.lock.txt
RUN --mount=type=tmpfs,target=/dev/mqueue \
    --mount=type=cache,id=vifa-m3-pip,target=/root/.cache/pip,sharing=locked \
    python -m pip install --require-hashes \
        --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
        --requirement /tmp/m3-requirements.lock.txt \
    && python -m pip install --no-cache-dir --only-binary=:all: \
        --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
        --requirement /tmp/m4-requirements.lock.txt \
    && python -m pip check \
    && rm -f /tmp/m3-requirements.lock.txt /tmp/m4-requirements.lock.txt

COPY --chown=10001:10001 m3/__init__.py /app/m3/__init__.py
COPY --chown=10001:10001 m3/worker /app/m3/worker
# 全量模块：M1/M2 Node-RED 启动器与 PV 服务共用同一 RUNTIME_IMAGE，
# 缺任一模块会导致对应链路容器秒退（2026-10-10 事故）。
COPY --chown=10001:10001 m1 /app/m1
COPY --chown=10001:10001 m2 /app/m2
COPY --chown=10001:10001 m3/contracts /app/m3/contracts
COPY --chown=10001:10001 m3/pv /app/m3/pv
COPY --chown=10001:10001 m3/scripts /app/m3/scripts
COPY --chown=10001:10001 m4/__init__.py /app/m4/__init__.py
COPY --chown=10001:10001 m4/settings /app/m4/settings
COPY --chown=10001:10001 m4/optimizer /app/m4/optimizer
COPY --chown=10001:10001 m4/orchestrator /app/m4/orchestrator
COPY --chown=10001:10001 m4/selection /app/m4/selection
COPY --chown=10001:10001 shared /app/shared
COPY --chown=10001:10001 config/projects /app/config/projects
COPY --chown=10001:10001 pyproject.toml uv.lock /app/
COPY m3/deploy/container-entrypoint.sh /usr/local/bin/vifa-m3-entrypoint

USER 10001:10001
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/vifa-m3-entrypoint"]
