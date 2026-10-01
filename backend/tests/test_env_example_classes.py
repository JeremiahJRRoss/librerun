"""K4 — every variable in exactly one class, in exactly one example file.

Decision L29 says `.env` is the seed layer rather than a leftover: the
deployment's declared default under every runtime override. That is only
true if a reader can tell, of any variable, *why* it is in the
environment at all. K4 answers that with four classes, spelled as
machine-readable banner headers in every committed example file:

    # [1] BOOTSTRAP — must be in the environment
    # [2] POSTURE — in the environment by policy
    # [3] RUNTIME DEFAULTS — the value until something overrides it
    # [4] SECRETS — where each one lives

This module holds the tree to two properties, and neither is a list of
what we believe:

* **completeness** — every variable either settings model, `compose.yaml`,
  `agents.compose.yaml` or `config/*.yaml` reads is declared in some
  example file. A variable nobody documents is the "setting nothing
  delivers" defect from the other side: an operator sets it and waits.
* **uniqueness** — it is declared in exactly ONE file, under exactly ONE
  class. Two homes is how `DATADOG_API_KEY` came to sit in both
  `.env.example` and `observability.env.example` before this batch, one
  of which reached Vector and one of which reached nothing.

Both halves are derived. The census comes from the settings models and
from the compose and Vector/collector configs; the classification comes
from the example files themselves. Nothing here is maintained by hand
except the four class titles, and a typo in one of those is a failure
rather than a silent new class.

`backend/tests/test_secret_partition.py` (Gate P) is the neighbouring
rule and answers a different question: it asks who RECEIVES a secret.
This one asks where a variable is WRITTEN DOWN. A value can pass one and
fail the other — before K4, the Cribl HEC token passed Gate P (Vector
reads it) and failed this one (it was declared in `.env.example`, which
Vector never loads, and in no file Vector does).
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

# The gateway's settings model is a sibling service, not an installed
# package. APPENDED rather than inserted, for the reason
# `test_secret_partition.py` spells out at length: that directory also
# holds a `tests/` package, and putting it first would let it answer for
# this suite's `from tests.X import ...`.
_GATEWAY_SRC = REPO / "services" / "gateway"
if str(_GATEWAY_SRC) not in sys.path:
    sys.path.append(str(_GATEWAY_SRC))


# --------------------------------------------------------------------------
# The four classes
# --------------------------------------------------------------------------
#
# Pinned by title, not only by number. A header with the right number and
# a drifted title would otherwise open a block that looks classified and
# means something else; here it fails and names itself.
CLASS_TITLES = {
    1: "BOOTSTRAP — must be in the environment",
    2: "POSTURE — in the environment by policy",
    3: "RUNTIME DEFAULTS — the value until something overrides it",
    4: "SECRETS — where each one lives",
}

# A banner line. Deliberately loose about leading whitespace so that a
# near-miss (`#  [1] …`) is still SEEN and then rejected by its title,
# rather than quietly read as prose while the block below it inherits
# whatever class came before. That is why the prose in every example
# writes "Class 1" instead of "[1]".
HEADER = re.compile(r"^#\s*\[([1-4])\]\s+(.*\S)\s*$")

# A declaration, commented or not. The same shape Gate P uses: an example
# ships most of its lines commented out, and a commented declaration
# still says where the value goes, which is the whole job of an example.
DECLARATION = re.compile(r"^\s*(#?)\s*([A-Z][A-Z0-9_]*)\s*=(.*)$")

# A documented FAMILY: one line standing for a set of variables whose
# names are not the chassis's to know — `LIBRERUN_AGENT_KEY_<ID>`, one
# per agent id (D10), and `<AGENT>_AGENT_URL`, which an agent's own
# manifest interpolates into `container.url`. Naming the members would
# put agent ids in a chassis config file, which L13 forbids; naming the
# shape documents them without it.
#
# More than one placeholder is allowed, and not because any family needs
# two: a line like `A_<X>_<Y>=` matches neither this nor the declaration
# pattern if only one is, so it would be read as prose and the family it
# meant to document would go missing without anything saying so.
FAMILY = re.compile(r"^\s*#?\s*((?:[A-Z0-9_]*<[A-Z]+>)+[A-Z0-9_]*)\s*=(.*)$")

# Compose's `${NAME}` / `$NAME`, and the OpenTelemetry Collector's
# `${env:NAME}` — the bridge's configs use that second spelling, and a
# reader that knew only the first reported them undocumented while they
# were documented, and would have reported them documented when they
# were not.
INTERPOLATION = re.compile(r"\$\{?(?:env:)?([A-Z][A-Z0-9_]*)")

COMPOSE_FILES = ("compose.yaml", "agents.compose.yaml")


# --------------------------------------------------------------------------
# Reading an example file
# --------------------------------------------------------------------------


def value_of(rest: str) -> str:
    """The value on a declaration line, minus any trailing ``# comment``.

    A ``#`` only ends the value when whitespace precedes it, because
    ``POSTGRES_PASSWORD=pw#1`` is a password and
    ``LOG_LEVEL=INFO   # DEBUG | INFO`` is a value with a note after it.
    """
    out: list[str] = []
    quote: str | None = None
    for ch in rest:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (not out or out[-1].isspace()):
            break
        else:
            out.append(ch)
    return "".join(out).strip()


class Declaration:
    """One name, where it was found, and what the example shows for it."""

    __slots__ = ("name", "file", "klass", "commented", "value", "line")

    def __init__(self, name, file, klass, commented, value, line):
        self.name, self.file, self.klass = name, file, klass
        self.commented, self.value, self.line = commented, value, line

    @property
    def where(self) -> str:
        """The RUNTIME file an operator writes, not the committed example."""
        return self.file.removesuffix(".example")

    @property
    def shown_default(self) -> str:
        """How this line's value reads in a documentation table."""
        if self.commented:
            return "*(unset)*"
        if self.value == "":
            return "*(blank)*"
        return f"`{self.value}`"

    def __repr__(self):  # pragma: no cover - diagnostics only
        return f"<{self.name} {self.file}:[{self.klass}]:{self.line}>"


def read_example(name: str, text: str):
    """``(declarations, families, headers)`` for one example file.

    A declaration's class is the class of the nearest banner above it;
    ``None`` means no banner came first, which is the unclassified case
    the gate exists to catch. Several banners of the same class are
    allowed — the observability files put a class-[1] endpoint and a
    class-[4] token side by side under each vendor, because a URL and its
    token are configured together and telling them apart is the point.
    """
    declarations: list[Declaration] = []
    families: list[Declaration] = []
    headers: list[tuple[int, str]] = []
    klass: int | None = None
    for number, line in enumerate(text.splitlines(), 1):
        header = HEADER.match(line)
        if header:
            klass = int(header.group(1))
            headers.append((klass, header.group(2)))
            continue
        family = FAMILY.match(line)
        if family:
            families.append(
                Declaration(family.group(1), name, klass, True, value_of(family.group(2)), number)
            )
            continue
        declaration = DECLARATION.match(line)
        if declaration:
            declarations.append(
                Declaration(
                    declaration.group(2),
                    name,
                    klass,
                    bool(declaration.group(1)),
                    value_of(declaration.group(3)),
                    number,
                )
            )
    return declarations, families, headers


def example_names() -> list[str]:
    """Every committed ``*.env.example`` at the repository root.

    Derived rather than listed: the next process that needs its own file
    joins this gate by existing, which is how `observability-traces.env`
    joined it without anybody remembering to.
    """
    return sorted(
        path.name
        for path in REPO.iterdir()
        if path.is_file() and path.name.endswith(".env.example")
    )


def example_texts() -> dict[str, str]:
    return {name: (REPO / name).read_text(encoding="utf-8") for name in example_names()}


def read_all(texts: dict[str, str] | None = None):
    texts = example_texts() if texts is None else texts
    declarations: list[Declaration] = []
    families: list[Declaration] = []
    headers: list[tuple[int, str]] = []
    for name, text in sorted(texts.items()):
        one, family, header = read_example(name, text)
        declarations += one
        families += family
        headers += header
    return declarations, families, headers


def family_pattern(name: str) -> re.Pattern[str]:
    """``LIBRERUN_AGENT_KEY_<ID>`` as a matcher.

    ``[A-Z0-9_]+`` and not ``[A-Z0-9]+``: an agent id is upper-cased with
    every character outside ``[A-Z0-9]`` replaced by ``_``, so
    ``echo-v1`` is ``ECHO_V1`` and a placeholder that stopped at the
    underscore matched none of the real keys while reporting the family
    documented.
    """
    return re.compile("^" + re.sub(r"<[A-Z]+>", "[A-Z0-9_]+", name) + "$")


# --------------------------------------------------------------------------
# What the deployment READS — the census
# --------------------------------------------------------------------------


def settings_names() -> set[str]:
    from app.config import Settings
    from gateway.config import GatewaySettings

    return set(Settings.model_fields) | set(GatewaySettings.model_fields)


def compose_names() -> set[str]:
    found: set[str] = set()
    for name in COMPOSE_FILES:
        found |= set(INTERPOLATION.findall((REPO / name).read_text(encoding="utf-8")))
    return found


def router_config_names() -> set[str]:
    """Vector's and the collector's own configs — the non-Python readers."""
    found: set[str] = set()
    for path in sorted((REPO / "config").glob("*.yaml")):
        found |= set(INTERPOLATION.findall(path.read_text(encoding="utf-8")))
    return found


def census() -> set[str]:
    """Every variable the deployment reads, as one set.

    ``<NAME>_FILE`` folds into ``<NAME>``: K2's file spelling is a
    DELIVERY of a value, not a second setting, and each example
    documents it on the ``# or …_FILE=`` line beside the value it
    carries. Folding is asserted rather than assumed —
    `test_the_file_spellings_really_folded` fails if the fold ever stops
    finding any.
    """
    raw = settings_names() | compose_names() | router_config_names()
    return {name for name in raw if not (name.endswith("_FILE") and name[:-5] in raw)}


def read_anywhere() -> set[str]:
    """The census, widened by what Python reads straight from the process
    environment — used only for the mirror rule (an example that
    documents a variable nothing reads). It is deliberately NOT the
    census: `os.environ` also carries test switches and the OS's own
    names, and requiring a documented line for `XDG_STATE_HOME` would
    make the forward rule absurd.
    """
    found = census()
    pattern = re.compile(r"os\.(?:environ(?:\.get)?\(|environ\[|getenv\()\s*[\"']([A-Z][A-Z0-9_]*)[\"']")
    for root in ("backend", "services", "sdk", "cli", "scripts"):
        for path in (REPO / root).rglob("*.py"):
            found |= set(pattern.findall(path.read_text(encoding="utf-8", errors="ignore")))
    for path in sorted((REPO / "scripts").glob("*.sh")) + [REPO / "compose.sh"]:
        if path.exists():
            found |= set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)", path.read_text(encoding="utf-8")))
    return found


# --------------------------------------------------------------------------
# Placement: name -> {(file, class)}
# --------------------------------------------------------------------------


def placements(texts: dict[str, str] | None = None) -> dict[str, set[tuple[str, int | None]]]:
    declarations, _, _ = read_all(texts)
    out: dict[str, set[tuple[str, int | None]]] = {}
    for declaration in declarations:
        out.setdefault(declaration.name, set()).add((declaration.file, declaration.klass))
    return out


def unclassified(texts: dict[str, str] | None = None) -> list[Declaration]:
    declarations, families, _ = read_all(texts)
    return [item for item in declarations + families if item.klass is None]


def misplaced(texts: dict[str, str] | None = None) -> dict[str, set[tuple[str, int | None]]]:
    return {name: where for name, where in placements(texts).items() if len(where) > 1}


def undocumented(texts: dict[str, str] | None = None) -> list[str]:
    _, families, _ = read_all(texts)
    patterns = [family_pattern(item.name) for item in families]
    known = placements(texts)
    return sorted(
        name
        for name in census()
        if name not in known and not any(pattern.match(name) for pattern in patterns)
    )


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_the_reader_found_the_example_files():
    """A census that came back empty would make every rule below vacuous.

    The floors are measured on the tree K4 shipped, not guessed: four
    example files, 115 declarations, two families. They exist so that a
    renamed file or a broken regex reports itself instead of certifying
    a repository it never opened.
    """
    names = example_names()
    declarations, families, headers = read_all()

    assert ".env.example" in names, f"the root example is not among {names}"
    assert len(names) >= 4, f"only {names} — an example file has gone missing"
    assert len(declarations) >= 100, (
        f"only {len(declarations)} declarations parsed from {names}; the reader "
        f"is broken, not the tree"
    )
    assert len(families) >= 2, f"only {len(families)} documented families parsed"
    assert len(headers) >= 8, f"only {len(headers)} class banners found across {names}"


def test_every_class_banner_is_one_of_the_four():
    """A fifth class, or a drifted title, is a failure and not a feature."""
    _, _, headers = read_all()
    wrong = [
        (number, title)
        for number, title in headers
        if CLASS_TITLES.get(number) != title
    ]

    assert wrong == [], (
        "these banner lines are not one of the four classes — a class is a "
        "contract, and a near-miss opens a block that looks classified and "
        f"is not:\n" + "\n".join(f"  [{n}] {t!r}" for n, t in wrong)
    )


def test_all_four_classes_are_present():
    _, _, headers = read_all()
    assert {number for number, _ in headers} == set(CLASS_TITLES), (
        "one of the four classes is used nowhere; either it was emptied or a "
        "banner was renamed"
    )


def test_every_declaration_sits_under_a_class():
    """The batch's headline, and its named negative probe's target."""
    stray = unclassified()

    assert stray == [], (
        "these lines declare a variable before any class banner, so nothing "
        "says why the deployment carries them:\n"
        + "\n".join(f"  {item.file}:{item.line}  {item.name}" for item in stray)
    )


def test_no_variable_is_declared_in_two_places():
    """One variable, one file, one class.

    Before K4 `DATADOG_API_KEY` was declared in `.env.example` and in
    `observability.env.example`. Only the second one reached Vector; the
    first read as configuration and delivered nothing.
    """
    doubled = misplaced()

    assert doubled == {}, (
        "these variables are declared in more than one place — one of the "
        "copies is reaching a process and the rest are not:\n"
        + "\n".join(f"  {name}: {sorted(where)}" for name, where in doubled.items())
    )


def test_every_variable_the_deployment_reads_is_documented():
    """The completeness half: a variable in no example fails."""
    missing = undocumented()

    assert missing == [], (
        "these variables are read by a settings model, by compose or by a "
        "router config and are declared in no example file — an operator "
        "cannot set what nothing documents:\n" + "\n".join(f"  {name}" for name in missing)
    )


def test_every_documented_variable_is_read_by_something():
    """The mirror: an example that documents a dead name is the same
    defect seen from the other end — "configured" and delivering nothing.
    """
    readable = read_anywhere()
    known = placements()
    dead = sorted(name for name in known if name not in readable)

    assert dead == [], (
        "these variables are declared in an example file and read by nothing "
        "in the tree:\n" + "\n".join(f"  {name}" for name in dead)
    )


def test_every_documented_family_stands_for_real_variables():
    """A family line that matches nothing is a wildcard covering for a
    gap: it would silence the completeness rule without documenting
    anything."""
    _, families, _ = read_all()
    names = census()
    empty = [
        item.name
        for item in families
        if not any(family_pattern(item.name).match(name) for name in names)
    ]

    assert empty == [], (
        f"these family lines match no variable the deployment reads: {empty}. "
        f"A pattern that matches nothing is an exemption with better manners."
    )


def test_no_family_overlaps_a_declaration_or_another_family():
    """Two patterns matching one name would make its class ambiguous, and
    a pattern that swallowed an explicitly declared name would hide a
    real misplacement."""
    _, families, _ = read_all()
    known = placements()
    problems = []
    for name in sorted(census()):
        matched = [item.name for item in families if family_pattern(item.name).match(name)]
        if len(matched) > 1:
            problems.append(f"{name} matches {matched}")
        if matched and name in known:
            problems.append(f"{name} is both declared and covered by {matched}")

    assert problems == [], "\n".join(problems)


def test_the_file_spellings_really_folded():
    """`census()` folds `<NAME>_FILE` into `<NAME>`. If the models ever
    stop declaring the file spellings the fold becomes a no-op, and a
    rule that no longer does anything should say so rather than pass."""
    raw = settings_names() | compose_names() | router_config_names()
    folded = {name for name in raw if name.endswith("_FILE") and name[:-5] in raw}

    assert len(folded) >= 8, (
        f"only {sorted(folded)} folded — K2's `_FILE` spellings are the "
        f"reason this fold exists, and it is no longer finding them"
    )
    assert not (folded & census())


def test_the_census_draws_on_each_of_its_sources():
    """Three readers, three derivations. One that silently returned an
    empty set would narrow the completeness rule without failing."""
    assert len(settings_names()) >= 60, "the settings models contributed almost nothing"
    assert len(compose_names()) >= 30, "compose contributed almost nothing"
    assert len(router_config_names()) >= 15, "the router configs contributed almost nothing"
    assert len(census()) >= 100, "the census is too small to be the whole deployment"


def test_the_root_example_carries_no_provider_key():
    """L28's headline, restated as a placement rule: the provider keys are
    class [4] and their file is `gateway.env`. Gate P asserts the same
    thing about DELIVERY; this asserts it about the documentation, which
    is what an operator copies."""
    known = placements()
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_AI_API_KEY"):
        assert known.get(name) == {("gateway.env.example", 4)}, (
            f"{name} is documented at {known.get(name)}; it belongs in "
            f"gateway.env.example under class [4] and nowhere else"
        )


def test_the_observability_tokens_left_the_root_file():
    """K4's other move, stated by name so a regression says which token."""
    known = placements()
    log_leg = ("DATADOG_API_KEY", "CRIBL_HEC_TOKEN", "SPLUNK_HEC_TOKEN", "ELASTIC_API_KEY")
    trace_leg = ("CRIBL_OTLP_TOKEN", "SPLUNK_ACCESS_TOKEN", "ELASTIC_APM_API_KEY")
    for name in log_leg:
        assert known.get(name) == {("observability.env.example", 4)}, (
            f"{name} is documented at {known.get(name)}; `vector` is the only "
            f"process that reads it, so observability.env is the only file it "
            f"belongs in"
        )
    for name in trace_leg:
        assert known.get(name) == {("observability-traces.env.example", 4)}, (
            f"{name} is documented at {known.get(name)}; `otel-bridge` is the "
            f"only process that reads it"
        )


# --------------------------------------------------------------------------
# A value in `.env` reaches the CONTAINERS too
# --------------------------------------------------------------------------
#
# Not a class rule, and it lives here because this is the module that
# already reads `.env.example` against `compose.yaml`. `.env` is read by
# compose as well as by a local uvicorn, so an uncommented line here
# outranks compose's own `${NAME:-default}` inside every container. For
# most names that is exactly right — a port, a volume, a password is the
# operator's to set once for both.
#
# It is wrong for one shape: a service address. Compose's default names a
# SERVICE (`http://backend:8000`) because that is what another container
# can route to; the dev-mode value names `localhost`, which from inside a
# container is the container itself. One file cannot carry both, so it
# must carry neither — commented, each side keeps its own default.
#
# Measured, not theorised: `hand-configured-boot` booted a stack from a
# `cp .env.example .env` and `/api/v1/meta` came back
# `"gateway": "unreachable"`, because `LIBRERUN_GATEWAY_URL` was set here
# to the localhost URL and the backend container followed it to itself.
# `LIBRERUN_PUBLIC_URL` had the same defect and had had it for longer;
# `compose.yaml`'s own comment states the hazard — "an agent following a
# localhost URL would call ITSELF, not the chassis" — while the file
# beside it handed out exactly that. Nothing caught it because the only
# path CI ever booted was `scripts/demo.sh`, which writes neither name.

SERVICE_DEFAULT = re.compile(r"^\s+([A-Z][A-Z0-9_]*):\s*\$\{\1:?-([^}]*)\}\s*$", re.M)
LOOPBACK = re.compile(r"\b(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)\b")


def compose_service_defaults() -> dict[str, set[str]]:
    """`{NAME: {default, ...}}` for every `NAME: ${NAME:-default}` line
    in a compose `environment:` block — what a container gets when `.env`
    is silent."""
    out: dict[str, set[str]] = {}
    for file in COMPOSE_FILES:
        text = (REPO / file).read_text(encoding="utf-8")
        for name, default in SERVICE_DEFAULT.findall(text):
            out.setdefault(name, set()).add(default)
    return out


def loopback_overrides(texts: dict[str, str] | None = None) -> list[str]:
    """Names `.env.example` sets to a loopback address while compose
    defaults them to something else — which is a container pointed at
    itself."""
    declarations, _, _ = read_all(texts)
    defaults = compose_service_defaults()
    bad = []
    for item in declarations:
        if item.file != ".env.example" or item.commented or not item.value:
            continue
        theirs = defaults.get(item.name)
        if theirs and item.value not in theirs and LOOPBACK.search(item.value):
            bad.append(
                f"{item.name}={item.value} would override compose's {sorted(theirs)} "
                f"inside every container, where that address is the container itself"
            )
    return bad


def test_no_uncommented_line_points_a_container_at_itself():
    problems = loopback_overrides()

    assert problems == [], (
        "`.env` reaches the containers, so these lines break the deployment "
        "they are supposed to configure — comment them and let each side keep "
        "its own default:\n" + "\n".join(f"  {problem}" for problem in problems)
    )


def test_the_compose_default_reader_found_the_lines_it_needs():
    """A reader that matched nothing would make the rule above vacuous."""
    defaults = compose_service_defaults()

    assert len(defaults) >= 20, f"only {len(defaults)} defaulted lines parsed from compose"
    assert defaults.get("LIBRERUN_PUBLIC_URL") == {"http://backend:8000"}, defaults.get(
        "LIBRERUN_PUBLIC_URL"
    )
    assert defaults.get("LIBRERUN_GATEWAY_URL") == {"http://gateway:8090"}, defaults.get(
        "LIBRERUN_GATEWAY_URL"
    )


def test_a_loopback_override_turns_the_gate_red():
    """The probe is the defect as it really shipped."""
    doctored = example_texts()
    doctored[".env.example"] = doctored[".env.example"].replace(
        "# LIBRERUN_GATEWAY_URL=http://localhost:8090",
        "LIBRERUN_GATEWAY_URL=http://localhost:8090",
    )

    problems = loopback_overrides(doctored)
    assert any("LIBRERUN_GATEWAY_URL" in problem for problem in problems), (
        "the line that really did point the backend container at itself was "
        "not reported"
    )
    # …and a value that AGREES with compose is not a false alarm: the
    # rule is about a loopback address overriding a service name, not
    # about every line compose also defaults.
    agreeing = example_texts()
    agreeing[".env.example"] += "\nLIBRERUN_PUBLIC_URL=http://backend:8000\n"
    assert not any("LIBRERUN_PUBLIC_URL" in problem for problem in loopback_overrides(agreeing))


# The other side of a loopback line: the URL a browser is sent to. T1 put
# `.env.example`'s two ports on loopback (`127.0.0.1:3000`), and
# `scripts/demo.sh` prints the web UI's address — and derives the API's —
# from those bindings. The backend's CORS list is exact, so a page opened
# at `http://127.0.0.1:3000` loads and every API call it makes is refused
# by the example's `APP_CORS_ORIGINS=http://localhost:3000`. A loopback
# binding is named `localhost` wherever a URL is made from it (the CLI's
# `_stack` names it so too, `test_librerun_cli.py`).

DEMO = REPO / "scripts" / "demo.sh"


def demo_url_host(binding: str) -> str:
    """`scripts/demo.sh`'s own `bind_host`, run on one binding."""
    function = re.search(r"^bind_host\(\) \{\n.*?^\}\n", DEMO.read_text(encoding="utf-8"), re.M | re.S)
    assert function, "scripts/demo.sh defines no bind_host()"
    done = subprocess.run(
        ["bash", "-c", function.group(0) + 'bind_host "$1"', "bind_host", binding],
        capture_output=True, text=True, check=True,
    )
    return done.stdout


def test_the_ui_origin_demo_prints_is_the_one_cors_allows():
    lines = {
        item.name: item.value
        for item in read_all()[0]
        if item.file == ".env.example" and not item.commented
    }
    frontend, backend = lines["FRONTEND_PORT"], lines["BACKEND_PORT"]
    assert frontend.startswith("127.0.0.1:") and backend.startswith("127.0.0.1:"), (frontend, backend)

    origin = f"http://{demo_url_host(frontend)}:{frontend.rsplit(':', 1)[1]}"
    assert origin in lines["APP_CORS_ORIGINS"].split(","), (
        f"demo.sh prints {origin} for FRONTEND_PORT={frontend}, which "
        f"APP_CORS_ORIGINS={lines['APP_CORS_ORIGINS']} does not allow"
    )
    api = f"http://{demo_url_host(backend)}:{backend.rsplit(':', 1)[1]}/api/v1"
    assert api == lines["NEXT_PUBLIC_API_URL"], (api, lines["NEXT_PUBLIC_API_URL"])
    # …and it is the real function, still telling addresses apart: a LAN
    # binding is where the stack is reached, so it stays in the URL.
    assert demo_url_host("192.0.2.10:3000") == "192.0.2.10"
    assert demo_url_host("[::1]:3000") == demo_url_host("0.0.0.0:3000") == demo_url_host("3000") == "localhost"


# --------------------------------------------------------------------------
# docs/platform/Install.md — the tables and the files agree line for line
# --------------------------------------------------------------------------

INSTALL = REPO / "docs" / "platform" / "Install.md"
TABLE = "<!-- env-class-table:{number}:{edge} -->"
ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|(.*)\|\s*$")


def install_table(number: int, text: str | None = None) -> dict[str, tuple[str, str]]:
    """``{VARIABLE: (where, default)}`` from one marked table."""
    text = INSTALL.read_text(encoding="utf-8") if text is None else text
    start = TABLE.format(number=number, edge="start")
    end = TABLE.format(number=number, edge="end")
    assert start in text and end in text, (
        f"docs/platform/Install.md no longer carries the class-[{number}] table between "
        f"its markers — this check has nothing to compare and must not pass"
    )
    body = text.split(start, 1)[1].split(end, 1)[0]
    rows: dict[str, tuple[str, str]] = {}
    for line in body.splitlines():
        match = ROW.match(line.strip())
        if match:
            rows[match.group(1)] = (match.group(2), match.group(3))
    return rows


def documented_by_class(texts: dict[str, str] | None = None) -> dict[int, dict[str, Declaration]]:
    declarations, families, _ = read_all(texts)
    out: dict[int, dict[str, Declaration]] = {number: {} for number in CLASS_TITLES}
    for item in declarations + families:
        if item.klass in out:
            out[item.klass][item.name] = item
    return out


@pytest.mark.parametrize("number", sorted(CLASS_TITLES))
def test_the_install_table_lists_exactly_this_class(number):
    """The Accept item: the tables and the files agree line for line.

    Not "the table is a reasonable selection" — a reference that quietly
    omits a variable is where the next "I set it and nothing happened"
    comes from.
    """
    table = set(install_table(number))
    files = set(documented_by_class()[number])

    assert table == files, (
        f"docs/platform/Install.md's class-[{number}] table and the example files "
        f"disagree.\n  only in the document: {sorted(table - files)}\n"
        f"  only in the files:     {sorted(files - table)}"
    )


@pytest.mark.parametrize("number", sorted(CLASS_TITLES))
def test_the_install_table_names_the_file_and_the_default(number):
    """Each row's Where cell names the file that really declares it, and
    each Default cell is the value the example really shows."""
    table = install_table(number)
    files = documented_by_class()[number]
    wrong = []
    for name, (where, default) in sorted(table.items()):
        declaration = files.get(name)
        if declaration is None:
            # A row for a variable this class does not declare. That is
            # the other test's finding, reported there by name; raising
            # a KeyError here would replace its message with a traceback.
            continue
        if where != declaration.where:
            wrong.append(f"{name}: document says {where}, declared in {declaration.where}")
        if default != declaration.shown_default:
            wrong.append(
                f"{name}: document says {default}, the example shows {declaration.shown_default}"
            )

    assert wrong == [], "\n".join(wrong)


# --------------------------------------------------------------------------
# Negative probes — the gate driven against the violations it exists for
# --------------------------------------------------------------------------
#
# On a clean tree a working checker and a broken one are indistinguishable
# (CLAUDE.md). Each probe below starts from the REAL text of the real
# files and injects one violation, so what is proven red is the rule as
# it actually runs and not a fixture that resembles it.


def _texts_with(line: str, into: str = ".env.example", at_end: bool = True) -> dict[str, str]:
    texts = example_texts()
    texts[into] = texts[into] + "\n" + line + "\n" if at_end else line + "\n" + texts[into]
    return texts


def test_an_unclassified_line_turns_the_gate_red():
    """The probe the blueprint names: a declaration above every banner."""
    doctored = _texts_with("PROBE_UNCLASSIFIED=1", at_end=False)

    stray = unclassified(doctored)
    assert [item.name for item in stray] == ["PROBE_UNCLASSIFIED"], (
        "a declaration placed before every class banner was not reported — "
        "the gate passes because nothing above it told it where the classes "
        "begin, which is the failure it exists to catch"
    )
    # …and the real tree is still clean, so the probe proved the rule
    # rather than the tree's current state.
    assert unclassified() == []


def test_a_variable_declared_twice_turns_the_gate_red():
    doctored = example_texts()
    doctored["observability.env.example"] += "\n# APP_SECRET_KEY=\n"

    doubled = misplaced(doctored)
    assert "APP_SECRET_KEY" in doubled, (
        "the session secret declared in a second example file was not "
        "reported; one variable in two files is exactly how a value ends up "
        "reaching one process and not the other"
    )
    assert misplaced() == {}


def test_a_variable_moved_into_a_second_class_turns_the_gate_red():
    """Same file, second class — the subtler half of uniqueness."""
    doctored = example_texts()
    doctored[".env.example"] = doctored[".env.example"].replace(
        "LOG_STDERR_ENABLED=true",
        "LOG_STDERR_ENABLED=true\n\n# [3] RUNTIME DEFAULTS — the value until something overrides it\nLOG_LEVEL=INFO",
    )

    doubled = misplaced(doctored)
    assert "LOG_LEVEL" in doubled, (
        "a variable declared under two different class banners in ONE file "
        "was not reported — the rule is one class, not one file"
    )


def test_a_variable_documented_nowhere_turns_the_gate_red():
    """The "setting nothing delivers" half, injected by deletion."""
    doctored = example_texts()
    doctored[".env.example"] = doctored[".env.example"].replace(
        "PLATFORM_TENANT_SLUG=dev", "# (deleted by the probe)"
    )

    assert "PLATFORM_TENANT_SLUG" in undocumented(doctored), (
        "a backend setting removed from every example was still reported "
        "documented — the completeness half is not reading the models"
    )
    assert undocumented() == []


def test_a_drifted_class_title_turns_the_gate_red():
    doctored = example_texts()
    doctored[".env.example"] = doctored[".env.example"].replace(
        "# [2] POSTURE — in the environment by policy",
        "# [2] POSTURE - in the environment by policy",
    )

    _, _, headers = read_all(doctored)
    wrong = [(n, t) for n, t in headers if CLASS_TITLES.get(n) != t]
    assert wrong, (
        "a banner whose title drifted (an ASCII hyphen for the em dash) was "
        "accepted — the titles are pinned so that a near-miss cannot open a "
        "block that reads as classified"
    )


def test_a_family_that_matches_nothing_turns_the_gate_red():
    doctored = _texts_with("# NOTHING_<HERE>_MATCHES=")

    _, families, _ = read_all(doctored)
    names = census()
    empty = [
        item.name
        for item in families
        if not any(family_pattern(item.name).match(name) for name in names)
    ]
    assert empty == ["NOTHING_<HERE>_MATCHES"], (
        "a documented family standing for no real variable was accepted; a "
        "pattern like that silences the completeness rule for free"
    )


def test_an_over_broad_family_turns_the_gate_red():
    """`<X>_KEY` would swallow half the tree and report it documented."""
    doctored = _texts_with("# LIBRERUN_AGENT_KEY_<ANY><ID>=")
    doctored = {
        name: text.replace("# LIBRERUN_AGENT_KEY_<ANY><ID>=", "# LIBRERUN_AGENT_<ANY>_<ID>=")
        for name, text in doctored.items()
    }

    _, families, _ = read_all(doctored)
    overlaps = [
        name
        for name in census()
        if len([item for item in families if family_pattern(item.name).match(name)]) > 1
    ]
    assert overlaps, (
        "a second family pattern covering the agent keys was accepted — two "
        "patterns over one name make its class ambiguous"
    )


def test_a_disagreeing_install_row_turns_the_gate_red():
    text = INSTALL.read_text(encoding="utf-8")
    start = TABLE.format(number=2, edge="start")
    end = TABLE.format(number=2, edge="end")
    body = text.split(start, 1)[1].split(end, 1)[0]
    assert "| `LOG_LEVEL` |" in body, "the probe's anchor row is gone; re-aim it"
    doctored = text.replace(
        start + body + end,
        start + body.replace("| `LOG_LEVEL` | `.env` | `INFO` |", "| `LOG_LEVEL` | `.env` | `DEBUG` |") + end,
    )

    table = install_table(2, doctored)
    assert table["LOG_LEVEL"][1] == "`DEBUG`"
    files = documented_by_class()[2]
    assert table["LOG_LEVEL"][1] != files["LOG_LEVEL"].shown_default, (
        "a table default edited away from the example's value still matched — "
        "the Default column is decoration, not a check"
    )


def test_an_install_table_missing_a_row_turns_the_gate_red():
    text = INSTALL.read_text(encoding="utf-8")
    doctored = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("| `JWT_EXPIRY_HOURS` |")
    )

    assert "JWT_EXPIRY_HOURS" not in install_table(3, doctored), (
        "the row reader kept a row that was deleted — it is not reading the "
        "document"
    )
    assert "JWT_EXPIRY_HOURS" in documented_by_class()[3]
