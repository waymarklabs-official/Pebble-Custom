# syntax=docker/dockerfile:1.7
# Multi-arch friendly (amd64 / arm64 / armv7): builds the same on a Pi 4/5 or an x86 box.
FROM python:3.12-slim-bookworm AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[llm]"

FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends adb tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 ringhub
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
USER ringhub
WORKDIR /app
# /app/data: sqlite, audio, notes. /home/ringhub/.android: ADB key the TVs were told to trust.
VOLUME ["/app/data", "/home/ringhub/.android"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=2).status==200 else 1)"
ENTRYPOINT ["tini", "--", "ringhub"]
CMD ["serve", "-c", "/app/config.yaml"]
