"""Every lock in the tree must not silently drift from the source it came from.

``backend/Dockerfile`` installs from the LOCKS, so the locks are what actually
ships. The sources are only statements of intent. If someone edits an intent
and forgets ``scripts/lock_requirements.sh``, the image keeps installing the
old pins and the edit does nothing — the same shape as the outage this change
set exists to fix, where what the file said and what the image ran had quietly
diverged.

Four pairs, because the two images perform four installs and each one can
drift on its own:

===========================================  ==================================
source                                       lock
===========================================  ==================================
``requirements.txt``                         ``requirements.lock.txt``
``adapters/pyproject.toml`` ``[project]``    ``adapters/requirements.lock.txt``
``adapters/pyproject.toml`` build-system     ``adapters/build-constraints.txt``
``services/gateway/requirements.txt``        ``services/gateway/requirements.lock.txt``
===========================================  ==================================

The gateway's pair is hashed (A3): its image installs the lock with
``--require-hashes`` and nothing else, LiteLLM is pinned to one version with
no extra, and the tests at the end of this file hold all three.

The adapter pairs were added after Codex observed that a guard reading only
the chassis files leaves the adapter free to drift: ``adapter-battery.yml``
installs from the live ``pyproject.toml`` and stays green, while the image
installs the stale lock and then the adapter itself with ``--no-deps``, so a
newly declared runtime dependency is simply absent at runtime. The build pair
is the same argument one layer down — ``[build-system].requires`` is resolved
in an isolated environment that no runtime lock describes.

The check is deliberately offline and resolver-free, and it compares each pair
in BOTH directions:

- every declared requirement must be pinned in the lock, and each pin must
  satisfy the declared specifier — this catches an addition or a re-bound;
- every pin the lock records as a DIRECT requirement must still be declared —
  this catches a removal, which the first direction cannot see. Delete a line
  from a source without regenerating and the package stays in the lock and
  stays installed; a one-directional check just stops looking at it and
  reports success. (Codex P2 on PR #52 caught exactly that hole.)

Provenance comes from each lock itself: uv writes ``# via -r <source>`` under
each direct pin, so no extra bookkeeping is needed. ``build-constraints.txt``
is generated with ``--no-annotate`` (its provenance line would name a
temporary path), so its direct set is taken as "every pin in the file" — true
by construction for a single-source, one-line resolution, and asserted below
rather than assumed.

What this still cannot see: re-resolution. A new top-level requirement that is
already present as a transitive pin, at a version its specifier accepts,
passes without regeneration — correctly, since the lock does honour it, but it
also means this cannot tell whether adding that requirement would have shifted
the resolution of other packages. Only re-running the lock script shows that.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

BACKEND = Path(__file__).resolve().parent.parent
REQUIREMENTS = BACKEND / "requirements.txt"
LOCK = BACKEND / "requirements.lock.txt"
ADAPTER_PYPROJECT = BACKEND / "adapters" / "pyproject.toml"
ADAPTER_LOCK = BACKEND / "adapters" / "requirements.lock.txt"
ADAPTER_BUILD_CONSTRAINTS = BACKEND / "adapters" / "build-constraints.txt"

_PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*==\s*(?P<version>[^\s;#]+)")


def parse_requirements(text: str) -> list[Requirement]:
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        out.append(Requirement(line))
    return out


def adapter_requirements() -> list[Requirement]:
    data = tomllib.loads(ADAPTER_PYPROJECT.read_text())
    return [Requirement(r) for r in data["project"]["dependencies"]]


def adapter_build_requirements() -> list[Requirement]:
    data = tomllib.loads(ADAPTER_PYPROJECT.read_text())
    return [Requirement(r) for r in data["build-system"]["requires"]]


def parse_lock(text: str, direct_marker: str) -> tuple[dict[str, str], set[str]]:
    """``({name: version}, {names the lock records as direct requirements})``.

    ``direct_marker`` is the ``# via`` fragment uv writes for this lock's own
    source. Passing it in rather than hardcoding one keeps the adapter lock —
    whose marker names the pyproject — from reading as "nothing is direct",
    which would silently disable the removal half.
    """
    pins: dict[str, str] = {}
    direct: set[str] = set()
    current: str | None = None

    for raw in text.splitlines():
        if not raw.strip():
            current = None
            continue
        if raw[0].isspace():
            # An indented line is provenance for the pin above it.
            if current and direct_marker in raw:
                direct.add(current)
            continue
        if raw.lstrip().startswith("#"):
            current = None
            continue
        match = _PIN.match(raw.strip())
        if match:
            current = canonicalize_name(match.group("name"))
            pins[current] = match.group("version")
            if direct_marker in raw:  # inline `name==1.0  # via -r ...`
                direct.add(current)
        else:
            current = None
    return pins, direct


def drift(
    requirements: list[Requirement], pins: dict[str, str], direct: set[str]
) -> list[str]:
    """Human-readable reasons the lock does not match the requirements."""
    problems = []
    declared = {canonicalize_name(r.name) for r in requirements}

    for req in requirements:
        name = canonicalize_name(req.name)
        pinned = pins.get(name)
        if pinned is None:
            problems.append(f"{req.name}: declared but absent from the lock")
            continue
        if req.specifier and not req.specifier.contains(Version(pinned), prereleases=True):
            problems.append(
                f"{req.name}: lock pins {pinned}, which does not satisfy '{req.specifier}'"
            )

    for name in sorted(direct - declared):
        problems.append(
            f"{name}: the lock records it as a direct requirement, but it is no "
            f"longer declared — it would still be installed"
        )
    return problems


# --- the three real pairs ---------------------------------------------------


def _chassis() -> list[str]:
    pins, direct = parse_lock(LOCK.read_text(), "-r requirements.txt")
    return drift(parse_requirements(REQUIREMENTS.read_text()), pins, direct)


def _adapter() -> list[str]:
    pins, direct = parse_lock(ADAPTER_LOCK.read_text(), "pyproject.toml")
    return drift(adapter_requirements(), pins, direct)


def _adapter_build() -> list[str]:
    # Generated with --no-annotate, so every pin in it is the resolution of
    # the build-system requires. test_build_constraints_are_a_closed_set
    # below is what keeps that reading honest.
    pins, _ = parse_lock(ADAPTER_BUILD_CONSTRAINTS.read_text(), "")
    return drift(adapter_build_requirements(), pins, set(pins))


def test_chassis_lock_matches_requirements_in_both_directions():
    assert not _chassis(), (
        "requirements.lock.txt is stale — run scripts/lock_requirements.sh:\n  "
        + "\n  ".join(_chassis())
    )


def test_adapter_lock_matches_its_pyproject_in_both_directions():
    assert not _adapter(), (
        "adapters/requirements.lock.txt is stale — run "
        "scripts/lock_requirements.sh:\n  " + "\n  ".join(_adapter())
    )


def test_adapter_build_constraints_match_its_build_system():
    assert not _adapter_build(), (
        "adapters/build-constraints.txt is stale — run "
        "scripts/lock_requirements.sh:\n  " + "\n  ".join(_adapter_build())
    )


def test_the_backend_lock_carries_no_provider_sdk():
    """S4a moved every provider client to the gateway (L23).

    The backend's own requirements are asserted elsewhere; this is the lock,
    which is the file the image actually installs — the two can disagree, and
    that disagreement is exactly what this module exists to catch.
    """
    pins, _ = parse_lock(LOCK.read_text(), "-r requirements.txt")
    for banned in ("anthropic", "openai", "google-genai", "litellm"):
        assert canonicalize_name(banned) not in pins, (
            f"{banned} is pinned in the backend lock — a provider client here "
            f"is one every in-process agent shares"
        )


def test_the_adapter_lock_pins_langgraph():
    """The package the second lock exists for. `-c` could not constrain it."""
    pins, _ = parse_lock(ADAPTER_LOCK.read_text(), "pyproject.toml")
    assert canonicalize_name("langgraph") in pins


def test_provenance_is_actually_recorded():
    """The removal check is only real if a lock marks direct requirements.

    If uv ever stopped writing its `# via` provenance, `direct` would be empty
    and the removal half would pass by having nothing to compare.
    """
    _, direct = parse_lock(LOCK.read_text(), "-r requirements.txt")
    declared = {canonicalize_name(r.name) for r in parse_requirements(REQUIREMENTS.read_text())}
    assert direct, "the chassis lock records no direct requirements — provenance parsing broke"
    assert direct == declared

    _, adapter_direct = parse_lock(ADAPTER_LOCK.read_text(), "pyproject.toml")
    adapter_declared = {canonicalize_name(r.name) for r in adapter_requirements()}
    assert adapter_direct, "the adapter lock records no direct requirements"
    assert adapter_direct == adapter_declared


def test_build_constraints_are_a_closed_set():
    """`_adapter_build` treats every pin as direct because the file is
    generated without annotations. That is only sound while the resolution
    stays a closed set — one requirement, no transitive dependencies. If
    setuptools ever grows one, this fails and the reading must be revisited
    rather than the assertion widened."""
    pins, _ = parse_lock(ADAPTER_BUILD_CONSTRAINTS.read_text(), "")
    declared = {canonicalize_name(r.name) for r in adapter_build_requirements()}
    assert set(pins) == declared


# --- negative tests: inject the drift the guard exists to catch -------------


def test_guard_catches_a_requirement_missing_from_the_lock():
    problems = drift(
        parse_requirements("alembic\nnewpkg>=2\n"), {"alembic": "1.20.0"}, {"alembic"}
    )
    assert len(problems) == 1
    assert "newpkg" in problems[0] and "absent" in problems[0]


def test_guard_catches_a_pin_that_violates_its_specifier():
    problems = drift(parse_requirements("opentelemetry-sdk<2\n"), {"opentelemetry-sdk": "2.1.0"}, {"opentelemetry-sdk"})
    assert len(problems) == 1
    assert "2.1.0" in problems[0] and "does not satisfy" in problems[0]


def test_guard_catches_a_requirement_removed_without_regenerating():
    """The hole Codex found: the package stays locked, and stays installed."""
    problems = drift(
        parse_requirements("alembic\n"),
        {"alembic": "1.20.0", "pinecone": "5.0.0"},
        {"alembic", "pinecone"},
    )
    assert len(problems) == 1
    assert "pinecone" in problems[0] and "no longer declared" in problems[0]


def test_adapter_guard_catches_a_new_pyproject_dependency():
    """The adapter half of the same hole: `adapter-battery.yml` installs from
    the live pyproject and stays green, while the image installs the stale
    lock and then `--no-deps`, so the new dependency is absent at runtime."""
    declared = adapter_requirements() + [Requirement("brand-new-dep>=1")]
    pins, direct = parse_lock(ADAPTER_LOCK.read_text(), "pyproject.toml")
    problems = drift(declared, pins, direct)
    assert len(problems) == 1
    assert "brand-new-dep" in problems[0] and "absent" in problems[0]


def test_adapter_guard_catches_a_removed_pyproject_dependency():
    kept = [r for r in adapter_requirements() if canonicalize_name(r.name) != "langgraph"]
    pins, direct = parse_lock(ADAPTER_LOCK.read_text(), "pyproject.toml")
    problems = drift(kept, pins, direct)
    assert any("langgraph" in p and "no longer declared" in p for p in problems)


def test_build_guard_catches_an_unpinned_build_requirement():
    """A build requirement added to the pyproject without regenerating leaves
    PIP_CONSTRAINT silent about it, and the isolated build resolves it free."""
    declared = adapter_build_requirements() + [Requirement("hatchling>=1")]
    pins, _ = parse_lock(ADAPTER_BUILD_CONSTRAINTS.read_text(), "")
    problems = drift(declared, pins, set(pins))
    assert len(problems) == 1
    assert "hatchling" in problems[0] and "absent" in problems[0]


def test_a_transitive_pin_is_not_mistaken_for_a_removal():
    """Only DIRECT pins are compared back. Every transitive dependency is in
    the lock and in no requirements file; flagging those would make the guard
    useless on its first run."""
    problems = drift(
        parse_requirements("alembic\n"),
        {"alembic": "1.20.0", "certifi": "2026.7.22"},
        {"alembic"},
    )
    assert problems == []


def test_provenance_parsing_handles_both_via_shapes():
    lock = (
        "# header comment\n"
        "alembic==1.20.0\n"
        "    # via -r requirements.txt\n"
        "certifi==2026.7.22\n"
        "    # via\n"
        "    #   httpx\n"
        "    #   httpcore\n"
        "inline==1.0  # via -r requirements.txt\n"
    )
    pins, direct = parse_lock(lock, "-r requirements.txt")
    assert pins == {"alembic": "1.20.0", "certifi": "2026.7.22", "inline": "1.0"}
    assert direct == {"alembic", "inline"}


def test_a_lock_with_the_wrong_marker_records_nothing_as_direct():
    """Why the marker is a parameter and not a constant: read the adapter lock
    with the chassis marker and every pin looks transitive, which would
    disable the removal half without failing anything."""
    _, direct = parse_lock(ADAPTER_LOCK.read_text(), "-r requirements.txt")
    assert direct == set()


@pytest.mark.parametrize(
    "declared, pinned, version",
    [
        ("pydantic[email]", "pydantic", "2.13.5"),
        ("redis[hiredis]", "redis", "5.2.1"),
        ("uvicorn[standard]", "uvicorn", "0.34.0"),
        # The underscore spelling uv writes for a hyphenated distribution.
        ("openinference-instrumentation>=0.1.40,<1", "openinference_instrumentation", "0.1.65"),
    ],
)
def test_names_match_across_extras_and_spelling(declared, pinned, version):
    """A false alarm here would train people to ignore the guard.

    Each version satisfies its own specifier, so the only thing under test is
    name normalization — an extra, or a hyphen written as an underscore.
    """
    name = canonicalize_name(pinned)
    assert drift(parse_requirements(declared + "\n"), {name: version}, {name}) == []


# --- the gateway's lock (A3; LIC-09, C-19 to C-21, C07) ---------------------
#
# The gateway image installs services/gateway/requirements.lock.txt, hashed,
# and nothing else. Its requirements.txt stays the provenance: what the
# gateway is willing to accept, with LiteLLM at one exact version, as a
# library and never its proxy server.

GATEWAY = BACKEND.parent / "services" / "gateway"
GATEWAY_REQUIREMENTS = GATEWAY / "requirements.txt"
GATEWAY_LOCK = GATEWAY / "requirements.lock.txt"
GATEWAY_DOCKERFILE = GATEWAY / "Dockerfile"
_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")
# LiteLLM's proxy server and its Enterprise code ship as their own
# distributions and as extras; the gateway takes neither (C-19, C-20).
_PROXY_OR_ENTERPRISE = re.compile(r"litellm[-_.](?:proxy|enterprise)[\w.-]*", re.IGNORECASE)


def unhashed_pins(text: str) -> list[str]:
    """Each pin in a hashed lock that carries no sha256 hash of its own."""
    entries: list[list] = []
    for raw in text.splitlines():
        if raw and not raw[0].isspace():
            match = _PIN.match(raw.strip())
            if match:
                entries.append([match.group("name"), bool(_HASH.search(raw))])
            continue
        if entries and _HASH.search(raw):
            entries[-1][1] = True
    return [name for name, hashed in entries if not hashed]


def litellm_problems(requirements: list[Requirement], pins: dict[str, str]) -> list[str]:
    """Why the declared LiteLLM is not one exact version of the library alone."""
    declared = [r for r in requirements if canonicalize_name(r.name) == "litellm"]
    if len(declared) != 1:
        return [f"litellm is declared {len(declared)} times, not once"]
    req, problems = declared[0], []
    if req.extras:
        problems.append(f"litellm[{','.join(sorted(req.extras))}]: an extra pulls in the proxy (C-19)")
    specifiers = list(req.specifier)
    if len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
        problems.append(f"litellm{req.specifier or ''}: not one exact version (C-21)")
    elif pins.get("litellm") != specifiers[0].version:
        problems.append(
            f"the lock pins litellm {pins.get('litellm')}, requirements.txt {specifiers[0].version}"
        )
    problems += [
        f"{name}: LiteLLM's proxy or Enterprise distribution is in the lock (C-19, C-20)"
        for name in pins
        if _PROXY_OR_ENTERPRISE.fullmatch(name)
    ]
    return problems


def install_problems(dockerfile: str) -> list[str]:
    """Why the gateway image does not install its hashed lock, and it alone."""
    installs = re.findall(r"pip install[^\n]*", dockerfile)
    problems = []
    if not any("-r requirements.lock.txt" in i and "--require-hashes" in i for i in installs):
        problems.append("no `pip install --require-hashes -r requirements.lock.txt`")
    problems += [f"installs from its ranges: {i.strip()}" for i in installs if "requirements.txt" in i.replace("requirements.lock.txt", "")]
    if not re.search(r"^COPY\s+[^\n]*requirements\.lock\.txt", dockerfile, re.MULTILINE):
        problems.append("the lock is never copied into the build")
    return problems


def _gateway() -> list[str]:
    pins, direct = parse_lock(GATEWAY_LOCK.read_text(), "-r requirements.txt")
    return drift(parse_requirements(GATEWAY_REQUIREMENTS.read_text()), pins, direct)


def test_gateway_lock_matches_its_requirements():
    problems = _gateway()
    assert not problems, (
        "services/gateway/requirements.lock.txt has drifted from its requirements.txt; "
        "run scripts/lock_requirements.sh:\n  " + "\n  ".join(problems)
    )


def test_gateway_pins_litellm_exactly():
    pins, _ = parse_lock(GATEWAY_LOCK.read_text(), "-r requirements.txt")
    problems = litellm_problems(parse_requirements(GATEWAY_REQUIREMENTS.read_text()), pins)
    assert not problems, "\n".join(problems)


def test_every_gateway_pin_has_a_hash():
    text = GATEWAY_LOCK.read_text()
    pins, _ = parse_lock(text, "-r requirements.txt")
    assert pins, "the gateway lock pins nothing — parsing broke"
    assert not unhashed_pins(text), f"pins without a hash: {unhashed_pins(text)}"


def test_gateway_installs_its_lock_alone():
    problems = install_problems(GATEWAY_DOCKERFILE.read_text())
    assert not problems, "services/gateway/Dockerfile:\n  " + "\n  ".join(problems)


def test_litellm_stays_a_library():
    """No extra and no proxy or Enterprise distribution (C-19, C-20); the
    process is uvicorn serving the gateway, never LiteLLM's own server; and
    the cost map is the pinned wheel's, not a start-up fetch."""
    pins, _ = parse_lock(GATEWAY_LOCK.read_text(), "-r requirements.txt")
    assert not litellm_problems(parse_requirements(GATEWAY_REQUIREMENTS.read_text()), pins)
    dockerfile = GATEWAY_DOCKERFILE.read_text()
    cmd = re.findall(r'^CMD\s+\[(.*)\]\s*$', dockerfile, re.MULTILINE)
    assert cmd and cmd[-1].split(",")[0].strip().strip('"') == "uvicorn", cmd
    assert "gateway.main:app" in cmd[-1]
    assert re.search(r"^ENV\s+LITELLM_LOCAL_MODEL_COST_MAP=True\s*$", dockerfile, re.MULTILINE)


# --- the doubles: each guard, fed the violation it exists to catch ------------


def test_guard_catches_an_unhashed_pin():
    lock = (
        "anyio==4.9.0 \\\n    --hash=sha256:" + "a" * 64 + "\n    # via httpx\n"
        "litellm==1.103.1\n    # via -r requirements.txt\n"
    )
    assert unhashed_pins(lock) == ["litellm"]


def test_guard_catches_a_litellm_range():
    for line in ("litellm>=1.55", "litellm~=1.103", "litellm==1.*", "litellm"):
        problems = litellm_problems(parse_requirements(line), {"litellm": "1.103.1"})
        assert any("not one exact version" in p for p in problems), (line, problems)


def test_guard_catches_a_proxy_extra():
    problems = litellm_problems(parse_requirements("litellm[proxy]==1.103.1"), {"litellm": "1.103.1"})
    assert any("an extra pulls in the proxy" in p for p in problems), problems
    problems = litellm_problems(
        parse_requirements("litellm==1.103.1"),
        {"litellm": "1.103.1", "litellm-proxy-extras": "0.2.1", "litellm-enterprise": "0.1.2"},
    )
    assert sum("proxy or Enterprise distribution" in p for p in problems) == 2, problems
