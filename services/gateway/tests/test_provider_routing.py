"""Every provider the platform ADVERTISES must be one the egress library
can actually route (blueprint S4a; Codex round 9, P1).

`AgentConfigMeta` offers admins `["openai", "anthropic", "google"]`. The
step resolver built `"<provider>/<model>"` from that word directly, but
LiteLLM reaches Google AI Studio as `gemini/<model>` and rejects
`google/<model>` outright — so every step configured with the advertised
Google option failed at provider resolution, never reaching a model.

What hid it is that the credential half was already right:
`egress._PROVIDER_KEYS` maps both `gemini` and `google` to the Google
key. The duality had been handled once, on the half that does not route.

So the guard is the whole advertised list against the real library, not
an assertion about the string "google": the next provider added to that
list gets checked the same way, by existing.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from gateway.steps import ResolvedStep


def advertised_providers() -> list[tuple[str, str]]:
    """`(source, provider)` for every provider offered to an admin — the
    chassis default and every installed agent's own list."""
    found: list[tuple[str, str]] = []

    protocol = (
        pathlib.Path(__file__).resolve().parents[3]
        / "backend/app/agents/protocol.py"
    ).read_text()
    import re

    match = re.search(
        r'default_factory=lambda: \[([^\]]+)\]', protocol
    )
    if match:
        for raw in match.group(1).split(","):
            name = raw.strip().strip('"\'')
            if name:
                found.append(("chassis default", name))

    agents = pathlib.Path(__file__).resolve().parents[3] / "backend/agents"
    for manifest in sorted(agents.glob("*/agent.yaml")):
        data = yaml.safe_load(manifest.read_text()) or {}
        for step in ((data.get("llm") or {}).get("steps") or []):
            if step.get("provider"):
                found.append((f"{manifest.parent.name}/{step['id']}", step["provider"]))
    return sorted(set(found))


def test_there_are_providers_to_check():
    """A scan over nothing proves nothing."""
    names = {p for _, p in advertised_providers()}
    assert names, "no advertised providers found — the scan is looking in the wrong place"
    assert "google" in names, (
        "the scan no longer sees the google option; if it was removed on "
        "purpose, retire this assertion deliberately rather than letting "
        "the scan quietly cover less"
    )


def _routes(target: str):
    """The provider LiteLLM resolves `target` to, or None."""
    from litellm import get_llm_provider

    try:
        _model, routed, _, _ = get_llm_provider(model=target)
    except Exception:  # noqa: BLE001
        return None
    return routed or None


@pytest.mark.parametrize("source,provider", advertised_providers())
def test_every_advertised_provider_routes(source, provider):
    """The real library is the judge, not a list of names we keep in
    step with it by hand."""
    target = ResolvedStep(step_id="s", provider=provider, model="a-model").target
    routed = _routes(target)

    assert routed is not None, (
        f"{source} advertises provider {provider!r}, which builds the target "
        f"{target!r} that LiteLLM cannot route at all"
    )


@pytest.mark.parametrize("source,provider", advertised_providers())
def test_a_translation_exists_only_where_the_name_does_not_already_work(
    source, provider
):
    """The sharp half, and the one a "does it route?" test misses.

    Routing *somewhere* is not routing *correctly*: mapping ``openai`` to
    ``gemini`` still resolves, and would send every OpenAI step to Google
    while every assertion about routability passed. The rule that has
    teeth is about when a translation may exist at all —

    - a name LiteLLM already routes must NOT be translated, because
      changing it can only send the call somewhere the admin did not
      choose;
    - a name LiteLLM does not route MUST be translated, or the option is
      advertised and broken.

    Neither half reads the mapping to justify the mapping: the library
    decides, for the public name and the final target independently.
    """
    from gateway.steps import _LITELLM_PROVIDER

    key = provider.lower()
    native = _routes(f"{provider}/a-model") is not None
    translated = key in _LITELLM_PROVIDER

    if native:
        assert not translated, (
            f"{source} advertises {provider!r}, which LiteLLM already routes "
            f"natively, yet the platform rewrites it to "
            f"{_LITELLM_PROVIDER[key]!r}. A translation here cannot fix "
            f"anything and can only send the call to the wrong provider."
        )
    else:
        assert translated, (
            f"{source} advertises {provider!r}, which LiteLLM does not route, "
            f"and no translation is declared for it — the option is offered "
            f"to admins and cannot work"
        )
        assert _routes(ResolvedStep(
            step_id="s", provider=provider, model="a-model"
        ).target) is not None


def test_the_public_name_survives_for_the_admin_and_the_span():
    """Translation happens at the routing boundary and nowhere else: the
    step still reports the word the admin chose, so the configuration
    page, the error messages and `gen_ai.system` do not start speaking
    LiteLLM's vocabulary."""
    step = ResolvedStep(step_id="s", provider="google", model="gemini-2.0-flash")

    assert step.provider == "google"
    assert step.target == "gemini/gemini-2.0-flash"
