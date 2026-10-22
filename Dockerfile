# Scoring API image (Phase 11 service, packaged in Phase 12). Local use: no authentication or TLS.
# Build:  docker compose build        Run: docker compose up
# The model and the data lake are NOT in the image; compose mounts them read-only.

FROM python:3.12-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
# Only the serving dependencies (no pytest, matplotlib, shap, mlflow or duckdb).
RUN python -m venv /opt/venv && /opt/venv/bin/pip install ".[serve]"

FROM python:3.12-slim-bookworm
# LightGBM needs the OpenMP runtime, which the slim image does not include.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin mmrisk
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 10001
EXPOSE 8000
# Standard library only, so the check itself needs almost no memory.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/ready', timeout=3)"]
# 0.0.0.0 is the container's own interface: --allow-external is needed for that, and compose only
# publishes the port on the host's 127.0.0.1.
CMD ["python", "-m", "mmrisk.api", "serve", "--host", "0.0.0.0", "--allow-external", \
     "--port", "8000", "--model-dir", "/app/models/advanced", "--lake", "/app/data/processed"]
