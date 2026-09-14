# vCenter to OCI helper VM service
# Build from the repository root:  docker build -f helper/Dockerfile -t vc-oci-helper .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /opt/vc-oci
COPY helper/pyproject.toml ./helper/
COPY helper/helper_app/ ./helper/helper_app/

RUN pip install ./helper

# Job database and TLS material live on a volume
VOLUME ["/var/lib/vc-oci-helper", "/etc/vc-oci-helper"]
ENV HELPER_DB_PATH=/var/lib/vc-oci-helper/jobs.sqlite3 \
    HELPER_TLS_CERT_FILE=/etc/vc-oci-helper/server.crt \
    HELPER_TLS_KEY_FILE=/etc/vc-oci-helper/server.key

EXPOSE 8443
# Needs access to the paravirtualized volume device nodes: run with --device=/dev/oracleoci/... or --privileged
CMD ["vc-oci-helper"]
