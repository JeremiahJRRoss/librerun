"""The gateway's version string, in one place (blueprint S9).

The gateway is a service, not a distribution: its image is built from
this directory and the repository root, and nothing pip-installs it. So
the version is a constant here — the shape ``backend/app/version.py``
already uses — rather than package metadata that does not exist.

The root ``VERSION`` file is the single source (L17's release
engineering, S9). This constant is not *derived* from it: the file is
not copied into the image, and a constant that reads a missing file at
import time is a boot failure waiting for the first operator who copies
the directory somewhere else. It is *asserted equal* to it instead, by
the ``single-version`` job of ``.github/workflows/release-readiness.yml``
— which fails the build when the two drift, and is negative-tested
against that exact drift.
"""

__version__ = "1.1.0-beta.1"
