# Build from the generated deployment package's backend/ directory.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    M4_SETTINGS_DB=/data/settings.sqlite3 \
    M4_DECISION_RESULTS_DIR=/data/solver-decisions \
    M4_NOCOBASE_TOKEN_FILE=/run/secrets/nocobase_token

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 m4 \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin m4 \
    && mkdir -p /app /opt/m4-runtime /data/solver-decisions \
    && chown 10001:10001 /data /data/solver-decisions

COPY requirements.lock.txt /opt/m4-runtime/requirements.lock.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: -r /opt/m4-runtime/requirements.lock.txt \
    && python -m pip check

COPY entrypoint.py healthcheck.py preflight.py /opt/m4-runtime/
COPY app/ /app/

WORKDIR /app

# The entrypoint reads the Docker secret and prepares the volume, then permanently
# drops to UID/GID 10001 before starting the single Uvicorn worker.
USER root
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/opt/m4-runtime/healthcheck.py"]
ENTRYPOINT ["python", "/opt/m4-runtime/entrypoint.py"]
