FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
WORKDIR /app
COPY . .
RUN uv sync --locked --no-editable

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl gdb git iproute2 less lsof procps rsync strace util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" HOME=/tmp UV_CACHE_DIR=/tmp/uv-cache

RUN podbench --version
USER 65532:65532
ENTRYPOINT ["podbench"]
CMD ["--version"]
