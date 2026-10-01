"""No provider SDK is importable from this process (blueprint S4a, L23).

The backend is the one process every ``python-package`` agent shares.
A provider client here — and the credential it would need — is readable
by all of them, which is why the gateway holds both and this process
reaches it over HTTP.

``requirements.txt`` says so, and a test already reads it. This one
reads the SOURCE, because the two failures are different: a line in
requirements is a dependency nobody may add, and an ``import openai``
inside a function is a dependency nobody may USE. The second one is
what actually broke a run — the demo agent's retry wrapper kept three
lazy provider imports to name exception classes from, long after the
clients they belonged to were deleted. Nothing imported that path in
the suite; the first real run raised ``ModuleNotFoundError`` and the
step failed with a message about a missing module rather than anything
about a model.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
SCANNED = (BACKEND / "app", BACKEND / "agents")

# Top-level module names, matched exactly against the root of a dotted
# path: ``openinference.instrumentation`` is not ``openai``.
PROVIDER_MODULES = frozenset(
    {
        "openai",
        "anthropic",
        "litellm",
        "cohere",
        "mistralai",
        "vertexai",
        "google_genai",
    }
)
# ``from google.genai import ...`` — a provider under a package that has
# entirely legitimate other members (``google.auth`` is a dependency).
PROVIDER_PATHS = frozenset({("google", "genai")})


def _root(dotted: str) -> str:
    return dotted.split(".", 1)[0]


def _is_provider(dotted: str) -> bool:
    parts = tuple(dotted.split("."))
    return _root(dotted) in PROVIDER_MODULES or parts[:2] in PROVIDER_PATHS


def provider_imports_in(path: Path) -> list[tuple[int, str]]:
    """Every provider import in one file, at any depth — module scope,
    inside a function, inside a ``try``. Returns (line, module)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:  # pragma: no cover - a broken file is another test's
        return []
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_provider(alias.name):
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has module None and level > 0.
            if node.module and node.level == 0 and _is_provider(node.module):
                found.append((node.lineno, node.module))
    return found


def _python_files():
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def test_the_backend_imports_no_provider_sdk_anywhere():
    offenders = []
    for path in _python_files():
        for line, module in provider_imports_in(path):
            offenders.append(f"{path.relative_to(BACKEND)}:{line} imports {module}")
    assert not offenders, (
        "a provider SDK is imported in the process every python-package "
        "agent shares (blueprint S4a, L23). Model calls go to the gateway "
        "over HTTP; catch app.capabilities.LlmError rather than a provider's "
        "exception class:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "source",
    [
        "import anthropic\n",
        "import openai.types\n",
        "from google.genai import errors\n",
        "def f():\n    import litellm\n",
        "def f():\n    try:\n        import openai\n    except ImportError:\n        pass\n",
    ],
)
def test_the_scan_catches_an_import_at_any_depth(tmp_path, source):
    """The gate, negative-tested at each shape it has to catch —
    including the one that actually happened: an import inside a
    function, which no module-scope check would see."""
    probe = tmp_path / "probe.py"
    probe.write_text(source)
    assert provider_imports_in(probe), f"the scan missed:\n{source}"


def test_the_scan_admits_the_packages_that_are_not_providers(tmp_path):
    """`google.auth` and `openinference` are real dependencies of this
    process. A gate that rejected them would be removed the first time
    it fired, and then it would not be a gate."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import google.auth\n"
        "from google.oauth2 import service_account\n"
        "from openinference.instrumentation import safe_json_dumps\n"
        "import openinference.semconv\n"
    )
    assert provider_imports_in(probe) == []
