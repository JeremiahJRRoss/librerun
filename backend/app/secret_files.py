"""``<NAME>_FILE`` — a secret delivered as a file instead of a variable.

Docker secrets, Podman secrets and Kubernetes' mounted secrets all put a
value in a file and tell the process where it is. Nothing in this tree
needed a wrapper script to read one: the convention is small enough to
live in the settings models themselves, and small enough that both of
them can share it (K blueprint K2, decision L30).

The rule, for a secret field ``X`` (K blueprint §8):

* ``X`` non-blank → use it. The variable still wins, so nothing about a
  deployment that never heard of this file changes.
* ``X`` blank and ``X_FILE`` set → the value is the file's contents with
  **one** trailing newline removed, because ``printf`` writes one and
  every editor adds one, and a session key with a ``\\n`` on the end is
  a key that works until someone rotates it by hand.
* both set and **different** → the process refuses to start, naming the
  variable and never a value. Two sources disagreeing about one secret
  is not something to resolve by precedence: one of them is what the
  operator meant and there is no way to tell which. (Postgres' own image
  refuses the same pair for the same reason, so the behaviour is not a
  surprise in a compose file.)
* both set and equal → fine, and deliberately so: an override that sets
  ``X_FILE`` beside an inherited ``X`` is the normal shape of a compose
  override, and a deployment that arrives at one value twice has no
  ambiguity to report.
* ``X_FILE`` set but unreadable → refused **by path**. A missing mount is
  the failure this convention has, and "your secret is empty" three
  layers downstream is a bad way to learn about it.

Stdlib only, and no import of ``app.config`` — ``app.config`` imports
*this*, and the gateway's settings model imports it too (the gateway
image copies ``backend/app``; ``services/gateway/Dockerfile``). One
helper, two models, no third copy of the rule.

``SecretStr`` is the models' business rather than this module's, but it
reaches here anyway: with ``validate_assignment`` on, a settings model
re-runs its validators on every assignment, so the values handed back
are ``SecretStr`` after the first pass. They are unwrapped by duck
typing (``get_secret_value``) rather than by importing pydantic, so this
module stays stdlib and the resolution stays idempotent — re-resolving
an already-resolved model must not invent a conflict with itself.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# The suffix that names the file a secret is read from. One spelling,
# here, because a test, a document and a settings model all have to
# agree about it.
FILE_SUFFIX = "_FILE"


class SecretSourceConflict(RuntimeError):
    """``X`` and ``X_FILE`` disagree about one secret.

    Raised at settings construction, which is process start. The message
    names the variable; it never carries either value, because this
    exception is printed by whatever is watching the container come up
    (L31).

    ``RuntimeError`` and **not** ``ValueError``, which is the obvious
    choice and the wrong one: pydantic converts a ``ValueError`` raised
    inside a validator into a ``ValidationError``, and a
    ``ValidationError`` from a model-level validator renders the
    validator's whole input — every merged settings value, secrets
    included — as ``input_value=`` in its message. The exception meant
    to protect a secret would print all of them. Anything that is not a
    ``ValueError`` or an ``AssertionError`` propagates out of pydantic
    untouched, which is what these two want: one line, the variable's
    name, no values.
    """


class SecretFileUnreadable(RuntimeError):
    """``X_FILE`` names a path this process cannot read.

    Names the path and the operating system's reason — a path is not a
    secret, and an operator staring at a container that will not start
    needs to know which mount is missing.
    """


def file_variable(name: str) -> str:
    """The companion variable for secret field ``name``."""
    return f"{name}{FILE_SUFFIX}"


def reveal(value: Any) -> str:
    """Whatever a settings source or an earlier validation pass left, as
    a plain string. ``None`` and absent both read as blank.

    Public because a few readers are handed *a* settings object rather
    than *the* one — ``app.demo``'s guard takes a double in tests — and
    have to cope with a ``SecretStr`` or a bare string with equal grace.
    Code that knows it holds the real model calls ``.get_secret_value()``
    instead, so the type stays visible at the use site.
    """
    if value is None:
        return ""
    reveal = getattr(value, "get_secret_value", None)
    if callable(reveal):
        value = reveal()
    return value if isinstance(value, str) else str(value)


def read_secret_file(path: str) -> str:
    """The file's contents, minus one trailing newline.

    Bytes, then a strict UTF-8 decode: a secret file written by another
    tool in another encoding is a configuration error worth failing on,
    not something to paper over with replacement characters that would
    then be used as a key.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        raise SecretFileUnreadable(f"{path}: {reason}") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretFileUnreadable(f"{path}: not valid UTF-8") from None
    # One newline, not every trailing blank: a secret may legitimately
    # end in whitespace, and .rstrip() would silently change it.
    for ending in ("\r\n", "\n", "\r"):
        if text.endswith(ending):
            return text[: -len(ending)]
    return text


def resolve(values: Any, file_backed: Iterable[str]) -> Any:
    """Apply the ``_FILE`` rule to ``values`` and hand it back.

    ``values`` is what a pydantic ``model_validator(mode="before")``
    receives: the merged dict of every settings source, keyed by field
    name. Anything that is not a mapping is returned untouched, so a
    model constructed some other way does not explode here.

    Raises ``SecretSourceConflict`` or ``SecretFileUnreadable``.
    Neither is a ``ValueError``, so pydantic lets them out as they are
    instead of wrapping them in a ``ValidationError`` that would quote
    this very dict back at the operator — see ``SecretSourceConflict``.
    """
    if not isinstance(values, Mapping):
        return values
    resolved = dict(values)
    for name in file_backed:
        path = reveal(resolved.get(file_variable(name))).strip()
        if not path:
            continue
        from_file = read_secret_file(path)
        plain = reveal(resolved.get(name))
        if plain == from_file:
            # Already resolved, or the two sources agree. Returning here
            # rather than assigning is not tidiness: under
            # ``validate_assignment`` pydantic re-runs this validator and
            # then writes back the fields it did NOT validate straight
            # from this dict, so replacing a ``SecretStr`` with the bare
            # string it holds would quietly un-mask the field on the
            # first ``settings.LOG_LEVEL = …`` anywhere in the process.
            continue
        if plain:
            raise SecretSourceConflict(
                f"{name} and {file_variable(name)} disagree. Set one or "
                f"the other — a compose override that adds "
                f"{file_variable(name)} must blank {name}, because an "
                f"inherited value is still a value. (Neither value is "
                f"shown here, and neither should be.)"
            )
        resolved[name] = from_file
    return resolved


def pairing_problems(
    field_names: Iterable[str],
    file_backed: Iterable[str],
    masked_only: Iterable[str] = (),
) -> list[str]:
    """Everything wrong with one model's secret declaration, as English.

    A list a test asserts is empty. It exists because the two halves of
    the declaration — the ``SecretStr`` field and its ``_FILE``
    companion — are written by hand in two places, and a secret whose
    companion was never declared would read ``_FILE`` from nowhere:
    ``extra="ignore"`` means an undeclared ``X_FILE`` in the environment
    is silently discarded, so the feature would be absent rather than
    broken, and absent is the failure nobody notices.
    """
    names = set(field_names)
    file_backed = list(file_backed)
    masked_only = list(masked_only)
    problems: list[str] = []

    for name in file_backed:
        if name not in names:
            problems.append(f"{name} is listed as file-backed but is not a field")
        if file_variable(name) not in names:
            problems.append(
                f"{name} is file-backed but the model declares no "
                f"{file_variable(name)} field, so the variable would be "
                f"ignored rather than read"
            )
    for name in masked_only:
        if name not in names:
            problems.append(f"{name} is listed as masked-only but is not a field")
        if file_variable(name) in names:
            problems.append(
                f"{name} declares {file_variable(name)} but is masked-only; "
                f"either it is file-backed or the companion is a lie"
            )
    overlap = set(file_backed) & set(masked_only)
    if overlap:
        problems.append(
            "listed both as file-backed and as masked-only: "
            + ", ".join(sorted(overlap))
        )
    for name in sorted(names):
        if not name.endswith(FILE_SUFFIX):
            continue
        partner = name[: -len(FILE_SUFFIX)]
        if partner not in file_backed:
            problems.append(
                f"{name} is declared but {partner} is not file-backed, so "
                f"nothing reads it"
            )
    return problems


# Field names that LOOK like a secret. Used by the guard tests on both
# models: a field matching one of these must be declared a secret (and
# so masked) or excused by name, with the reason in the model. The
# alternative — remembering — is what leaves the next credential
# printing itself into a settings dump.
SECRET_SHAPED_SUFFIXES = ("_SECRET", "_PASSWORD", "_API_KEY", "_TOKEN", "_KEY")


def secret_shaped(field_names: Iterable[str]) -> list[str]:
    """The field names that look like a secret, ``_FILE`` companions
    excluded — a path is not a secret."""
    return sorted(
        name
        for name in field_names
        if not name.endswith(FILE_SUFFIX)
        and any(name.endswith(suffix) for suffix in SECRET_SHAPED_SUFFIXES)
    )
