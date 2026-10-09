#!/usr/bin/env bash
# Robust 12-hour SAP HANA fuzzing campaign for the Ganda fuzzer, run on this host
# (direct connection to HANA Cloud -- no tunnel). Terminates gracefully if the instance
# becomes unreachable for a sustained window, and at a 12-hour hard deadline, keeping every
# finding bundle written so far (bundles are written to disk the instant each is found).
#
# Usage:  nohup bash run_12h.sh > /dev/null 2>&1 &
# Everything lands under a timestamped run directory printed at startup.
set -u

cd /data/i782049/ganda
. .venv/bin/activate
set -a; . ./.env; set +a

# --- knobs ---------------------------------------------------------------------------------
DURATION=${DURATION:-43200}          # 12 hours, in seconds
POLL=${POLL:-60}                     # reachability / deadline check interval (s)
UNREACHABLE_LIMIT=${UNREACHABLE_LIMIT:-5}   # consecutive failed probes before giving up (~5 min)
ITERATIONS=${ITERATIONS:-100000}     # effectively unbounded; the watchdog stops it
SEED=${SEED:-1}
WORKERS=${WORKERS:-2}
# capped seeds: each finishes in ~1-2 min so many seeds (schema/data variety) run in 12h
SELECT_CASES=${SELECT_CASES:-40}
TRAVERSAL_CASES=${TRAVERSAL_CASES:-20}
PATH_CASES=${PATH_CASES:-10}
WRITE_CASES=${WRITE_CASES:-8}
HHOST=$HANA_HOST
HPORT=${HANA_PORT:-443}

TS=$(date +%Y%m%d-%H%M%S)
RUN=/data/i782049/ganda/runs/campaign-$TS
mkdir -p "$RUN"
BUGS="$RUN/bugs"
LOG="$RUN/campaign.log"
WLOG="$RUN/watchdog.log"
echo "RUN=$RUN"
echo "run dir: $RUN" | tee "$WLOG"

# --- a real DB health probe: a SELECT 1 round trip through the driver, so a STOPPED instance
# (TCP ingress up but error 1890) counts as unhealthy -- a bare TCP check would pass it.
reachable() {
  python3 - <<'PY' >/dev/null 2>&1
import os
from hdbcli import dbapi
kw = dict(address=os.environ["HANA_HOST"], port=int(os.environ.get("HANA_PORT", "443")),
          user=os.environ["HANA_USER"], password=os.environ["HANA_PASSWORD"],
          encrypt=True, sslValidateCertificate=os.environ.get("HANA_SSL_VALIDATE", "true").lower() == "true")
c = dbapi.connect(**kw); cur = c.cursor(); cur.execute("SELECT 1 FROM DUMMY"); cur.fetchone(); c.close()
PY
}

# --- launch the campaign -------------------------------------------------------------------
# a big --iterations so it never stops on its own; findings are unbounded by default
# (--max-findings 0); bundles stream to $BUGS as they are found.
setsid python -u -m ganda hana run \
    --iterations "$ITERATIONS" --seed "$SEED" --workers "$WORKERS" \
    --select-cases "$SELECT_CASES" --traversal-cases "$TRAVERSAL_CASES" \
    --path-cases "$PATH_CASES" --write-cases "$WRITE_CASES" \
    --out-dir "$BUGS" --run-tag camp12h --stats-dir "$RUN" \
    > "$LOG" 2>&1 &
CAMP=$!
echo "$(date '+%F %T') campaign pid $CAMP, writing bundles to $BUGS" | tee -a "$WLOG"
echo "$CAMP" > "$RUN/campaign.pid"

# --- graceful stop: SIGINT (clean, if it can) then SIGTERM then SIGKILL --------------------
stop_campaign() {
  local why="$1"
  echo "$(date '+%F %T') STOPPING ($why): SIGINT -> pid group $CAMP" | tee -a "$WLOG"
  kill -INT -"$CAMP" 2>/dev/null || kill -INT "$CAMP" 2>/dev/null
  # SIGINT often cannot interrupt a blocking driver call; give it a short grace, then SIGTERM
  for _ in $(seq 1 10); do kill -0 "$CAMP" 2>/dev/null || break; sleep 2; done
  if kill -0 "$CAMP" 2>/dev/null; then
    echo "$(date '+%F %T') still alive after 20s, SIGTERM" | tee -a "$WLOG"
    kill -TERM -"$CAMP" 2>/dev/null || kill -TERM "$CAMP" 2>/dev/null
    sleep 10
    kill -0 "$CAMP" 2>/dev/null && { echo "$(date '+%F %T') SIGKILL" | tee -a "$WLOG"; kill -KILL -"$CAMP" 2>/dev/null; }
  fi
  # bundles are per-finding directories written atomically as they are found, so every
  # mismatch already on disk is preserved regardless of which signal stopped the campaign.
}

# --- watchdog loop -------------------------------------------------------------------------
START=$(date +%s)
MISS=0
while kill -0 "$CAMP" 2>/dev/null; do
  sleep "$POLL"
  NOW=$(date +%s); ELAPSED=$((NOW - START))
  if [ "$ELAPSED" -ge "$DURATION" ]; then
    stop_campaign "12h deadline reached (${ELAPSED}s)"; break
  fi
  if reachable; then
    MISS=0
  else
    MISS=$((MISS + 1))
    echo "$(date '+%F %T') HANA unreachable ($MISS/$UNREACHABLE_LIMIT)" | tee -a "$WLOG"
    if [ "$MISS" -ge "$UNREACHABLE_LIMIT" ]; then
      stop_campaign "HANA unreachable for $((MISS * POLL))s"; break
    fi
  fi
  # heartbeat every ~10 min
  if [ $((ELAPSED % 600)) -lt "$POLL" ]; then
    FOUND=$(find "$BUGS" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)
    echo "$(date '+%F %T') alive ${ELAPSED}s elapsed, bundles so far: $FOUND" | tee -a "$WLOG"
  fi
done

wait "$CAMP" 2>/dev/null
FOUND=$(find "$BUGS" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)
echo "$(date '+%F %T') campaign ended, bundles: $FOUND" | tee -a "$WLOG"
# tidy up: drop the schemas this run's workers left on the instance (a killed campaign does
# not drop its own; harmless, but keep the instance clean). Only touches fuzzer schemas.
if [ -f hana_cleanup.py ]; then
  python -I hana_cleanup.py >> "$WLOG" 2>&1 || echo "$(date '+%F %T') cleanup skipped (instance down?)" | tee -a "$WLOG"
fi
echo "$(date '+%F %T') DONE. elapsed $(( $(date +%s) - START ))s, bundles: $FOUND, dir: $RUN" | tee -a "$WLOG"
touch "$RUN/COMPLETE"
