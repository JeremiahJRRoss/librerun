#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Regenerate the dependency locks from their sources.
#
#   backend/requirements.lock.txt          <- backend/requirements.txt
#   backend/adapters/requirements.lock.txt <- backend/adapters/pyproject.toml
#   backend/adapters/build-constraints.txt <- backend/adapters/pyproject.toml
#                                             ([build-system].requires)
#   services/gateway/requirements.lock.txt <- services/gateway/requirements.txt
#                                             (hashed; A3)
#
# Run this after any edit to either source, and commit the lock beside it. The
# image installs from the locks, so a stale lock means the change you made is
# not the change that ships. backend/tests/test_requirements_lock.py fails if
# the chassis lock drifts from its requirements in either direction.
#
# Three flags carry the contract and none is optional:
#   --python-version 3.12  matches backend/Dockerfile. A lock resolved on a
#                          different interpreter can pin wheels or markers the
#                          image cannot use.
#   --universal            keeps the locks valid on both amd64 and arm64
#                          builders, since python:3.12-slim is multi-arch.
#   --constraint           pins the adapter's shared packages to whatever the
#                          chassis lock already chose, so installing both is
#                          consistent. This is why order matters below.
#
# The adapter gets its own lock rather than rows in the chassis one because
# chassis-zero-agents deliberately runs with LangGraph absent; see the header
# of backend/adapters/requirements.lock.txt.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here/backend"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

# Keep each lock's explanatory header; uv emits only the resolution.
#
# The resolution is written to a temporary file, and uv reads the pins
# already in its --output-file as preferences. A fresh, empty file would
# give it none, and every floating line would re-resolve to whatever is
# newest: adding one requirement would move dozens of pins as a side
# effect. So the temporary file starts as a copy of the current lock, and
# a pin moves only when the change asks it to (D25: a pin moves by review,
# not by a side effect).
relock() {
  local source="$1" out="$2" ; shift 2
  local tmp head
  tmp="$(mktemp)"; head="$(mktemp)"
  if [ -f "$out" ]; then cp "$out" "$tmp"; fi
  uv pip compile "$source" --universal --python-version 3.12 --no-header \
     --output-file "$tmp" "$@"
  awk '/^#/ {print; next} {exit}' "$out" > "$head" 2>/dev/null || true
  cat "$head" "$tmp" > "$out"
  rm -f "$tmp" "$head"
  echo "wrote ${out#"$here/"}"
}

# Chassis first: the adapter is resolved against whatever it chose.
relock requirements.txt requirements.lock.txt
relock adapters/pyproject.toml adapters/requirements.lock.txt \
       --constraint requirements.lock.txt

# The adapter's BUILD requirements, which neither lock above covers.
#
# A runtime lock says nothing about PEP 517: pip resolves [build-system]
# requires in a fresh isolated environment, so `setuptools>=68` picked a new
# release on every rebuild — the same unbounded-range exposure as the outage,
# one layer down, and `--no-deps` does not reach it (that flag governs runtime
# dependencies only). pip DOES pass PIP_CONSTRAINT through to the build
# environment, which is the documented hook and the one the Dockerfile uses.
#
# Resolved from the pyproject's own [build-system].requires rather than a
# hand-typed name, so adding a build requirement cannot leave this behind.
build_requires="$(python3 - <<'PY'
import tomllib, pathlib
data = tomllib.loads(pathlib.Path("adapters/pyproject.toml").read_text())
print("\n".join(data["build-system"]["requires"]))
PY
)"
build_src="$(mktemp -t librerun-build-requires-XXXXXX.txt)"
printf '%s\n' "$build_requires" > "$build_src"
# --no-annotate: the provenance line would otherwise name this temporary
# path, which is neither stable nor informative. The source is the
# pyproject's [build-system].requires, stated in the file's own header.
relock "$build_src" adapters/build-constraints.txt \
       --constraint requirements.lock.txt --no-annotate
rm -f "$build_src"

# The gateway's lock (A3; LIC-09, C-21), hashed: services/gateway/Dockerfile
# installs it with --require-hashes and nothing else, so every pin carries
# the hash of each file PyPI serves for it, and a file that changes under a
# version fails the build. It is resolved in services/gateway/, so its
# provenance lines read `-r requirements.txt`, as
# backend/tests/test_requirements_lock.py expects.
#
# The first time, it was seeded from the chassis lock: every package the
# gateway shares with the chassis code it runs started at the chassis's
# pin, and only LiteLLM's own tree resolved fresh (§11 names each pin that
# moved). The seed is the chassis's pins under this lock's own header,
# since relock keeps the header of the file it finds. From then on the lock
# is its own preference, as each lock above is.
cd "$here/services/gateway"
if [ ! -f requirements.lock.txt ]; then
  {
    cat <<'HEADER'
# LibreRun gateway — resolved, hashed dependency lock. DO NOT EDIT BY HAND.
#
# services/gateway/Dockerfile installs this file with --require-hashes and
# nothing else (A3); requirements.txt beside it states what the gateway is
# willing to accept, and this states what it runs: every version exact,
# every file PyPI serves for it hashed. LiteLLM is here as a library alone,
# at the one version requirements.txt names (C-19 to C-21).
#
# Regenerate with scripts/lock_requirements.sh after ANY edit to
# requirements.txt; it resolves under Python 3.12, universally, as the
# chassis lock does. backend/tests/test_requirements_lock.py fails if the
# two drift apart in either direction or a pin loses its hashes.
HEADER
    grep -v '^#' "$here/backend/requirements.lock.txt"
  } > requirements.lock.txt
fi
relock requirements.txt requirements.lock.txt --generate-hashes
