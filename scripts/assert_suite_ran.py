#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Assert that a test suite RAN, from its own JUnit report.

A runner's exit code says "nothing collected" or "something failed". It
says nothing about a suite that collected everything and skipped it, and
nothing about one whose discovery was narrowed until a handful of tests
remained. Both of those report success by not looking, which is the
failure `.github/workflows/unit-suites.yml` exists to prevent — so a job
that runs a suite without checking it ran would reproduce it.

ONE script for both halves of that workflow, and that is the point
rather than tidiness. The backend half grew a baseline check while the
frontend half kept only "vitest exits 1 when it finds no files" — two
standards for one risk, and the weaker one was the half nobody had
looked at twice (Codex P2). A rule stated once cannot drift between its
callers.

The two reports are shaped differently and this reads both:

  pytest  <testsuites><testsuite tests= skipped= errors= failures=>
  vitest  <testsuites tests= failures= errors=>   <- no `skipped` here
            <testsuite tests= skipped= ...> x6    <- and six of them

So the counts are summed over every <testsuite> element rather than read
off whichever one happens to be first, and a lone <testsuite> root is
accepted too.

Usage: assert_suite_ran.py REPORT.xml BASELINE LABEL
"""
import sys
import xml.etree.ElementTree as ET


def main(argv):
    if len(argv) != 4:
        sys.exit("usage: assert_suite_ran.py REPORT.xml BASELINE LABEL")
    path, baseline, label = argv[1], int(argv[2]), argv[3]

    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        sys.exit(f"{label}: cannot read {path}: {exc}")

    suites = ([root] if root.tag == "testsuite"
              else root.findall(".//testsuite"))
    if not suites:
        sys.exit(f"{label}: no <testsuite> in {path}; the run produced nothing")

    def total(name):
        return sum(int(s.get(name, 0)) for s in suites)

    tests, skipped = total("tests"), total("skipped")
    errors, failures = total("errors"), total("failures")
    recorded = len(root.findall(".//testcase"))
    print(f"{label}: tests={tests} skipped={skipped} "
          f"errors={errors} failures={failures} recorded={recorded} "
          f"(baseline {baseline})")

    # The counts above are the report's own SUMMARY of itself. A producer
    # that writes `<testsuite tests="1048"/>` and no <testcase> elements
    # satisfies every rule below while having run nothing — the declared
    # total is not evidence that anything happened, it is an assertion by
    # the thing under test (Codex round 19).
    #
    # Measured across every report this repository produces — pytest and
    # vitest, 3 tests to 1253 — the two numbers are ALWAYS equal, skips
    # included, because a skipped test still emits its <testcase> with a
    # <skipped/> child. So demanding equality costs nothing real and
    # refuses a report that merely claims a number.
    if recorded != tests:
        sys.exit(
            f"{label}: the report DECLARES {tests} tests but records "
            f"{recorded} <testcase> elements. A summary without the cases "
            f"it summarises is not evidence the suite ran — check what "
            f"produced this report."
        )

    # A FLOOR, not `> 0`. Any positive count passed the first version of
    # this, so a conftest or a vitest config that narrowed discovery could
    # leave one clean test and satisfy the gate — recreating the very hole
    # the workflow was added to close. A lower bound is not a census
    # (blueprint §12 136(f)).
    #
    # The floor goes stale UPWARD: it will not notice a suite that grows
    # and then loses the growth. It cannot go stale downward, which is the
    # direction this gate is for.
    if tests < baseline:
        sys.exit(
            f"{label}: collected {tests} tests against a measured baseline "
            f"of {baseline}. Fewer means discovery lost something — find "
            f"out what. If the tests were deliberately removed, LOWER the "
            f"baseline in the workflow to the newly measured count."
        )
    if skipped:
        sys.exit(
            f"{label}: {skipped} test(s) skipped. This suite skips nothing "
            f"in this configuration, so a skip means either a service did "
            f"not come up, an optional dependency is missing, or a new "
            f"test can pass by not running."
        )
    if errors or failures:
        sys.exit(f"{label}: {errors} error(s) and {failures} failure(s)")


if __name__ == "__main__":
    main(sys.argv)
