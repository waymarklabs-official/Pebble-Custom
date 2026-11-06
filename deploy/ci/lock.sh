#!/usr/bin/env bash
# Regenerate the hash-locked dependency files from pyproject.toml (needs uv: https://docs.astral.sh/uv/).
#   bash deploy/ci/lock.sh               re-lock, keeping current versions where they still fit
#   bash deploy/ci/lock.sh --upgrade     move everything to the newest allowed versions
# Then run the tests; CI installs exactly these files, and so does the Docker build.
set -euo pipefail
cd "$(dirname "$0")/../.."
common=(--universal --python-version 3.11 --generate-hashes --quiet "$@")
uv pip compile pyproject.toml --extra llm "${common[@]}" -o requirements.txt
uv pip compile pyproject.toml --extra llm --extra dev "${common[@]}" -c requirements.txt -o requirements-dev.txt
uv pip compile requirements-build.in "${common[@]}" -c requirements-dev.txt -o requirements-build.txt
echo "locked: requirements.txt requirements-dev.txt requirements-build.txt"
