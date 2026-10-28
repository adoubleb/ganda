#!/bin/bash
# The reports of a phase's twin campaigns (scripts/coverage/run_twins.sh), detached:
#
#   scripts/coverage/report_twins.sh <phase> [engine...]
#
# Each engine's report lands in $EVAL/<phase>/<engine>-mc/ (report.json, report.md),
# its console in $EVAL/<phase>/<engine>-mc.log. ArangoDB's runs in its build image, for
# nothing else on the host reads its binary as fast; the others in the host's venv.
set -euo pipefail
phase=$1
shift
engines=${*:-orientdb surrealdb arangodb}
EVAL=${EVAL:-/home/hakam/ganda/mc-eval}
PY=${PY:-/home/hakam/ganda/venv312/bin/python}
COV=${COV:-/home/hakam/ganda/cov-src}
BUILD_IMAGE=public.ecr.aws/b0b8h2r4/arangodb/ubuntubuildarangodb-312:24.04-f7032bef0
tree=$(pwd)
D="$EVAL/$phase"
for e in $engines; do
  a="$D/$e-A/$e-A-windows.jsonl.gz" b="$D/$e-B/$e-B-windows.jsonl.gz"
  args=(--log "$a" --twin "$b" --out "$D/$e-mc")
  case $e in
    orientdb)
      cmd=("$PY" scripts/coverage/mc_report.py orientdb "${args[@]}" --probe-map "$EVAL/maps/probe-lines-282bbeb41447.jsonl.gz") ;;
    surrealdb)
      cmd=("$PY" scripts/coverage/mc_report.py surrealdb "${args[@]}" --binary "$COV/surrealdb-build/target/release/surreal"
           --profile "$(ls "$D/$e-A/$e-A-coverage/"*-1.profraw)") ;;
    arangodb)
      cmd=(docker run --rm -v "$tree:/g" -v "$EVAL:$EVAL" -v "$COV/arangodb-build:/build:ro" -w /g -e PYTHONPATH=/g "$BUILD_IMAGE"
           python3 scripts/coverage/mc_report.py arangodb "${args[@]}" --binary /build/out/bin/arangod
           --profile "$(ls "$D/$e-A/$e-A-coverage/"*-1.profraw)") ;;
  esac
  echo "$e: ${cmd[*]}" >> "$D/reports.txt"
  PYTHONPATH="$tree" setsid nohup "${cmd[@]}" > "$D/$e-mc.log" 2>&1 < /dev/null &
done
echo "reports started: $engines -> $D/<engine>-mc"
