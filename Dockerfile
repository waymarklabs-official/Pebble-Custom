# Multi-arch (amd64 / arm64): the same image runs on a Raspberry Pi 4/5 and on a mini PC.
# CI builds, tests and publishes it (.github/workflows/ci.yml); a production machine only pulls it.
#
# Reproducible and verified:
#   - the base image is pinned by digest (Dependabot proposes updates);
#   - every Python package, and the build backend, is pinned by version and sha256 in
#     requirements.txt / requirements-build.txt; pip refuses anything that doesn't match;
#   - wheels only (--only-binary): nothing is compiled from source with unpinned build tools.
#
# No system packages are needed by default. TV control over ADB is opt-in (Debian's adb package,
# from the pinned base image's apt sources):  docker build --build-arg WITH_ADB=1 .
# Both stages use the same base, written out in full so Dependabot can update it.
FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9 AS build
ENV PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
COPY requirements.txt requirements-build.txt ./
# Dependencies are resolved (not --no-deps), so a lock file missing any transitive package fails here.
RUN pip install --require-hashes --only-binary=:all: -r requirements-build.txt \
 && pip download --require-hashes --only-binary=:all: --dest /wheels -r requirements.txt \
 && cp requirements.txt /wheels/
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-deps --no-build-isolation --no-index --wheel-dir /wheels .

FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9
ARG WITH_ADB=0
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN if [ "$WITH_ADB" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends adb && rm -rf /var/lib/apt/lists/*; \
    fi \
 && useradd --create-home --uid 1000 ringhub \
 && mkdir -p /app \
 && install -d -o ringhub -g ringhub /app/data
# Installed straight from the build stage (bind mount), so the wheels never become an image layer.
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-index --find-links=/wheels --require-hashes --only-binary=:all: -r /wheels/requirements.txt \
 && pip install --no-index --no-deps /wheels/ringhub-*.whl \
 && pip check
USER ringhub
WORKDIR /app
# /app/data: SQLite (notes, reminders, catalog), audio. /home/ringhub/.android: ADB key (WITH_ADB only).
VOLUME ["/app/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=2).status==200 else 1)"
# Run with an init process (compose: `init: true`, docker run: `--init`) so signals reach uvicorn.
ENTRYPOINT ["ringhub"]
CMD ["serve", "-c", "/app/config.yaml"]
