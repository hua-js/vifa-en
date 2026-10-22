# Supply the verified current M2 production image digest when preparing a release.
ARG M2_BASE_IMAGE
FROM ${M2_BASE_IMAGE}
WORKDIR /app
COPY --chown=10001:10001 m2/energy-efficiency-api.py /app/m2/energy-efficiency-api.py
COPY --chown=10001:10001 m2/station_energy_data_adapter.py /app/m2/station_energy_data_adapter.py
COPY --chown=10001:10001 m2/station_efficiency_job.py /app/m2/station_efficiency_job.py
COPY --chown=10001:10001 m2/station_efficiency_history.py /app/m2/station_efficiency_history.py
COPY --chown=10001:10001 m2/station_efficiency_nocobase.py /app/m2/station_efficiency_nocobase.py
COPY --chown=10001:10001 m2/resident_api.py /app/m2/resident_api.py
COPY --chown=10001:10001 m2/read_transport.py /app/m2/read_transport.py
USER 10001:10001
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/python", "-m", "m2.resident_api"]
