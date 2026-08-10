# IPO Pulse — backend CLI + the static studio, in one image.
#
#   docker compose up                      studio on http://localhost:8000
#   docker compose run --rm cli build      any ipopulse command
#
# The image carries the code; your data stays on the host through the volumes
# in docker-compose.yml, so YAML edits and published JSON survive a rebuild.

FROM python:3.12-slim AS base

# Faster, quieter, and no stale .pyc in the layer
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements first so dependency layers cache across code edits
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

# Run as a normal user rather than root. UID 1000 matches the default first
# user on most Linux hosts, so bind-mounted data stays writable; `getent`
# guards against a base image that already claims that UID. USER is numeric so
# it works regardless of which branch ran.
RUN set -eux; \
    if ! getent passwd 1000 >/dev/null; then \
        useradd --create-home --uid 1000 pulse; \
    fi; \
    mkdir -p /app/backend/data/cache /app/backend/out /app/frontend/data; \
    chown -R 1000:1000 /app
USER 1000

WORKDIR /app/backend
ENV IPOPULSE_HOST=0.0.0.0
EXPOSE 8000

# A container with no port bound is a container you can't reach — bind 0.0.0.0
CMD ["python", "-m", "ipopulse.cli", "serve", "--host", "0.0.0.0", "--port", "8000"]
