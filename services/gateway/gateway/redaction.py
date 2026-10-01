"""Outbound redaction: everything the model will read, before it leaves.

The rule is **total by construction**. Every string in the outbound
request, and every number anywhere in it, is in exactly one of two
classes:

* **rewritable** — redacted in place through the same ``pii_service``
  the intake pipeline runs, so a placeholder reaches the provider
  instead of the value;
* **unrewritable** — checked and never rewritten. If redaction *would*
  change it, the request is refused before anything leaves the box.

There is no third class that is forwarded unexamined, and that is the
part worth being pedantic about: a "protocol field" exemption is exactly
how an address ends up at a provider. So the walk classifies by **path**
and its default for anything it does not recognise is the unrewritable
class — an unanticipated field gets checked, not waved through, and a
future provider field carrying user text fails loudly instead of
silently shipping it.

Why identifiers are checked rather than redacted: a tool name, a tool
call id, a schema property name and a ``pattern`` are all model-visible,
an identifier pattern does not stop a phone number from being a valid
name, and a *rewritten* one would break the tool-call round trip or the
schema. So the request is refused, with the offending field's **path**
named — never its value, or the refusal would leak the thing it exists
to keep in.

Numbers get the S4 walker's number rule. A phone number stored as a JSON
number cannot take a textual placeholder without changing its type, so
it is checked as its decimal text and refuses the request.

Two things the gateway cannot read at all are refused rather than
forwarded while the switch is on: **media parts** (a photographed
address, a spoken name, a scanned document are as model-visible as text,
and the gateway cannot redact media in 1.0) and **token-id embedding
input** (the same text, encoded past the redactor's reach). An agent
that needs either sets ``llm.redact_outbound: false`` — an explicit
opt-out the manifest records and the admin page shows in amber.

The switch governs what the *model* sees. It never governs what
telemetry keeps: the span's content attributes go through the walker
either way (``gateway/telemetry.py``).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from app.services import pii_service

from gateway import errors

# Bounds, so a hostile or broken request cannot walk forever. Deliberately
# the chassis walker's, because this is the same job on a different shape.
MAX_DEPTH = pii_service.WALK_MAX_DEPTH
MAX_NODES = pii_service.WALK_MAX_NODES

# JSON Schema keywords whose values are DATA the model reads, not
# structure: their string leaves are rewritable, recursively.
VALUE_BEARING_KEYWORDS = frozenset({"enum", "const", "examples", "default"})

# The two schema keywords that are prose written for the model.
PROSE_KEYWORDS = frozenset({"description", "title"})

# Message content parts the gateway can read. Anything else is media.
TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})


class _Refusal(Exception):
    """Internal: raised at the offending node, turned into a GatewayError
    by the caller so the path is reported once."""

    def __init__(self, code: str, path: str, what: str) -> None:
        super().__init__(path)
        self.code = code
        self.path = path
        self.what = what


@dataclass
class RedactionReport:
    """What the walk did — for the log line and the tests. Counts and
    paths only; no value ever appears here."""

    redacted_paths: list[str] = field(default_factory=list)
    dropped_paths: list[str] = field(default_factory=list)
    nodes: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.redacted_paths or self.dropped_paths)


def opaque_user(tenant_id: str | None) -> str:
    """What replaces the caller's ``user`` field.

    The top-level ``user`` is provider-side metadata — an abuse-tracking
    handle — rather than model input, so it is not redacted but
    *replaced*: a stable opaque value per tenant, which keeps the
    provider's own rate-limiting useful while carrying nothing about a
    person.
    """
    return hashlib.sha256(f"librerun:{tenant_id or ''}".encode()).hexdigest()[:32]


def _redact_text(value: str) -> str:
    redacted, _ = pii_service.redact(value, quiet=True)
    return redacted


def _check_key(key, index: int, parent: str, keys: tuple = ()) -> None:
    """Check an object key, addressed by ORDINAL.

    The key IS the value here, so naming it in the refusal — which is
    what a normal ``$.parent.key`` path would do — would leak exactly
    what the refusal exists to keep in the box. The chassis walk
    addresses a flagged key as ``<key N>`` for the same reason (S4), and
    this follows it so an operator reading a gateway refusal and a
    chassis refusal is reading one convention.
    """
    if pii_service.check_identifier(str(key), keys, path=parent) is not None:
        raise _Refusal("pii_in_identifier", f"{parent}.<key {index}>", "key")


def _check_unrewritable(value: str, path: str, keys: tuple = ()) -> None:
    """The unrewritable class: checked, never rewritten.

    The rule is the chassis's own identifier rule (S4) — the one that
    already decides object keys, store keys and step ids — rather than
    the full redactor. That is deliberate and it is the difference
    between a gate and a nuisance: the redactor's NER stage reads
    ``json_schema`` and ``librerun`` as person names, so running it over
    protocol strings would refuse ordinary requests while proving
    nothing. The identifier rule catches what an identifier position can
    actually smuggle — an address, an email, a key, a URL, a phone or
    card number hiding in a digit run — without inventing people.
    """
    if pii_service.check_identifier(value, keys, path=path) is not None:
        raise _Refusal("pii_in_identifier", path, "identifier")


def _check_number(value, keys: tuple, path: str) -> None:
    if isinstance(value, bool):
        return
    finding = pii_service.check_number_text(
        format(value, "f").rstrip("0").rstrip(".") if isinstance(value, float) else str(value),
        keys,
        path=path,
    )
    if finding is not None:
        raise _Refusal("pii_in_structured_value", path, "number")


class _Walk:
    """One pass over one request."""

    def __init__(self, *, tenant_id: str | None, embedding: bool) -> None:
        self.tenant_id = tenant_id
        self.embedding = embedding
        self.report = RedactionReport()
        self._budget = MAX_NODES

    # -- helpers ---------------------------------------------------------

    def _spend(self, path: str) -> None:
        self._budget -= 1
        self.report.nodes += 1
        if self._budget < 0:
            raise _Refusal("request_too_large", path, "budget")

    def _descend(self, depth: int, path: str) -> None:
        """The other half of the budget, and for a while only three of
        the ten walkers had it.

        `content`, `structure` and `schema` compared `depth` with
        `MAX_DEPTH`; `message`, `message_content`, `json_arguments`,
        `tool`, `tool_calls`, `response_format` and `embedding_input`
        recursed into themselves without ever looking. So an embeddings
        `input` nested four hundred deep was accepted although the limit
        is sixty-four, and one nested five thousand deep raised
        `RecursionError` inside the redaction walk — neither redacted
        nor refused, just a 500 (Codex P2).

        Written once and called from every walker, because the defect
        was a per-method omission and a per-method fix invites the
        eleventh method to repeat it. `test_walk_depth.py` fails if any
        method taking `depth` does not call this.
        """
        if depth > MAX_DEPTH:
            raise _Refusal("request_too_large", path, "depth")

    def _string(self, value: str, path: str, *, rewritable: bool, keys: tuple = ()) -> str:
        if not rewritable:
            _check_unrewritable(value, path, keys)
            return value
        redacted = _redact_text(value)
        if redacted != value:
            self.report.redacted_paths.append(path)
        return redacted

    # -- generic modes ---------------------------------------------------

    def content(self, node, path: str, keys: tuple, depth: int):
        """Data the model reads: strings rewritable, object keys checked,
        numbers checked."""
        self._spend(path)
        self._descend(depth, path)
        if isinstance(node, str):
            return self._string(node, path, rewritable=True)
        if isinstance(node, bool) or node is None:
            return node
        if isinstance(node, (int, float)):
            _check_number(node, keys, path)
            return node
        if isinstance(node, list):
            return [
                self.content(item, f"{path}[{i}]", keys, depth + 1)
                for i, item in enumerate(node)
            ]
        if isinstance(node, dict):
            out = {}
            for index, (key, value) in enumerate(node.items()):
                # An object key is model-visible on the next turn and
                # cannot be rewritten without changing the structure, so
                # it falls under the refusal rule wherever it sits.
                _check_key(key, index, path, keys)
                out[key] = self.content(value, f"{path}.{key}", keys + (str(key),), depth + 1)
            return out
        # Anything json.dumps could not have produced.
        raise _Refusal("unrewritable_value", path, type(node).__name__)

    def structure(self, node, path: str, keys: tuple, depth: int):
        """Protocol and structure: strings checked, numbers checked. The
        default mode, and deliberately the strict one."""
        self._spend(path)
        self._descend(depth, path)
        if isinstance(node, str):
            return self._string(node, path, rewritable=False, keys=keys)
        if isinstance(node, bool) or node is None:
            return node
        if isinstance(node, (int, float)):
            _check_number(node, keys, path)
            return node
        if isinstance(node, list):
            return [
                self.structure(item, f"{path}[{i}]", keys, depth + 1)
                for i, item in enumerate(node)
            ]
        if isinstance(node, dict):
            out = {}
            for index, (key, value) in enumerate(node.items()):
                _check_key(key, index, path, keys)
                out[key] = self.structure(
                    value, f"{path}.{key}", keys + (str(key),), depth + 1
                )
            return out
        raise _Refusal("unrewritable_value", path, type(node).__name__)

    # -- JSON Schema -----------------------------------------------------

    def schema(self, node, path: str, keys: tuple, depth: int):
        """A JSON Schema the model is shown.

        ``description`` and ``title`` are prose written for the model and
        are rewritable. ``enum``, ``const``, ``examples`` and ``default``
        carry data and switch to content mode, recursively — an agent
        that builds an enum from tenant rows would otherwise ship them.
        Every other keyword is structure the model needs verbatim
        (``pattern``, ``format``, ``$ref``, ``required`` entries, the
        numeric bounds): checked, never rewritten.
        """
        self._spend(path)
        self._descend(depth, path)
        if isinstance(node, list):
            return [
                self.schema(item, f"{path}[{i}]", keys, depth + 1)
                for i, item in enumerate(node)
            ]
        if not isinstance(node, dict):
            return self.structure(node, path, keys, depth)
        out = {}
        for index, (key, value) in enumerate(node.items()):
            child = f"{path}.{key}"
            _check_key(key, index, path, keys)
            child_keys = keys + (str(key),)
            if key in PROSE_KEYWORDS and isinstance(value, str):
                out[key] = self._string(value, child, rewritable=True)
            elif key in VALUE_BEARING_KEYWORDS:
                out[key] = self.content(value, child, child_keys, depth + 1)
            else:
                out[key] = self.schema(value, child, child_keys, depth + 1)
        return out

    # -- the shapes the request actually has ------------------------------

    def json_arguments(self, raw, path: str, depth: int):
        """A tool call's ``arguments``: a JSON string, so it is parsed,
        its values redacted and its keys and numbers checked, then
        re-serialised — the round trip has to stay valid JSON. A string
        that does not parse is redacted as the text it is."""
        self._spend(path)
        self._descend(depth, path)
        if not isinstance(raw, str):
            # Some clients send an object where the spec says string.
            return self.content(raw, path, ("arguments",), depth + 1)
        try:
            parsed = json.loads(raw)
        except ValueError:
            return self._string(raw, path, rewritable=True)
        walked = self.content(parsed, path, ("arguments",), depth + 1)
        if walked == parsed:
            return raw
        self.report.redacted_paths.append(path)
        return json.dumps(walked)

    def message_content(self, value, path: str, depth: int):
        self._descend(depth, path)
        if isinstance(value, str):
            return self._string(value, path, rewritable=True)
        if not isinstance(value, list):
            return self.content(value, path, ("content",), depth + 1)
        parts = []
        for index, part in enumerate(value):
            child = f"{path}[{index}]"
            self._spend(child)
            if isinstance(part, str):
                parts.append(self._string(part, child, rewritable=True))
                continue
            if not isinstance(part, dict):
                raise _Refusal("binary_not_redactable", child, "part")
            kind = str(part.get("type") or "")
            if kind not in TEXT_PART_TYPES:
                raise _Refusal("binary_not_redactable", child, kind or "untyped")
            walked = {}
            for index, (key, sub) in enumerate(part.items()):
                _check_key(key, index, child)
                if key == "text" and isinstance(sub, str):
                    walked[key] = self._string(sub, f"{child}.text", rewritable=True)
                else:
                    walked[key] = self.structure(
                        sub, f"{child}.{key}", ("content", str(key)), depth + 2
                    )
            parts.append(walked)
        return parts

    def message(self, node, path: str, depth: int):
        self._spend(path)
        self._descend(depth, path)
        if not isinstance(node, dict):
            return self.structure(node, path, ("messages",), depth)
        out = {}
        for index, (key, value) in enumerate(node.items()):
            child = f"{path}.{key}"
            _check_key(key, index, path)
            if key == "content":
                out[key] = self.message_content(value, child, depth + 1)
            elif key == "refusal":
                # An assistant turn's refusal text, replayed back as
                # input. It is prose the model reads, exactly like
                # ``content`` — but it fell to ``structure``, which
                # checks a string without rewriting it, and the
                # identifier check deliberately omits person-name NER.
                #
                # Two failures at once, both shown by the same request
                # (Codex P1). A refusal naming a customer went out
                # VERBATIM while the identical name in ``content`` beside
                # it became ``[REDACTED_PERSON_1]``. And a refusal
                # carrying an email did the opposite — tripped the
                # identifier check and 400'd the WHOLE request, because a
                # string the walk may not rewrite can only be refused, so
                # replaying a perfectly ordinary assistant turn was
                # impossible. Reading it as content fixes both: the name
                # is redacted and the email is redacted, and the call
                # goes through.
                out[key] = self.message_content(value, child, depth + 1)
            elif key == "name":
                # A participant label the model sees. Redacted — and
                # DROPPED when redaction changed it, because providers
                # constrain this field to an identifier pattern no
                # placeholder satisfies, so sending the placeholder would
                # fail the request at the provider instead of here.
                if isinstance(value, str) and _redact_text(value) != value:
                    self.report.dropped_paths.append(child)
                    continue
                out[key] = value if isinstance(value, str) else self.structure(
                    value, child, ("name",), depth + 1
                )
            elif key in ("tool_calls", "function_call"):
                out[key] = self.tool_calls(value, child, depth + 1)
            else:
                out[key] = self.structure(value, child, (str(key),), depth + 1)
        return out

    def tool_calls(self, node, path: str, depth: int):
        """``tool_calls`` (a list) and the legacy ``function_call`` (one
        object) have the same two interesting fields."""
        self._spend(path)
        self._descend(depth, path)
        if isinstance(node, list):
            return [
                self.tool_calls(item, f"{path}[{i}]", depth + 1)
                for i, item in enumerate(node)
            ]
        if not isinstance(node, dict):
            return self.structure(node, path, ("tool_calls",), depth)
        out = {}
        for index, (key, value) in enumerate(node.items()):
            child = f"{path}.{key}"
            _check_key(key, index, path)
            if key == "arguments":
                out[key] = self.json_arguments(value, child, depth + 1)
            elif key == "function" and isinstance(value, dict):
                out[key] = self.tool_calls(value, child, depth + 1)
            else:
                out[key] = self.structure(value, child, (str(key),), depth + 1)
        return out

    def tool(self, node, path: str, depth: int):
        self._spend(path)
        self._descend(depth, path)
        if not isinstance(node, dict):
            return self.structure(node, path, ("tools",), depth)
        out = {}
        for index, (key, value) in enumerate(node.items()):
            child = f"{path}.{key}"
            _check_key(key, index, path)
            if key == "function" and isinstance(value, dict):
                out[key] = self.tool(value, child, depth + 1)
            elif key in PROSE_KEYWORDS and isinstance(value, str):
                out[key] = self._string(value, child, rewritable=True)
            elif key in ("parameters", "schema", "input_schema"):
                out[key] = self.schema(value, child, (str(key),), depth + 1)
            else:
                out[key] = self.structure(value, child, (str(key),), depth + 1)
        return out

    def response_format(self, node, path: str, depth: int):
        self._spend(path)
        self._descend(depth, path)
        if not isinstance(node, dict):
            return self.structure(node, path, ("response_format",), depth)
        out = {}
        for index, (key, value) in enumerate(node.items()):
            child = f"{path}.{key}"
            _check_key(key, index, path)
            if key == "json_schema" and isinstance(value, dict):
                out[key] = self.response_format(value, child, depth + 1)
            elif key in PROSE_KEYWORDS and isinstance(value, str):
                out[key] = self._string(value, child, rewritable=True)
            elif key == "schema":
                out[key] = self.schema(value, child, ("schema",), depth + 1)
            else:
                out[key] = self.structure(value, child, (str(key),), depth + 1)
        return out

    def embedding_input(self, node, path: str, depth: int):
        """``input``: a string, or a list of strings. A list of ints — or
        of lists of ints — is the same text encoded past the redactor's
        reach, so it is refused rather than forwarded."""
        self._spend(path)
        self._descend(depth, path)
        if isinstance(node, str):
            return self._string(node, path, rewritable=True)
        if isinstance(node, bool):
            return node
        if isinstance(node, int):
            raise _Refusal("tokens_not_redactable", path, "token id")
        if isinstance(node, list):
            return [
                self.embedding_input(item, f"{path}[{i}]", depth + 1)
                for i, item in enumerate(node)
            ]
        return self.content(node, path, ("input",), depth + 1)

    # -- the entry point --------------------------------------------------

    def request(self, body: dict) -> dict:
        out = {}
        for index, (key, value) in enumerate(body.items()):
            path = f"$.{key}"
            _check_key(key, index, "$")
            if key == "messages":
                out[key] = (
                    [
                        self.message(m, f"{path}[{i}]", 1)
                        for i, m in enumerate(value)
                    ]
                    if isinstance(value, list)
                    else self.structure(value, path, ("messages",), 1)
                )
            elif key in ("tools", "functions"):
                # ``functions`` is the legacy spelling of ``tools`` and
                # still supported by the API, so it carries the same
                # model-visible prose: a description, and the ``name``
                # and ``description`` of every property in its parameter
                # schema. Falling through to ``structure`` checked those
                # with the identifier rule, which omits the NER stage on
                # purpose — so a customer's name in a function
                # description reached the provider while outbound
                # redaction was on. One branch, because two spellings of
                # one thing must not be two code paths (Codex P1).
                out[key] = (
                    [self.tool(t, f"{path}[{i}]", 1) for i, t in enumerate(value)]
                    if isinstance(value, list)
                    else self.tool(value, path, 1)
                )
            elif key == "response_format":
                out[key] = self.response_format(value, path, 1)
            elif key == "input" and self.embedding:
                out[key] = self.embedding_input(value, path, 1)
            elif key == "librerun":
                # The platform's own request extension. It carries agent
                # content (a keyless fixture), so it is rewritable like
                # any other content the model would read — and it is
                # walked rather than exempted, because "the platform put
                # it there" is not a reason to stop looking.
                out[key] = self.content(value, path, ("librerun",), 1)
            elif key == "prediction":
                # Predicted Outputs: ``{"type": "content", "content": …}``
                # where ``content`` is the text the caller expects back,
                # which the model reads. Left to the structural default
                # it would be checked with the identifier rule and never
                # rewritten — the same shape as the ``functions`` gap,
                # and it is reachable because the egress allowlist admits
                # this field. Content, walked like content.
                out[key] = self.content(value, path, ("prediction",), 1)
            elif key == "metadata":
                # Free text the caller attaches for its own bookkeeping.
                # It is not read by the model, but it IS forwarded to the
                # provider, and it landed in the structural default,
                # where a string is checked with the identifier rule and
                # never rewritten — so a customer's name in it left the
                # box. Unlike a tool name or a schema key, nothing about
                # the request depends on these values, so they are
                # content and are redacted like content.
                out[key] = self.content(value, path, ("metadata",), 1)
            elif key == "user":
                out[key] = opaque_user(self.tenant_id)
            else:
                out[key] = self.structure(value, path, (str(key),), 1)
        return out


_MESSAGES = {
    "pii_in_identifier": (
        "the request carries personal data at {path}, which the gateway may "
        "not rewrite — a tool or call name, a schema key or a structural "
        "keyword has to reach the model verbatim, so the request is refused "
        "instead. Remove the value from that position."
    ),
    "pii_in_structured_value": (
        "the number at {path} looks like personal data (a phone number, a "
        "card, a national id). A number cannot take a textual placeholder "
        "without changing its type, so the request is refused. Send it as a "
        "string if it is really data the model needs."
    ),
    "binary_not_redactable": (
        "the content part at {path} is not text, and the gateway cannot "
        "redact media in 1.0 — a photographed address or a spoken name is "
        "as model-visible as typed text. Set llm.redact_outbound: false in "
        "the manifest to send media, an opt-out the admin page shows."
    ),
    "tokens_not_redactable": (
        "the embeddings input at {path} is token ids, which encode text the "
        "redactor cannot read. Send the text itself, or set "
        "llm.redact_outbound: false in the manifest."
    ),
    "request_too_large": (
        "the request is too deeply nested or too large to walk at {path}; "
        "nothing is forwarded unexamined."
    ),
    "unrewritable_value": (
        "the value at {path} is not JSON the gateway can classify, so it is "
        "refused rather than forwarded unexamined."
    ),
}


def redact_request(
    body: dict, *, tenant_id: str | None, enabled: bool, embedding: bool = False
) -> tuple[dict, RedactionReport]:
    """Walk one outbound request. Returns the body to send.

    With ``enabled`` false the body is returned unchanged and nothing is
    refused — media parts and token ids included. That is the manifest's
    explicit opt-out, not a bug: an agent that needs multimodal input
    says so, the admin page shows it in amber, and the operator knows.

    The one exception is ``user``, which is replaced either way.
    ``llm.redact_outbound`` is the switch for what the MODEL reads;
    ``user`` is never read by a model. It is provider-side account
    metadata — the handle an abuse system files a request under, and one
    the provider keeps — so an agent passing an end user's email or id
    there would be registering a person with the provider under their own
    name, which is not something the manifest's opt-out is offered for
    and not something an agent can opt into on that person's behalf.
    """
    if not enabled:
        if "user" in body:
            body = {**body, "user": opaque_user(tenant_id)}
        return body, RedactionReport()
    walk = _Walk(tenant_id=tenant_id, embedding=embedding)
    try:
        return walk.request(body), walk.report
    except _Refusal as refusal:
        raise errors.bad_request(
            refusal.code,
            _MESSAGES[refusal.code].format(path=refusal.path),
            param=refusal.path,
        ) from None
