#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# publish-purity (blueprint S9, decisions L21 and L39, gap I6): no tracked
# file, path or symlink target names THIS repository or its owner.
#
# The public repository is built from a clean initial commit of this
# tree; this one stays private as the development record. The tree is
# what gets published, so a clone URL, a CODEOWNERS entry, an image path
# or a permalink that carries the development repository's identity
# would walk straight into the public repository — and L39 goes further:
# nothing LibreRun publishes names the development repository's owner,
# in any spelling, prose included. The public repository's URL names
# the maintainer's own account, github.com/JeremiahJRRoss/librerun, and
# `scripts/prepare_public_repo.sh --check` refuses a placeholder owner
# segment that comes back.
#
# The check lives in scripts/check_purity.py, and it keeps DIGESTS, not
# names: written literally, the check would itself be a tracked file
# that names the owner, and the only way out would be to exclude it —
# the one file where a violation could then hide. It reads contents,
# paths and symlink targets (from the index, so materialised symlinks
# are covered), and it prints where a match is, never what it is.
#
# This script is the stable entry point: release-readiness.yml,
# release.yml and scripts/prepare_public_repo.sh call it.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
exec python3 scripts/check_purity.py
