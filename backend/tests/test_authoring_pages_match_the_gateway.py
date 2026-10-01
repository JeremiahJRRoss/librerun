"""The four contract pages say what S4a shipped (blueprint S4b, gap I8).

`docs/authoring/LLM_Gateway.md` is the normative page for the model
path: the gateway holds every provider key, an agent names a declared
step rather than a model, and a container presents two credentials —
its agent key and the invocation's run token. Four other pages are the
doors a newcomer actually comes in by, and ten days after S4a merged
three of them still described the credential model it replaced: the SDK
page called live configuration and model calls "not in this release"
and said an agent "holds its own keys until then"; the Run Contract
said `llm` is "in-process-only".

A page that is merely out of date reads exactly like a page that is
right, which is why this is a test and not a review habit: the reader
it costs is the one who believes it and goes looking for somewhere to
put a provider key.

Two directions, because either alone is half a check:

* **the stale claims are gone** — none of the four pages contains one of
  the phrases the pre-S4a model was written in;
* **the current one is present** — each page names the gateway, and each
  page that tells a container how to call a model names the header the
  run token travels in. A sweep that only forbade the old sentences
  would pass just as well on a page that had deleted them and said
  nothing in their place.

Scope is those four pages by name, not all of `docs/`: the blueprint
and the gap register quote the stale phrases in order to define and
diagnose them, and a rule that forbade quoting a defect would forbid
recording one. Every check below has a negative probe beside it that
injects the violation it exists to catch, because on a clean tree a
checker that never looks and one that works are indistinguishable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The pages this rule governs. `docs/platform/Install.md` is deliberately NOT here:
# its provider-key section belongs to the configuration-and-secrets batch
# K1, which rewrites it in the same window (blueprint S4b).
PAGES = (
    "docs/authoring/SDK.md",
    "docs/authoring/Container_Agents.md",
    "docs/authoring/Run_Contract_v1.md",
    "docs/authoring/Agents_Design.md",
)

# The pre-S4a model, in the words it was actually written in.
STALE_PHRASES = (
    "not in this release",
    "holds its own keys",
    "in-process-only",
)

# What each page must still SAY, so that deleting a stale sentence is not
# a way to pass. `X-LibreRun-Run-Token` is the header a model call cannot
# do without — the agent key beside it is what a client library needs, not
# what the gateway requires — so a page that tells someone how to reach
# the gateway and omits it has described a call that answers `401`.
REQUIRED_PHRASES = {
    "docs/authoring/SDK.md": ("gateway", "ctx.llm.complete", "X-LibreRun-Run-Token"),
    "docs/authoring/Container_Agents.md": ("gateway", "X-LibreRun-Run-Token"),
    "docs/authoring/Run_Contract_v1.md": ("gateway", "X-LibreRun-Run-Token"),
    "docs/authoring/Agents_Design.md": ("gateway",),
}


def _read(root: Path, page: str) -> str:
    """The page's text, or a failure — never a silent skip.

    A page that has been renamed or moved must fail this rule rather
    than pass it by not being read: "the file is gone" is the one way a
    phrase check can report success while looking at nothing.
    """
    path = root / page
    if not path.is_file():
        raise FileNotFoundError(
            f"{page} is not in the tree: this rule governs it by name, so a "
            f"rename must update the rule rather than quietly disable it"
        )
    return path.read_text(encoding="utf-8")


def stale_phrases_in(root: Path, pages=PAGES, phrases=STALE_PHRASES) -> dict:
    """Which of ``phrases`` each page still carries. Empty means clean.

    Case-insensitive: a sentence that opens with "In-process-only" is the
    same claim as one that does not.
    """
    found = {}
    for page in pages:
        text = _read(root, page).casefold()
        hits = sorted(p for p in phrases if p.casefold() in text)
        if hits:
            found[page] = hits
    return found


def missing_phrases_in(root: Path, required=REQUIRED_PHRASES) -> dict:
    """Which required phrases each page lacks. Empty means every page speaks."""
    missing = {}
    for page, phrases in required.items():
        text = _read(root, page).casefold()
        gone = sorted(p for p in phrases if p.casefold() not in text)
        if gone:
            missing[page] = gone
    return missing


def test_every_governed_page_is_checked_in_both_directions():
    """The two rules govern the same pages, or one of them has a blind spot.

    `REQUIRED_PHRASES` is a dict and `PAGES` a tuple, so adding a page to
    one and not the other reads as complete and checks half as much —
    the absence rule would cover a page the presence rule never opens.
    """
    assert set(REQUIRED_PHRASES) == set(PAGES), (
        "PAGES and REQUIRED_PHRASES name different pages: "
        f"{sorted(set(PAGES) ^ set(REQUIRED_PHRASES))}. A page in one and "
        "not the other is checked in one direction only."
    )


def test_the_four_pages_do_not_describe_the_pre_s4a_credential_model():
    """No page still says model calls or live configuration are unshipped."""
    stale = stale_phrases_in(REPO_ROOT)
    assert not stale, (
        f"these pages still describe the pre-S4a model: {stale}. S4a shipped "
        f"`ctx.llm.complete(step, messages)`, `ctx.config.steps()` and the "
        f"run-scoped `config_get` tool, and a container reaches the gateway "
        f"with its agent key and the invocation's run token — see "
        f"docs/authoring/LLM_Gateway.md, which is normative for all four."
    )


def test_the_four_pages_name_the_gateway_and_the_run_token_header():
    """Deleting a stale sentence is not the same as writing the true one."""
    missing = missing_phrases_in(REPO_ROOT)
    assert not missing, (
        f"these pages no longer say how a model call is made: {missing}. "
        f"Every one of them names the gateway; every one that tells a "
        f"container how to call it names the header the run token travels "
        f"in, because a call without it answers 401 run_token_required."
    )


def test_the_stale_phrase_rule_catches_an_injected_claim(tmp_path):
    """Negative probe: the rule must go red on a page that lies.

    Injected rather than trusted: a clean tree cannot tell a rule that
    reads the files from one that returns an empty dict.
    """
    root = tmp_path / "repo"
    for page in PAGES:
        (root / page).parent.mkdir(parents=True, exist_ok=True)
        (root / page).write_text(
            "Model calls go to the gateway with `X-LibreRun-Run-Token` "
            "beside the agent key; `ctx.llm.complete` is the SDK call.\n",
            encoding="utf-8",
        )
    assert stale_phrases_in(root) == {}
    assert missing_phrases_in(root) == {}

    victim = root / "docs" / "authoring" / "SDK.md"
    victim.write_text(
        victim.read_text(encoding="utf-8")
        + "| **Model calls** | `ctx.llm` | the gateway; not in this release "
        "— an agent holds its own keys until then |\n",
        encoding="utf-8",
    )
    assert stale_phrases_in(root) == {
        "docs/authoring/SDK.md": ["holds its own keys", "not in this release"]
    }

    # And the case that reads as a sentence rather than as a table cell,
    # capitalised the way a page would actually open one.
    contract = root / "docs" / "authoring" / "Run_Contract_v1.md"
    contract.write_text(
        contract.read_text(encoding="utf-8")
        + "In-process-only: a container holds its own provider keys.\n",
        encoding="utf-8",
    )
    assert "docs/authoring/Run_Contract_v1.md" in stale_phrases_in(root)


def test_the_required_phrase_rule_catches_a_page_that_says_nothing(tmp_path):
    """Negative probe for the other direction: silence must fail too.

    The failure this guards against is the cheap fix — deleting the
    sentence that was wrong and writing no replacement, which passes a
    forbidden-phrase sweep and leaves the reader with no model path at
    all.
    """
    root = tmp_path / "repo"
    for page in PAGES:
        (root / page).parent.mkdir(parents=True, exist_ok=True)
        (root / page).write_text(
            "Model calls go to the gateway with `X-LibreRun-Run-Token` "
            "beside the agent key; `ctx.llm.complete` is the SDK call.\n",
            encoding="utf-8",
        )
    assert missing_phrases_in(root) == {}

    (root / "docs" / "authoring" / "Container_Agents.md").write_text(
        "A container agent is an HTTP server the chassis addresses.\n",
        encoding="utf-8",
    )
    assert missing_phrases_in(root) == {
        "docs/authoring/Container_Agents.md": ["X-LibreRun-Run-Token", "gateway"]
    }


def test_a_renamed_page_fails_the_rule_rather_than_passing_it(tmp_path):
    """Negative probe for the skip: a missing page is not a clean page."""
    root = tmp_path / "repo"
    (root / "docs" / "authoring").mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="docs/authoring/SDK.md"):
        stale_phrases_in(root)
