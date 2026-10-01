"""The ``.env`` file, read the way compose reads it and edited line by line.

Never ``source``d and never evaluated: a value containing ``$(...)``,
backticks or ``;`` is data here, as it is for compose. The reader is a
port of ``scripts/demo.sh``'s — the same precedence (a name in the process
environment wins, even empty; then the file, later lines replacing
earlier ones) and the same interpolation (``${VAR}``, ``$VAR``,
``${VAR:-d}``, ``${VAR-d}``, ``${VAR:+a}``, ``${VAR+a}``, ``$$``, nested
defaults, a substituted value never rescanned).

The writers change exactly the lines they are asked to and leave every
other byte alone, because the file holds an operator's secrets and
comments and a rewrite that reflowed it would be a rewrite nobody
reviewed.
"""
from __future__ import annotations

import os
import re
import secrets
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from ._common import CliError, validate_agent_id

ENV_FILENAME = ".env"

AGENT_KEY_PREFIX = "LIBRERUN_AGENT_KEY_"
AGENT_KEY_PREVIOUS_SUFFIX = "_PREVIOUS"
KEY_VALUE_PREFIX = "lr_agent_"

# ``KEY=value`` or the YAML-style ``KEY: value`` the same parser takes,
# with an optional ``export`` prefix.
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:](.*)$")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def agent_key_variable(agent_id: str) -> str:
    """``my-agent`` -> ``LIBRERUN_AGENT_KEY_MY_AGENT`` (blueprint S4a, D10):
    upper-cased with every character outside ``[A-Z0-9]`` replaced by
    ``_``, because compose variable names admit no hyphens. The same rule
    ``scripts/demo.sh`` and the gateway apply."""
    validate_agent_id(agent_id)
    return AGENT_KEY_PREFIX + re.sub(r"[^A-Z0-9]", "_", agent_id.upper())


def mint_agent_key() -> str:
    """A fresh gateway key: the recognisable prefix and 192 random bits,
    generated the way ``scripts/demo.sh`` generates one."""
    return KEY_VALUE_PREFIX + secrets.token_hex(24)


def interpolate(value: str, lookup: Callable[[str], tuple[bool, str]]) -> str:
    """Compose's interpolation for an unquoted or double-quoted value.

    ``lookup(name)`` answers ``(is_set, value)``. ``${VAR:?e}`` and
    ``${VAR?e}`` read as ``${VAR}`` — compose itself refuses ``up`` on
    those, this reader only needs the value.
    """
    out: list[str] = []
    i, n = 0, len(value)
    while i < n:
        c = value[i]
        if c != "$":
            out.append(c)
            i += 1
            continue
        rest = value[i + 1 :]
        if rest.startswith("$"):
            out.append("$")
            i += 2
            continue
        if rest.startswith("{"):
            depth, j = 0, 0
            while j < len(rest):
                if rest[j] == "{":
                    depth += 1
                elif rest[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth != 0:
                out.append("$")
                i += 1
                continue
            expr = rest[1:j]
            i += j + 2
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(:?[-+?])?(.*)$", expr, re.S)
            if not m:
                out.append("${" + expr + "}")
                continue
            name, op, arg = m.group(1), m.group(2) or "", m.group(3) or ""
            is_set, val = lookup(name)
            if not is_set:
                val = ""
            if op == ":-":
                if not val:
                    val = interpolate(arg, lookup)
            elif op == "-":
                if not is_set:
                    val = interpolate(arg, lookup)
            elif op == ":+":
                val = interpolate(arg, lookup) if val else ""
            elif op == "+":
                val = interpolate(arg, lookup) if is_set else ""
            out.append(val)
            continue
        m = _NAME.match(rest)
        if m:
            is_set, val = lookup(m.group(0))
            out.append(val if is_set else "")
            i += 1 + len(m.group(0))
            continue
        out.append("$")
        i += 1
    return "".join(out)


def parse(text: str, environ: dict | None = None) -> dict[str, str]:
    """The values compose reads from ``text``: each line interpolated
    against the environment accumulated so far — the process environment
    (or ``environ``), then the lines above it — and a later line for the
    same key replacing an earlier one."""
    env = dict(os.environ if environ is None else environ)
    values: dict[str, str] = {}

    def lookup(name: str) -> tuple[bool, str]:
        if name in env:
            return True, env[name]
        if name in values:
            return True, values[name]
        return False, ""

    for raw in text.splitlines():
        line = raw.rstrip("\r").lstrip()
        if not line or line.startswith("#"):
            continue
        m = _ASSIGNMENT.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).lstrip()
        if value.startswith('"'):
            body, _closed = _double_quoted(value)
            value = interpolate(body, lookup)
        elif value.startswith("'"):
            end = value.find("'", 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            value = value.split(" #", 1)[0].rstrip()
            value = interpolate(value, lookup)
        values[key] = value
    return values


def _double_quoted(value: str) -> tuple[str, bool]:
    """The body of a double-quoted value: ``\\"`` and ``\\\\`` unescaped,
    ending at the closing quote (or the end of the line, unclosed)."""
    body: list[str] = []
    i = 1
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value) and value[i + 1] in ('"', "\\"):
            body.append(value[i + 1])
            i += 2
            continue
        if c == '"':
            return "".join(body), True
        body.append(c)
        i += 1
    return "".join(body), False


class DotEnv:
    """A ``.env`` file: its text, its values as compose sees them, and
    line-level edits that leave everything else untouched."""

    def __init__(self, path: Path, environ: dict | None = None):
        self.path = path
        self._environ = dict(os.environ if environ is None else environ)
        self.text = path.read_text(encoding="utf-8") if path.is_file() else None

    @property
    def exists(self) -> bool:
        return self.text is not None

    def values(self) -> dict[str, str]:
        return parse(self.text or "", self._environ)

    def effective(self, name: str) -> str | None:
        """The value compose will use for ``name``: the process environment
        when it carries the name (even empty), else the file, else None."""
        if name in self._environ:
            return self._environ[name]
        return self.values().get(name)

    def is_set(self, name: str) -> bool:
        return name in self._environ or name in self.values()

    def in_environment(self, name: str) -> bool:
        """Whether the PROCESS ENVIRONMENT carries ``name``. Since K3 the
        environment outranks the file for compose and ``compose.sh`` alike,
        so a name set there makes an edit to the file's line inert."""
        return name in self._environ

    def environment_names(self, prefix: str) -> list[str]:
        """The environment's names starting with ``prefix``, sorted."""
        return sorted(name for name in self._environ if name.startswith(prefix))

    def file_has(self, name: str) -> bool:
        """Whether the FILE carries an uncommented assignment of ``name`` —
        the question ``compose.sh`` asks before it placeholders a key."""
        return any(_key_of(line) == name for line in (self.text or "").splitlines())

    # -- edits ---------------------------------------------------------

    def set(self, name: str, value: str) -> None:
        """Replace every uncommented assignment of ``name`` with one line,
        or append one. Other lines are untouched."""
        lines = (self.text or "").split("\n")
        kept: list[str] = []
        replaced = False
        for line in lines:
            if _key_of(line) == name:
                if not replaced:
                    kept.append(f"{name}={value}")
                    replaced = True
                continue
            kept.append(line)
        text = "\n".join(kept)
        if not replaced:
            text = _with_trailing_newline(text) + f"{name}={value}\n"
        self.text = text

    def remove(self, name: str) -> int:
        """Drop every uncommented assignment of ``name``; returns how many."""
        lines = (self.text or "").split("\n")
        kept = [line for line in lines if _key_of(line) != name]
        removed = len(lines) - len(kept)
        self.text = "\n".join(kept)
        return removed

    def append_block(self, lines: list[str]) -> None:
        self.text = _with_trailing_newline(self.text or "") + "\n".join(lines) + "\n"

    def write(self, *, mode: int = 0o600) -> None:
        """Atomically, and owner-only when the file is new: it holds the
        secret and the admin password, and the usual umask would hand it
        to every local account."""
        if self.text is None:
            raise CliError(f"nothing to write to {self.path}")
        existing_mode = None
        if self.path.exists():
            existing_mode = stat.S_IMODE(self.path.stat().st_mode)
        fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self.text)
            os.chmod(tmp, existing_mode if existing_mode is not None else mode)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _key_of(line: str) -> str | None:
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#"):
        return None
    m = _ASSIGNMENT.match(stripped)
    return m.group(1) if m else None


def _with_trailing_newline(text: str) -> str:
    if text and not text.endswith("\n"):
        return text + "\n"
    return text


def bind_port(binding: str) -> str:
    """The port of a compose host binding ``[HOST:]PORT``."""
    return binding.rsplit(":", 1)[-1]


def bind_host(binding: str) -> str:
    """The URL host for that binding: the address when one is given, and
    ``localhost`` for none or a wildcard."""
    if ":" not in binding:
        return "localhost"
    host = binding.rsplit(":", 1)[0]
    return "localhost" if host in ("", "0.0.0.0", "[::]") else host
