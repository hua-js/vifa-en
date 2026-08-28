FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NUMBA_CACHE_DIR=/tmp/numba-cache

RUN --mount=type=tmpfs,target=/dev/mqueue apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 m3 \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /app --shell /usr/sbin/nologin m3

WORKDIR /app

COPY m3/requirements.lock.txt /tmp/m3-requirements.lock.txt
RUN --mount=type=tmpfs,target=/dev/mqueue \
    --mount=type=cache,id=vifa-m3-pip,target=/root/.cache/pip,sharing=locked \
    python -m pip install --require-hashes \
        --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
        --requirement /tmp/m3-requirements.lock.txt \
    && rm -f /tmp/m3-requirements.lock.txt

COPY --chown=10001:10001 m3_worker /app/m3_worker
COPY --chown=10001:10001 pyproject.toml uv.lock /app/
COPY m3/deploy/container-entrypoint.sh /usr/local/bin/vifa-m3-entrypoint

USER 10001:10001
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/vifa-m3-entrypoint"]
