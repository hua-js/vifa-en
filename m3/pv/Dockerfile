ARG M3_BASE_IMAGE=vifa-m3:0.1.0
FROM ${M3_BASE_IMAGE}
USER root
COPY --chown=10001:10001 m3/__init__.py /app/m3/__init__.py
COPY --chown=10001:10001 m3/worker /app/m3/worker
COPY --chown=10001:10001 m3/scripts /app/m3/scripts
COPY --chown=10001:10001 m3/contracts /app/m3/contracts
COPY --chown=10001:10001 pv-training /app/pv-training
COPY --chown=10001:10001 m3/pv/entrypoint.sh /app/m3/pv/entrypoint.sh
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /app
ENTRYPOINT ["/bin/sh", "/app/m3/pv/entrypoint.sh"]
CMD ["pv"]
