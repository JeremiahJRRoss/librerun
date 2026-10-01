"""``settings[]`` in the manifest: the declaration a tenant's values are
held to (K5a, L32).

A setting is a key, a type and a default. ``AgentSettingSpec.coerce`` is
the one place a value is judged — the admin API, the deprecated protocol
path and the service all call it — so the rules are pinned here, on the
model, and the service's tests pin that they are applied. The rule that
matters most is K5-07's: JSON's booleans are Python's integers (``True ==
1``), so a check that did not look at the type first would store ``1`` in
a boolean setting and call ``true`` an integer.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents.manifest import (
    SETTING_KEY_PATTERN,
    AgentManifest,
    AgentSettingSpec,
    load_manifest,
)

ECHO_DIR = Path(__file__).resolve().parents[1] / "agents" / "_examples" / "echo_container"


def spec(**fields) -> AgentSettingSpec:
    return AgentSettingSpec.model_validate(fields)


def manifest(settings: list[dict]) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "id": "probe-agent",
            "name": "Probe",
            "runtime": "python-package",
            "phases": [{"name": "work"}],
            "output": {"mode": "structured"},
            "settings": settings,
        }
    )


def test_true_is_not_an_int_and_one_is_not_a_bool():
    """K5-07, both ways round."""
    flag = spec(key="strict", type="bool", default=False)
    limit = spec(key="limit", type="int", default=3)

    assert flag.coerce(True) is True and flag.coerce(False) is False
    for not_a_bool in (1, 0, "true", None, 1.0):
        with pytest.raises(ValueError, match="true or false"):
            flag.coerce(not_a_bool)

    assert limit.coerce(7) == 7 and type(limit.coerce(7)) is int
    for not_an_int in (True, False, 7.0, "7", None):
        with pytest.raises(ValueError, match="an integer"):
            limit.coerce(not_an_int)


def test_a_float_setting_takes_an_integer_and_keeps_a_float():
    """A number input sends ``0`` for ``0.0``: the same value, stored as
    the float the setting is. The default is kept as a float too."""
    ratio = spec(key="ratio", type="float", default=0)

    assert ratio.default == 0.0 and type(ratio.default) is float
    assert type(ratio.coerce(0)) is float and ratio.coerce(0) == 0.0
    assert ratio.coerce(2.5) == 2.5
    with pytest.raises(ValueError, match="a number"):
        ratio.coerce(True)
    # JSON has neither, and PostgreSQL's JSONB refuses both.
    for not_finite in (float("nan"), float("inf"), 10**400):
        with pytest.raises(ValueError, match="finite"):
            ratio.coerce(not_finite)


def test_an_enums_options_are_declared_once_and_its_default_is_one_of_them():
    depth = spec(key="depth", type="enum", options=["basic", "advanced"], default="advanced")
    assert depth.coerce("basic") == "basic"
    with pytest.raises(ValueError, match=r"one of \['basic', 'advanced'\]"):
        depth.coerce("deep")
    with pytest.raises(ValueError):
        depth.coerce(1)

    with pytest.raises(ValidationError, match="declares no options"):
        spec(key="depth", type="enum", default="basic")
    with pytest.raises(ValidationError, match="declares no options"):
        spec(key="depth", type="enum", options=[], default="basic")
    with pytest.raises(ValidationError, match="an option twice"):
        spec(key="depth", type="enum", options=["basic", "basic"], default="basic")
    with pytest.raises(ValidationError, match="its default must be one of"):
        spec(key="depth", type="enum", options=["basic", "advanced"], default="deep")


def test_options_belong_to_an_enum_alone():
    with pytest.raises(ValidationError, match="options are for an enum setting"):
        spec(key="note", type="string", options=["a"], default="a")


def test_the_default_is_held_to_the_type():
    """A manifest cannot declare a default its own Settings tab would
    refuse, and a setting cannot go without one."""
    with pytest.raises(ValidationError, match="its default must be true or false"):
        spec(key="strict", type="bool", default=1)
    with pytest.raises(ValidationError, match="its default must be a string, not null"):
        spec(key="note", type="string", default=None)
    with pytest.raises(ValidationError, match="default"):
        AgentSettingSpec.model_validate({"key": "note", "type": "string"})


def test_a_string_list_is_a_list_of_strings():
    tags = spec(key="tags", type="string_list", default=[])
    assert tags.coerce(["a", "b"]) == ["a", "b"]
    assert tags.coerce([]) == []
    with pytest.raises(ValueError, match="item 1 is an integer"):
        tags.coerce(["a", 2])
    with pytest.raises(ValueError, match="list of strings, not a string"):
        tags.coerce("a,b")


def test_a_string_is_a_string_and_nothing_is_parsed_out_of_one():
    note = spec(key="note", type="string", default="")
    assert note.coerce("") == "" and note.coerce("x") == "x"
    for not_a_string in (1, True, None, ["x"], {"x": 1}):
        with pytest.raises(ValueError, match="a string"):
            note.coerce(not_a_string)


def test_a_refusal_names_the_type_and_never_echoes_the_value():
    """What an admin typed is not repeated into an error — the 400's
    detail, the log line a handler might write."""
    note = spec(key="note", type="int", default=1)
    with pytest.raises(ValueError) as refused:
        note.coerce("a value someone typed")
    assert "a value someone typed" not in str(refused.value)
    assert "a string" in str(refused.value)


def test_the_key_pattern_is_public_and_is_the_fields():
    """K8a names tool secrets in this charset, so it is exported — and it
    must be the pattern the field actually enforces, not a copy."""
    assert SETTING_KEY_PATTERN == r"^[a-z][a-z0-9_]*$"
    key_field = AgentSettingSpec.model_fields["key"]
    assert any(getattr(m, "pattern", None) == SETTING_KEY_PATTERN for m in key_field.metadata)
    for good in ("a", "note", "max_retries_per_step", "k" * 64):
        assert spec(key=good, type="string", default="").key == good
    for bad in ("", "Note", "1st", "_hidden", "a-b", "a b", "k" * 65):
        with pytest.raises(ValidationError):
            spec(key=bad, type="string", default="")
        assert not re.fullmatch(SETTING_KEY_PATTERN, bad) or len(bad) > 64


def test_the_label_defaults_to_the_key_and_unknown_fields_are_refused():
    assert spec(key="note", type="string", default="").label == "note"
    assert spec(key="note", label="Note", type="string", default="").label == "Note"
    with pytest.raises(ValidationError, match="extra"):
        spec(key="note", type="string", default="", secret=True)


def test_a_manifest_declares_each_key_once():
    both = [
        {"key": "note", "type": "string", "default": ""},
        {"key": "note", "type": "int", "default": 1},
    ]
    with pytest.raises(ValidationError, match=r"duplicate settings keys: \['note'\]"):
        manifest(both)


def test_a_manifest_that_declares_none_has_none():
    m = manifest([])
    assert m.settings == [] and m.setting("note") is None
    m = manifest([{"key": "note", "type": "string", "default": "hi"}])
    assert m.setting("note").default == "hi"


def test_the_reference_container_declares_its_note():
    """The echo example is the settings[] worked example (K5-10): its
    default is the note its output carried before it had a setting."""
    note = load_manifest(ECHO_DIR).setting("note")
    assert note is not None and note.type == "string"
    assert note.default == "echoed by the Run Contract v1 reference agent"
