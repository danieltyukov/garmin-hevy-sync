# syntax=docker/dockerfile:1

# Build: resolve the locked dependencies into a self-contained virtualenv.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Run: the virtualenv on a slim Python, as an unprivileged user.
FROM python:3.12-slim
LABEL org.opencontainers.image.title="garmin-hevy-sync" \
      org.opencontainers.image.description="Two-way sync between a Garmin watch and Hevy" \
      org.opencontainers.image.source="https://github.com/danieltyukov/garmin-hevy-sync" \
      org.opencontainers.image.licenses="MIT"
# tzdata so TZ= gives local timestamps in the log.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --home-dir /data --create-home --shell /usr/sbin/nologin ghsync
COPY --from=build /app/.venv /app/.venv
# Everything stateful lives under /data, one volume: the tool's own folder plus
# ~/.garminconnect (Garmin tokens) and ~/.hevy2garmin, because HOME is /data.
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/data \
    GH_SYNC_HOME=/data/garmin-hevy-sync \
    GH_SYNC_CONTAINER=1 \
    PYTHONUNBUFFERED=1
USER ghsync
WORKDIR /data
VOLUME ["/data"]
ENTRYPOINT ["garmin-hevy-sync"]
CMD ["sync", "--every", "30m"]
