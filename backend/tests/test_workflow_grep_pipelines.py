"""`cmd | grep -q` under `pipefail` reports the PRODUCER, not the match.

`grep -q` exits at the first match without draining stdin. The producer
then writes into a closed pipe, takes `SIGPIPE`, and under
`set -o pipefail` the pipeline's status becomes that failure — so a
pipeline in which grep **found** what it was looking for reports
failure. Where the caller writes `if ! producer | grep -q …`, the
inversion turns that into an accusation: the thing was not found, when
it was.

This repository has learned that rule three times and applied it by
hand three times:

  * `.github/workflows/docs.yml` states it in a header comment —
    "Every probe captures the checker's output into a variable before
    grepping it … Found by writing it the wrong way first."
  * S9's `be3a4a3` fixed the same shape in four more files, after the
    DCO check failed a commit whose sign-off was plainly there.
  * `injection_landed` in `librerun-smoke.yml` still carried it, and
    accused a working canary injection in CI (issue #81).

Three hand-applications is the point at which a rule becomes a guard,
which is what this file is.

**The remedy in docs.yml's own comment is not sufficient**, and that is
worth stating because it is the obvious fix. Capturing into a variable
removes the external producer but *not the pipe*:
`printf '%s' "$out" | grep -q …` still fails under `pipefail` once
`$out` is big enough, because `printf` takes the `SIGPIPE` instead.
Measured at 11 MB with the match at the start: exit **141**. Those
probes passed only because their output was small. The forms that
actually hold are a here-string (`grep -q PAT <<< "$x"` — no pipe) or a
bash `case`, and `be3a4a3` adopted the first of those.

**The predicate is `pipefail`, not `grep -q`.** Without `pipefail` a
pipeline reports its LAST command, so `producer | grep -q` is correct
there and this guard says nothing about it. GitHub Actions' default
shell is `bash -e`, not `bash -eo pipefail`, so the distinction is real
and a rule that ignored it would be demanding churn.
"""

from __future__ import annotations

import re
import subprocess
import sys

import pytest

from .test_adapter_kit import _load_workflow, _workflow_dir, _workflow_files

#: `| grep -q`, `| grep -qF`, `| grep -qiE` … any short-option cluster
#: containing `q`. Spelled as a cluster rather than a list of the four
#: spellings this tree happens to use, because a list would rot.
_PIPED_QUIET_GREP = re.compile(r"\|\s*grep\s+-[a-zA-Z]*q")


def _sets_pipefail(script, shell):
    """Does this step run under `pipefail`?

    Either the script turns it on itself, or the step (or a `defaults`
    block above it) names a shell that does. Both are asked; assuming
    only the first would miss a workflow that sets it once at the top.
    """
    if "pipefail" in (shell or ""):
        return True
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("set ") and "pipefail" in stripped:
            return True
    return False


def _steps_under_pipefail(doc):
    """`(job, step_name, script)` for every `run:` step under pipefail."""
    workflow_shell = ((doc.get("defaults") or {}).get("run") or {}).get("shell", "")
    for job_name, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        job_shell = ((job.get("defaults") or {}).get("run") or {}).get("shell", "")
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            script = step.get("run")
            if not isinstance(script, str):
                continue
            shell = step.get("shell") or job_shell or workflow_shell
            if _sets_pipefail(script, shell):
                yield job_name, step.get("name", "<unnamed>"), script


def _offending_lines(script):
    """The piped quiet greps in one script, comments excluded.

    A comment naming the shape is how the rule gets explained — this
    file's own subject appears in three of them — so a guard that
    flagged comments would make it impossible to write down.
    """
    return [
        line.strip()
        for line in script.splitlines()
        if _PIPED_QUIET_GREP.search(line) and not line.strip().startswith("#")
    ]


def _scan(workflow_files):
    """`(findings, steps_examined)` over these workflows."""
    findings = []
    examined = 0
    for path in workflow_files:
        for job, step, script in _steps_under_pipefail(_load_workflow(path)):
            examined += 1
            for line in _offending_lines(script):
                findings.append((path.name, job, step, line))
    return findings, examined


def test_no_workflow_pipes_into_a_quiet_grep_under_pipefail():
    """The rule, on the real tree."""
    findings, examined = _scan(_workflow_files(_workflow_dir()))

    assert examined, (
        "this scan recognised no step running under pipefail, so it "
        "examined nothing and would report success on a tree where every "
        "verdict is read off a broken pipeline"
    )
    assert not findings, "\n".join(
        [
            "a pipeline under `set -o pipefail` reads its verdict from "
            "`grep -q`, which exits at the first match and leaves the "
            "producer to die of SIGPIPE — so a MATCH can report failure. "
            "Use a here-string (`grep -q PAT <<< \"$x\"`) or a bash "
            "`case`, never a pipe:",
            *(
                f"  {workflow} [{job} / {step}]: {line}"
                for workflow, job, step, line in findings
            ),
        ]
    )


def test_the_shell_really_does_report_sigpipe_under_pipefail():
    """The guard's premise, asked of bash rather than asserted.

    If this ever stops being true the guard is demanding churn for
    nothing, and the right response is to delete it — so the claim is
    measured here instead of written down.
    """
    producer = (
        "{ echo MATCH; for i in $(seq 1 2000); do "
        "echo 'filler padding padding padding'; done; }"
    )

    with_pipefail = subprocess.run(
        ["bash", "-c", f"set -o pipefail\n{producer} | grep -q MATCH"],
        capture_output=True,
    )
    without = subprocess.run(
        ["bash", "-c", f"{producer} | grep -q MATCH"],
        capture_output=True,
    )

    assert with_pipefail.returncode == 141, (
        f"expected 141 (128 + SIGPIPE) from a pipeline whose grep MATCHED, "
        f"got {with_pipefail.returncode}"
    )
    assert without.returncode == 0, (
        f"without pipefail the pipeline reports grep's own success; got "
        f"{without.returncode}. The guard's whole predicate is pipefail, "
        f"so this must stay true or the rule is too broad"
    )


def test_a_here_string_and_a_case_both_survive_it():
    """The two remedies, measured — including the one docs.yml does NOT use.

    `printf "$big" | grep -q` is the idiom `docs.yml`'s header comment
    recommends, and it fails the same way once the variable is large
    enough: capturing removes the external producer, not the pipe.
    """
    build = (
        "big=\"MATCH\"$'\\n'\"$(for i in $(seq 1 200000); do "
        "echo 'filler padding padding padding'; done)\"\n"
    )

    def status(body):
        return subprocess.run(
            ["bash", "-c", f"set -o pipefail\n{build}{body}"],
            capture_output=True,
        ).returncode

    assert status('printf "%s" "$big" | grep -q MATCH') == 141, (
        "the capture-then-pipe idiom must still be shown to fail, or this "
        "test is not about the thing that caught us out"
    )
    assert status('grep -q MATCH <<< "$big"') == 0
    assert status('case "$big" in *MATCH*) exit 0 ;; *) exit 1 ;; esac') == 0


def test_the_guard_catches_the_shape_when_it_is_injected(tmp_path):
    """The negative test: put the violation back, in a copy, and watch it fire."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    probe = workflows / "probe.yml"

    clean = (
        "jobs:\n"
        "  probe:\n"
        "    steps:\n"
        "      - name: safe\n"
        "        run: |\n"
        "          set -euo pipefail\n"
        '          grep -q MATCH <<< "$out"\n'
    )
    probe.write_text(clean, encoding="utf-8")
    findings, examined = _scan([probe])
    assert examined == 1, "the control examined nothing, so it decides nothing"
    assert findings == [], f"the here-string form must pass, got {findings}"

    probe.write_text(
        clean.replace('grep -q MATCH <<< "$out"', 'printf "%s" "$out" | grep -q MATCH'),
        encoding="utf-8",
    )
    findings, examined = _scan([probe])
    assert examined == 1
    assert len(findings) == 1, f"the injected pipeline must be caught, got {findings}"


def test_a_step_without_pipefail_is_not_flagged(tmp_path):
    """The boundary, asserted rather than described.

    Without `pipefail` the pipeline reports grep's own status and is
    correct. Flagging it would be churn, and a guard that demands churn
    gets switched off.
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    probe = workflows / "probe.yml"
    probe.write_text(
        "jobs:\n"
        "  probe:\n"
        "    steps:\n"
        "      - name: no pipefail here\n"
        "        run: |\n"
        "          set -eu\n"
        '          printf "%s" "$out" | grep -q MATCH\n',
        encoding="utf-8",
    )
    findings, examined = _scan([probe])
    assert examined == 0, "a step without pipefail is not this guard's business"
    assert findings == []


def test_a_comment_explaining_the_shape_is_not_a_violation(tmp_path):
    """Three comments in this tree name the shape in order to warn about it."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    probe = workflows / "probe.yml"
    probe.write_text(
        "jobs:\n"
        "  probe:\n"
        "    steps:\n"
        "      - name: explains itself\n"
        "        run: |\n"
        "          set -euo pipefail\n"
        "          # never write `cmd | grep -q PAT` here: see issue #81\n"
        '          grep -q MATCH <<< "$out"\n',
        encoding="utf-8",
    )
    findings, examined = _scan([probe])
    assert examined == 1
    assert findings == [], f"a comment must not be a finding, got {findings}"
