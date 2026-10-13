ARG M1_BASE_IMAGE=ccr.ccs.tencentyun.com/taidai-holobase-168/omnipower_vifa@sha256:7a3026a62ce5fd33960c398529f20fdbf21b354103212ee44e9b1f34aee9779d
FROM ${M1_BASE_IMAGE}
WORKDIR /app
COPY --chown=10001:10001 m1/__init__.py /app/m1/__init__.py
COPY --chown=10001:10001 m1/dashboard_energy_api.py /app/m1/dashboard_energy_api.py
COPY --chown=10001:10001 m1/resident_api.py /app/m1/resident_api.py
COPY --chown=10001:10001 m1/http_reader.py /app/m1/http_reader.py
USER 10001:10001
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/python", "-m", "m1.resident_api"]
