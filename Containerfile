FROM docker.io/rclone/rclone:1.75.1@sha256:4055fb65b7a975caf3cb54a94bf12713e81947d6f3d0bb221907a05b0f6ab390 AS rclone
FROM docker.io/library/oraclelinux:9-slim@sha256:b61ecb8832422957df5ac8d5d463df46f17f0dddcaf92287f8f49e2953de65f2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
# Keep application packages separate from Oracle Linux's system Python.
RUN microdnf install -y python3.12 python3.12-pip shadow-utils ca-certificates && microdnf clean all
RUN python3.12 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /app
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
COPY requirements.lock requirements-build.lock pyproject.toml ./
RUN python -m pip install --no-cache-dir -r requirements.lock -r requirements-build.lock
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
# Editable layout retains colocated migration scripts for schema checks.
RUN python -m pip install --no-cache-dir --no-build-isolation --no-deps -e . && useradd --uid 10001 --create-home runner
USER 10001
CMD ["python", "-m", "uvicorn", "migration_runner.api:app", "--host", "0.0.0.0", "--port", "8000"]
