# syntax=docker/dockerfile:1.7
# Multi-arch (amd64 / arm64): the same image runs on a Raspberry Pi 4/5 and on a mini PC.
# No system packages are needed by default; TV control over ADB is opt-in:
#   docker build --build-arg WITH_ADB=1 .
FROM python:3.12-slim-bookworm AS build
# Patient downloads: a Pi on busy Wi-Fi (or sharing bandwidth with a stream) can be slow.
ENV PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[llm]"

FROM python:3.12-slim-bookworm
ARG WITH_ADB=0
RUN if [ "$WITH_ADB" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends adb && rm -rf /var/lib/apt/lists/*; \
    fi \
 && useradd --create-home --uid 1000 ringhub
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/* && rm -rf /wheels
USER ringhub
WORKDIR /app
# /app/data: SQLite (notes, reminders, catalog), audio. /home/ringhub/.android: ADB key (WITH_ADB only).
VOLUME ["/app/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=2).status==200 else 1)"
# Run with an init process (compose: `init: true`, docker run: `--init`) so signals reach uvicorn.
ENTRYPOINT ["ringhub"]
CMD ["serve", "-c", "/app/config.yaml"]
