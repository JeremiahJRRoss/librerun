"""The SDK's core imports with nothing installed but the standard library.

``pyproject.toml`` declares ``dependencies = []`` on purpose: an agent
image installs the SDK and the author's own file and nothing else, so a
module-scope import of a third-party package breaks ``import
librerun_agent`` in every such image — and the failure shows up as a
container that never answers ``/healthz``, with ``docker logs`` empty by
design.

That is not hypothetical. The first draft of ``_llm.py`` imported
``httpx`` at module scope; the container battery caught it, and this
test is here so the battery does not have to catch it again.

The extras (``uvicorn``, ``otel``) are real dependencies of the features
that use them, so they may be imported — lazily, inside the function
that needs them, where an ImportError can be handled or explained.
"""
from __future__ import annotations

import ast
import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "librerun_agent"

# What an agent image is guaranteed to have: this package, and the
# standard library.
ALLOWED_ROOTS = {"librerun_agent"} | set(sys.stdlib_module_names)


def _module_scope_imports(tree: ast.Module) -> set[str]:
    """Top-level imports only — the ones that run at ``import
    librerun_agent``. An import inside a function or a ``try`` at module
    scope is the lazy form and is fine."""
    roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import: this package
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_no_module_imports_anything_outside_the_standard_library():
    offenders: dict[str, set[str]] = {}
    modules = sorted(SRC.glob("*.py"))
    assert len(modules) >= 6, "the SDK lost modules, or this test lost its tree"

    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        outside = _module_scope_imports(tree) - ALLOWED_ROOTS
        # ``from __future__`` is not a package.
        outside.discard("__future__")
        if outside:
            offenders[path.name] = outside

    assert offenders == {}, (
        f"these modules import third-party packages at module scope: "
        f"{offenders}. The SDK's core is stdlib-only — an agent image "
        f"installs it and the author's own file and nothing else, so this "
        f"breaks `import librerun_agent` there, and `docker logs` is empty "
        f"by design when it does. Import inside the function that needs it."
    )


def test_the_check_would_notice_a_module_scope_dependency():
    """The negative test: a module that DOES import a third-party package
    at module scope must be caught, or the assertion above is one that
    passes by not looking."""
    tree = ast.parse("import httpx\nfrom pydantic import BaseModel\n")

    assert _module_scope_imports(tree) - ALLOWED_ROOTS == {"httpx", "pydantic"}

    # …and the lazy forms are not flagged.
    lazy = ast.parse(
        "def f():\n    import httpx\n    return httpx\n"
        "try:\n    import uvicorn\nexcept ImportError:\n    uvicorn = None\n"
    )
    assert _module_scope_imports(lazy) - ALLOWED_ROOTS == set()
