# Docker Hub's official image via Google's mirror: CI kept hitting Docker Hub pull limits.
FROM mirror.gcr.io/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/solarbank.db \
    PORT=8080

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY simulator ./simulator

# Which commit this image was built from, shown in the dashboard footer and logged at startup.
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

RUN useradd --system --uid 1000 solarbank && mkdir -p /data && chown solarbank /data
USER solarbank
VOLUME /data
EXPOSE 8080

# Checks the web server only; whether the battery is reachable is shown on
# the dashboard (and at /healthz) rather than marking the container unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8080\")}/api/live', timeout=4)" || exit 1

CMD ["python", "-m", "app"]
