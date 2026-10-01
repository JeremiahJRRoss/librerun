"""A value a workflow hands an option must still be that option's value.

``--password "$PW"`` looks safe and is not. The shell removes the quotes
before Python sees ``argv``, so when ``$PW`` expands to something that
begins with ``-``, argparse reads it as an *option* and ``--password``
is left with nothing:

    librerun_smoke.py: error: argument --password: expected one argument

That is not hypothetical. ``hand-configured-boot`` generates its admin
password with ``secrets.token_urlsafe(24)``, whose alphabet is URL-safe
base64 — last two entries ``-`` and ``_`` — and whose leading character
is the top six bits of the first random byte, uniform over all 64
positions. So the token begins with ``-`` with probability exactly
**1/64**, measured at 6251 of 400000 (1.5628% against the analytic
1.5625%), and the job died on the parse rather than on the boot it
exists to test (issue #84).

``--password="$PW"`` is immune: the value is part of the same argv word,
so there is no second word for argparse to mistake for an option.

**The rule this file enforces.** In any workflow, when an option that
its program says *takes a value* is given one that is a shell expansion,
the ``=`` form is required. The boundary is deliberate and worth stating:
a literal (``--agent vita-v1``) is left alone, because a literal cannot
surprise anyone, and requiring ``=`` everywhere would be a large diff
that buys nothing. An expansion is exactly the case nobody can check by
reading.

**Which options take a value is asked, never listed.** A hand-kept list
of credential-looking names is the defect this repository keeps finding
— a rule applied where the report pointed and not everywhere it is true.
So each script's *own* ``ArgumentParser`` is captured and interrogated:
it is the only thing that knows ``--no-require-trace`` is a flag while
``--trace-viewer-url`` is not. That also keeps the guard honest about
programs it does not understand: ``git reset --hard --quiet "$base"``
and ``docker run --network "$net"`` are not ours, their ``--quiet`` and
``--network`` mean different things, and ``--quiet=$base`` would be
wrong. They are not scanned, because no parser of ours describes them.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import subprocess
import sys

from .test_adapter_kit import (
    _load_workflow,
    _shell_invocations,
    _workflow_dir,
    _workflow_files,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class _ParserCaptured(Exception):
    """Carries the parser out of ``main()`` before it consumes ``argv``."""

    def __init__(self, parser):
        super().__init__("parser captured")
        self.parser = parser


def _tracked_files(root):
    """The tracked set, so an untracked scratch file is never scanned."""
    done = subprocess.run(
        ["git", "ls-files"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return set(done.stdout.split())


def _value_taking_long_options(path):
    """Ask the script's OWN parser which long options take a value.

    ``main()`` builds the parser and then calls ``parse_args``; patching
    that call to hand the parser back gets the real object, fully
    constructed, without running the program. Reading ``--help`` text
    would work for these scripts, but it reports what argparse chose to
    *print* rather than what it will *do*, and this repository has
    already been bitten once by a parser knowing a spelling its
    ``--help`` does not show.

    ``None`` for a program that does not use argparse at all — and that
    is decided by reading the source, not by running it. The first draft
    called ``main()`` on everything and ``scripts/assert_suite_ran.py``,
    which parses ``sys.argv`` by hand, exited the test run on its usage
    line. A program with no parser has no options for this rule to
    speak about; a program WITH one that cannot be captured is a hole,
    so that case raises instead of quietly returning nothing.
    """
    source = path.read_text(encoding="utf-8")
    if "ArgumentParser" not in source:
        return None

    real = argparse.ArgumentParser.parse_args

    def capture(self, *args, **kwargs):
        raise _ParserCaptured(self)

    argparse.ArgumentParser.parse_args = capture
    module_name = f"_option_form_probe_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            entry = getattr(module, "main", None)
            if entry is not None:
                try:
                    entry([]) if _takes_an_argv(entry) else entry()
                except _ParserCaptured as captured:
                    return {
                        option
                        for action in captured.parser._actions
                        for option in action.option_strings
                        if option.startswith("--") and action.nargs != 0
                    }
                except SystemExit:
                    pass
        finally:
            sys.modules.pop(module_name, None)
    finally:
        argparse.ArgumentParser.parse_args = real

    raise AssertionError(
        f"{path} builds an ArgumentParser but this scan could not capture "
        f"it, so every option it takes is invisible to the rule. Returning "
        f"an empty set here would make the guard silently stop watching a "
        f"script — fix the capture rather than the expectation."
    )


def _takes_an_argv(entry):
    import inspect

    try:
        return len(inspect.signature(entry).parameters) >= 1
    except (TypeError, ValueError):
        return False


def _run_blocks(doc):
    """Every ``run:`` script in a workflow, whatever job or step it is in."""
    blocks = []
    for job in (doc.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                blocks.append(step["run"])
    return blocks


def _is_an_expansion(token):
    """A value the reader cannot evaluate: ``$X``, ``${X}`` or ``$(…)``."""
    return "$" in token


def _violations_in_block(block, root, tracked, options_by_script):
    """``(violations, invocations_recognised)`` for ONE ``run:`` script.

    The single reader. Everything that scans — the real tree, a
    synthetic one-line command, the injected fixture — comes through
    here, because two loops over one grammar drift apart and this
    repository has paid for that more than once.
    """
    violations = []
    recognised = 0
    for tokens in _shell_invocations(block):
        for index, token in enumerate(tokens):
            if not token.endswith(".py") or token not in tracked:
                continue
            if token not in options_by_script:
                options_by_script[token] = _value_taking_long_options(root / token)
            takes_a_value = options_by_script[token]
            if not takes_a_value:
                break
            recognised += 1
            rest = tokens[index + 1:]
            for spot, word in enumerate(rest):
                if word not in takes_a_value:
                    continue
                following = rest[spot + 1] if spot + 1 < len(rest) else ""
                if _is_an_expansion(following):
                    violations.append((token, word, following))
            break
    return violations, recognised


def _scan(workflow_files, root):
    """``(violations, invocations_recognised)`` over these workflows.

    The second half of the pair is not decoration: without it "no
    violations" and "recognised no invocation at all" are the same
    answer, and this repository's rule is that a gate reporting success
    by not looking is worse than no gate.
    """
    tracked = _tracked_files(root)
    options_by_script = {}
    violations = []
    recognised = 0
    for path in workflow_files:
        for block in _run_blocks(_load_workflow(path)):
            found, seen = _violations_in_block(
                block, root, tracked, options_by_script
            )
            recognised += seen
            violations.extend((path.name, *item) for item in found)
    return violations, recognised


def _scan_one(command):
    """The same reader, over a single synthetic command."""
    found, _ = _violations_in_block(
        command, REPO_ROOT, _tracked_files(REPO_ROOT), {}
    )
    return found


def test_no_workflow_hands_an_expansion_to_an_option_space_separated():
    """The rule, on the real tree."""
    violations, scanned = _scan(_workflow_files(_workflow_dir()), REPO_ROOT)

    assert scanned, (
        "this scan recognised no invocation of any script of ours in any "
        "workflow, so it examined nothing and would report success on a "
        "tree where every credential is passed the unsafe way"
    )
    assert not violations, "\n".join(
        [
            "a value-taking option is given a shell expansion as a separate "
            "word; if it expands to something beginning with '-', argparse "
            "reads it as an option and the value is lost. Use the = form:",
            *(
                f"  {workflow}: {script} {option} {value}"
                f"   ->   {option}={value}"
                for workflow, script, option, value in violations
            ),
        ]
    )


def test_a_literal_value_is_left_alone():
    """The boundary, asserted rather than described.

    A literal cannot surprise anyone, so the rule does not reach it —
    and a guard that quietly widened to every option would produce a
    large diff of pure churn and then be switched off.
    """
    smoke = "scripts/librerun_smoke.py"
    takes = _value_taking_long_options(REPO_ROOT / smoke)
    assert "--agent" in takes, "this case needs an option that takes a value"

    literal = f"python3 {smoke} --agent vita-v1"
    expansion = f'python3 {smoke} --agent "$AGENT"'

    assert _scan_one(literal) == []
    assert _scan_one(expansion), (
        "an expansion passed space-separated must be reported, or the "
        "guard's whole subject is invisible to it"
    )


def test_the_guard_catches_the_space_form_when_it_is_injected(tmp_path):
    """The negative test: inject the violation into a COPY and watch it fire.

    On a clean tree a working checker and a broken one are
    indistinguishable, so the only way to know this one looks is to give
    it something to find. The injection is a copy of the real
    ``hand-configured-boot`` invocation with the ``=`` taken back out —
    the exact shape that failed in CI.
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    good = workflows / "probe.yml"
    good.write_text(
        "jobs:\n"
        "  probe:\n"
        "    steps:\n"
        "      - run: |\n"
        "          python3 scripts/librerun_smoke.py \\\n"
        '            --email="$SMOKE_ADMIN_EMAIL" --password="$SMOKE_ADMIN_PASSWORD"\n',
        encoding="utf-8",
    )
    clean, scanned = _scan([good], REPO_ROOT)
    assert scanned, "the control examined nothing, so it decides nothing"
    assert clean == [], f"the = form must pass, got {clean}"

    good.write_text(
        "jobs:\n"
        "  probe:\n"
        "    steps:\n"
        "      - run: |\n"
        "          python3 scripts/librerun_smoke.py \\\n"
        '            --email "$SMOKE_ADMIN_EMAIL" --password "$SMOKE_ADMIN_PASSWORD"\n',
        encoding="utf-8",
    )
    caught, scanned = _scan([good], REPO_ROOT)
    assert scanned, "the injected case examined nothing"
    options = sorted(option for _, _, option, _ in caught)
    assert options == ["--email", "--password"], (
        f"the injection must be caught on BOTH options, got {options}"
    )


def test_a_flag_is_not_mistaken_for_a_value_taking_option():
    """``--no-require-trace`` takes nothing, so nothing follows it to check.

    Without this distinction the guard would demand ``=`` on a flag,
    which argparse rejects outright — a guard that breaks working
    commands is worse than the defect it chases.
    """
    takes = _value_taking_long_options(REPO_ROOT / "scripts/librerun_smoke.py")
    assert "--no-require-trace" not in takes
    assert "--trace-viewer-url" in takes


def test_a_program_that_is_not_ours_is_not_scanned():
    """``git reset --hard --quiet "$base"`` is not a finding.

    ``--quiet`` is git's, means something else, and takes no value;
    ``--quiet=$base`` would be a bug this guard introduced. The scan only
    speaks about invocations of tracked scripts of ours whose parser it
    has read.
    """
    assert _scan_one('git reset --hard --quiet "$base"') == []
    assert _scan_one('docker run --rm --network "$net" curlimages/curl:8.10.1') == []
