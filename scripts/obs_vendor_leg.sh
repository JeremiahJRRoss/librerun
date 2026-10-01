#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# One vendor's contract test (blueprint S7a), driven by
# .github/workflows/obs-vendors.yml. Kept out of the workflow because
# it runs three times: three copies of this is three places for one
# timing bug to hide.
#
#   1. point the overlay at the mock intake and recreate the two
#      processes that talk to a vendor — vector and otel-bridge;
#   2. run the keyless demo run that carries the S4 raw-instrumentation
#      fixture (echo-v1 with `fetch_and_emit`);
#   3. wait for BOTH legs to arrive, with a deadline, and fail naming
#      the leg that did not;
#   4. decode what the mock received and judge it
#      (scripts/obs_vendor_contract.py);
#   5. check the bundled Jaeger viewer still has the same trace — an
#      overlay adds a destination, it does not displace the one in the
#      box.
#
# Run from the repository root with the demo stack already up.
set -euo pipefail

VENDOR="${1:?usage: obs_vendor_leg.sh <datadog|elastic|splunk>}"
JOURNAL="obs-capture/capture.jsonl"
PROFILES=(--profile app --profile viewer --profile demo --profile obs)
: "${OBS_FIXTURE:?OBS_FIXTURE must name the S4 raw-instrumentation fixture}"

# The path each leg lands on, as that vendor documents it. Duplicated
# from scripts/obs_vendor_contract.py on purpose: this half only has to
# know when to STOP waiting, and the judging is the checker's alone.
case "$VENDOR" in
  datadog) LOG_PATH="/api/v2/logs";                TRACE_PATH="/v1/traces" ;;
  elastic) LOG_PATH="/_bulk";                      TRACE_PATH="/v1/traces" ;;
  splunk)  LOG_PATH="/services/collector/event";   TRACE_PATH="/v2/trace/otlp" ;;
  *) echo "::error::unknown vendor '$VENDOR'"; exit 1 ;;
esac

echo "::group::$VENDOR — select the overlay"
export LIBRERUN_OBS_VENDOR="$VENDOR"
# The backend is recreated with them because it carries the selector's
# NAME (never the vendor's credentials) for /admin/otel-status, and the
# admin report is checked below against the overlay really loaded.
# `--no-deps`: postgres, redis and the agent are already up and have
# nothing to do with the selection.
./compose.sh "${PROFILES[@]}" up -d --no-deps --force-recreate \
  vector otel-bridge backend

# All three must be RUNNING, not merely created: a crash-looping bridge
# is invisible to the run, and the log leg alone would still arrive.
for service in librerun-vector librerun-otel-bridge librerun-backend; do
  ok=0
  for _ in $(seq 1 30); do
    state="$(docker inspect -f '{{.State.Status}}' "$service" 2>/dev/null || echo missing)"
    if [ "$state" = "running" ]; then ok=1; break; fi
    sleep 2
  done
  if [ "$ok" != 1 ]; then
    echo "::error::$service is not running with the $VENDOR overlay selected"
    docker logs "$service" 2>&1 | tail -60 || true
    exit 1
  fi
done
# …and still running a moment later: a collector that rejects its
# config exits a second or two in, which the loop above can race.
sleep 5
for service in librerun-vector librerun-otel-bridge librerun-backend; do
  state="$(docker inspect -f '{{.State.Status}}' "$service")"
  [ "$state" = "running" ] || {
    echo "::error::$service exited after starting with the $VENDOR overlay ($state)"
    docker logs "$service" 2>&1 | tail -60 || true
    exit 1
  }
done

# The backend answers again before anything drives it.
ready=0
for _ in $(seq 1 60); do
  if curl -sf -o /dev/null http://localhost:8000/api/v1/health; then ready=1; break; fi
  sleep 2
done
[ "$ready" = 1 ] || {
  echo "::error::the backend did not come back after recreating it for $VENDOR"
  docker logs librerun-backend 2>&1 | tail -60 || true
  exit 1
}
echo "vector, otel-bridge and the backend are up on the $VENDOR overlay"
echo "::endgroup::"

echo "::group::$VENDOR — the admin status names the active overlay"
# The Accept item, end to end rather than from the unit tests alone: an
# operator opening Admin -> Observability sees THIS vendor named.
python3 scripts/obs_admin_status_check.py "$VENDOR"
echo "::endgroup::"

echo "::group::$VENDOR — the keyless demo run"
# A fresh journal, taken HERE rather than before the recreate: a leg
# left over from the previous vendor would otherwise satisfy this one's
# "both legs arrived" wait — the previous Vector's last batch, landing
# on the same paths — and the checks would run before this run's own
# records had a chance to arrive. By now that container is gone.
# (Truncation, not delete-and-recreate: the mock holds an open
# descriptor, and an unlinked inode would swallow everything that
# follows while every check read an empty file.)
: > "$JOURNAL"

python3 echo_run.py "obs-run-$VENDOR.json"
TRACE_ID="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['trace_id'])" "obs-run-$VENDOR.json")"
RUN_NUMBER="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['run_number'])" "obs-run-$VENDOR.json")"
echo "trace $TRACE_ID   run $RUN_NUMBER"
echo "::endgroup::"

echo "::group::$VENDOR — wait for both legs"
# Named separately so a timeout says WHICH leg never arrived. A single
# "nothing came" would send someone debugging the wrong half.
deadline=$((SECONDS + 180))
while :; do
  have_logs=0; have_traces=0
  if [ -s "$JOURNAL" ]; then
    grep -Fq "\"path\": \"$LOG_PATH\"" "$JOURNAL" && have_logs=1 || true
    grep -Fq "\"path\": \"$TRACE_PATH\"" "$JOURNAL" && have_traces=1 || true
  fi
  [ "$have_logs" = 1 ] && [ "$have_traces" = 1 ] && break
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "::error::$VENDOR: after 180s the log leg is $( [ $have_logs = 1 ] && echo present || echo MISSING ) and the trace leg is $( [ $have_traces = 1 ] && echo present || echo MISSING ). Paths seen at the mock:"
    python3 - "$JOURNAL" <<'PY'
import json, sys, collections
seen = collections.Counter()
try:
    for line in open(sys.argv[1]):
        line = line.strip()
        if line:
            seen[json.loads(line).get("path")] += 1
except FileNotFoundError:
    pass
print(dict(seen) or "(the mock received nothing at all)")
PY
    docker logs librerun-vector 2>&1 | tail -40 || true
    docker logs librerun-otel-bridge 2>&1 | tail -40 || true
    exit 1
  fi
  sleep 3
done
# A short settle so a batch still in flight is judged whole rather than
# half — the checker reads a snapshot of the journal.
sleep 5
echo "both legs arrived"
echo "::endgroup::"

echo "::group::$VENDOR — decode and judge"
python3 scripts/obs_vendor_contract.py \
  --vendor "$VENDOR" \
  --capture "$JOURNAL" \
  --trace-id "$TRACE_ID" \
  --run-number "$RUN_NUMBER" \
  --forbid "$OBS_FIXTURE" \
  --expect-service librerun-backend \
  --summary-json "obs-contract-$VENDOR.json"
echo "::endgroup::"

echo "::group::$VENDOR — the bundled viewer is unaffected"
# An overlay ADDS a destination. If selecting one silently took the
# Jaeger forward away, every in-box trace link would break and nothing
# above would notice.
python3 - "$TRACE_ID" <<'PY'
import json, sys, time, urllib.request

trace_id = sys.argv[1]
url = f"http://localhost:16686/api/traces/{trace_id}"
for _ in range(20):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            body = json.loads(r.read() or b"{}")
    except Exception as exc:  # the viewer may still be indexing
        body, err = {}, exc
    spans = [s for t in (body.get("data") or []) for s in (t.get("spans") or [])]
    if spans:
        print(f"jaeger still has {len(spans)} span(s) for {trace_id}")
        break
    time.sleep(3)
else:
    raise SystemExit(
        f"the bundled Jaeger viewer has no spans for {trace_id} while a "
        f"vendor overlay is active — selecting an overlay must ADD a "
        f"destination, not replace the one in the box"
    )
PY
echo "::endgroup::"

echo "$VENDOR: both legs green, the fixture on neither, the viewer intact"
