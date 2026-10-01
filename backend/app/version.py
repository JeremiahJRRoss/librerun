"""The deployment's version string, in one place.

This feeds two OTel resources that must agree: the backend plane's
``service.version`` (via the FastAPI app object, which ``init_otel`` reads)
and the ux plane's (``observability/web_telemetry._web_resource``). Before
this constant existed the value was hardcoded inline on the FastAPI app and
the ux plane simply had no ``service.version`` at all, so the two planes of
the same deployment could not be correlated by build.

The root ``VERSION`` file is the single source (blueprint S9); this constant
is asserted equal to it by the ``single-version`` job of
``.github/workflows/release-readiness.yml``, not derived from it. Deriving
would mean reading a file the image does not contain: the backend is not
installed as a distribution, it is copied into the image from ``backend/``,
and ``VERSION`` lives at the repository root. Bump both together — the job
fails the build when they drift, and is negative-tested against that drift.

``__license__`` is the platform's licence (L36, L40), which ``/api/v1/meta``
serves beside the version (A1, R17). It is ``REUSE.toml``'s default — the
licence a first-party file takes — and ``tests/test_source_access.py``
asserts the two equal rather than this module reading the map, for the
same reason: the image holds no root file.
"""

__version__ = "1.1.0-beta.1"
__license__ = "AGPL-3.0-only"
