"""The FIRST scenario of an agent is load-bearing, in five places.

Nothing declares a primary sample, so five independent readers take the
alphabetically first file in an agent's `scenarios/` directory:

  * `adapter_kit/run_contract.py::_load_scenario` — what the container
    battery drives when `--scenario` is not given;
  * `scripts/librerun_smoke.py` — the run the keyless smoke submits;
  * `cli/src/librerun/_run.py` — what `librerun run` submits;
  * `.github/workflows/librerun-smoke.yml`, the D7 card sweep — one
    sample per card, each of which must reach `complete` AND produce a
    costed LLM span;
  * `backend/tests/test_librerun_cli.py` — the rendered template's.

So a file added to a scenarios directory silently repoints all five.
That is not hypothetical: this file exists because it happened. The
first draft of the capability scenario was `demo-capabilities.json`,
which sorts before `demo-echo.json` and therefore became the echo
card's sample everywhere. It asked no model, and D7 requires every
card's sample to produce a costed LLM span, so CI failed — in a
different workflow from the one the change was written against, with a
message about spans rather than about ordering.

These cases pin what each of those readers will pick. A new scenario
that displaces one fails here, in a second, rather than in a job that
takes twenty minutes to say something else.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

AGENTS = Path(__file__).resolve().parents[1] / "agents"

#: agent directory -> the scenario stem every "first scenario" reader
#: will pick. Derived from nothing: this is the pin itself, and changing
#: it is the deliberate act of changing what the demo shows.
FIRST_SCENARIO = {
    "vita_v1": "demo-vendor-interop",
    "_examples/echo_container": "demo-echo",
    "_examples/langgraph_triage": "degraded-search",
    "_examples/llamaindex_summarize": "demo-summarize",
    "_examples/vercel_ai_answer_ts": "demo-answer",
}


def _scenario_dirs():
    """Every agent on disk that ships scenarios, derived from the tree."""
    found = {}
    for manifest in sorted(AGENTS.glob("**/agent.yaml")):
        directory = manifest.parent
        scenarios = directory / "scenarios"
        if scenarios.is_dir() and any(scenarios.glob("*.json")):
            found[str(directory.relative_to(AGENTS))] = scenarios
    return found


def test_every_agent_with_scenarios_is_pinned_here():
    """The census, so the pin cannot go quiet by an agent being added."""
    on_disk = set(_scenario_dirs())
    assert on_disk == set(FIRST_SCENARIO), (
        f"agents on disk with scenarios: {sorted(on_disk)}; pinned here: "
        f"{sorted(FIRST_SCENARIO)}. Add the new one, and say which of its "
        f"samples the five 'first scenario' readers should pick."
    )


@pytest.mark.parametrize("agent", sorted(FIRST_SCENARIO))
def test_the_first_scenario_is_the_one_this_file_pins(agent):
    scenarios = _scenario_dirs()[agent]
    first = sorted(scenarios.glob("*.json"))[0]
    assert first.stem == FIRST_SCENARIO[agent], (
        f"{agent}'s first scenario is now {first.stem!r}, not "
        f"{FIRST_SCENARIO[agent]!r}. Five readers take the alphabetically "
        f"first file — the container battery, the keyless smoke, "
        f"`librerun run`, the D7 card sweep and the CLI template test — so "
        f"this changes all of them. If that is intended, change the pin; if "
        f"not, name the file so it sorts after."
    )


@pytest.mark.parametrize("agent", sorted(FIRST_SCENARIO))
def test_every_scenario_is_a_submittable_object(agent):
    """Not only the first: D7 drives one, but a card offers them all."""
    for path in sorted(_scenario_dirs()[agent].glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data.get("name"), str) and data["name"], path
        assert isinstance(data.get("user_inputs"), dict) and data["user_inputs"], path


def test_the_echo_examples_samples_both_ask_the_model():
    """D7's rule, on the agent whose samples can choose not to.

    Every card's sample must produce a costed LLM span. The echo agent
    is the only one whose model call is an INPUT switch rather than a
    code path it always takes, so it is the only one that can ship a
    sample which silently produces none — which is exactly what the
    first draft of `echo-capabilities` did. Both of its samples are held
    to it, so the pin above and this rule cannot disagree.
    """
    scenarios = _scenario_dirs()["_examples/echo_container"]
    files = sorted(scenarios.glob("*.json"))
    assert len(files) == 2, [p.name for p in files]
    for path in files:
        inputs = json.loads(path.read_text(encoding="utf-8"))["user_inputs"]
        assert inputs.get("ask_model"), (
            f"{path.name} asks no model, so its trace carries no costed LLM "
            f"span — and `librerun-smoke.yml`'s D7 sweep fails any card "
            f"whose sample produces none"
        )
