"""The migration graph has exactly one head (the K blueprint's §5.4).

Two batches of one wave each write a migration from the same parent —
K5a's ``0018_agent_settings`` and K6's ``0019_secrets`` both start from
``0017_run_error_reason`` — and git cannot see the fork: the files do not
overlap, so a branch merged with ``main`` and not re-pointed carries two
heads. ``scripts/migration_parity.sh`` reads only the first line of
``alembic heads`` for its label, and it happened once already (0015 was
numbered 0014 and re-pointed by hand).

``alembic upgrade head`` refuses two heads, so the database jobs would go
red; this is the fast, database-free version of that check, run by every
backend suite: the revision graph read from the files alone. Beside it,
every revision id must fit ``alembic_version.version_num``, a
``VARCHAR(32)`` (0016's id was cut to fit), and the negative probe proves
the check can fail: a revision that forks the chain is two heads.
"""
from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

from alembic.script import ScriptDirectory

ALEMBIC = Path(__file__).resolve().parents[1] / "alembic"

# alembic_version.version_num's width: the id is stored in it.
VERSION_NUM_WIDTH = 32


def _one_head_problem(script: ScriptDirectory) -> str | None:
    """None when the graph has exactly one head, else what is wrong,
    naming every head so the fix — which revision to re-point — is in
    the message."""
    heads = sorted(script.get_heads())
    if len(heads) == 1:
        return None
    if not heads:
        return "the migration graph has no head: no revision was read"
    return (
        f"the migration graph has {len(heads)} heads, {heads}: re-point the "
        f"later migration's down_revision at the other's id (the K "
        f"blueprint's §5.4)"
    )


def test_the_migration_graph_has_exactly_one_head():
    script = ScriptDirectory(str(ALEMBIC))
    problem = _one_head_problem(script)
    assert problem is None, problem
    # The walk read the real chain, down to the baseline, not an empty
    # directory that has no heads to count wrongly.
    revisions = [rev.revision for rev in script.walk_revisions()]
    assert "0001_baseline" in revisions and len(revisions) >= 18


def test_every_revision_id_fits_version_num():
    script = ScriptDirectory(str(ALEMBIC))
    too_long = sorted(
        rev.revision
        for rev in script.walk_revisions()
        if len(rev.revision) > VERSION_NUM_WIDTH
    )
    assert too_long == [], (
        f"revision ids longer than alembic_version.version_num "
        f"(VARCHAR({VERSION_NUM_WIDTH})) cannot be stamped: {too_long}"
    )


def test_a_revision_that_forks_the_chain_is_two_heads(tmp_path):
    """The negative probe, on a copy of the chain: a revision written from
    the head's parent — what the second of two parallel migration batches
    carries until it re-points — turns the check red, naming both heads."""
    copy = tmp_path / "alembic"
    shutil.copytree(ALEMBIC / "versions", copy / "versions",
                    ignore=shutil.ignore_patterns("__pycache__"))
    head = ScriptDirectory(str(ALEMBIC)).get_current_head()
    parent = ScriptDirectory(str(ALEMBIC)).get_revision(head).down_revision
    (copy / "versions" / "probe_fork.py").write_text(
        textwrap.dedent(
            f'''\
            """a probe that forks the chain"""
            revision = "probe_fork"
            down_revision = "{parent}"
            branch_labels = None
            depends_on = None


            def upgrade() -> None:
                pass


            def downgrade() -> None:
                pass
            '''
        )
    )

    problem = _one_head_problem(ScriptDirectory(str(copy)))

    assert problem is not None, "a forked chain passed the one-head check"
    assert "2 heads" in problem
    assert head in problem and "probe_fork" in problem
