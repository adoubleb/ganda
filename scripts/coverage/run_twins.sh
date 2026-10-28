#!/bin/bash
# The coverage evaluation's campaigns: for each engine, twins A and B -- the same seeds,
# each on a fresh server of the instrumented image -- under `--coverage variant
# --workers 1` for a time budget. Run on the campaign host from the Ganda tree:
#
#   scripts/coverage/run_twins.sh <phase> <hours> <first seed> [engine...]
#
# Everything lands in $EVAL/<phase>/ (default EVAL=/home/hakam/ganda/mc-eval): the code
# snapshot (code.tgz and its SHA-256), the environment (env.txt: host, images and their
# digests, the command lines) and one directory per run (<engine>-A, <engine>-B) holding
# its window log, statistics, cumulative coverage and console log.
set -euo pipefail
phase=$1 hours=$2 seed=$3
shift 3
engines=${*:-orientdb surrealdb arangodb}
EVAL=${EVAL:-/home/hakam/ganda/mc-eval}
PY=${PY:-/home/hakam/ganda/venv312/bin/python}
out="$EVAL/$phase"
mkdir -p "$out"
tree=$(pwd)
tar --exclude=./campaign_reports --exclude=./bugs --exclude=./coverage --exclude=__pycache__ --exclude=.git -czf "$out/code.tgz" -C "$tree" .
sha256sum "$out/code.tgz" > "$out/code.tgz.sha256"

# The configuration measured: every generation axis on (the ones off by default at a
# substantial probability; large rows rarely, as a large row grows toward the engine's
# limits), the known-bug guards off (every variant runs), and no bundles (the many
# mismatches guards-off finds are only counted). Collation only where the engine has it
# (SurrealDB's provider has no collation axis).
AXES="--custom-key-probability 0.5 --inheritance-probability 0.5 --narrow-type-probability 0.5 --date-map-probability 0.5
  --list-index-probability 0.5 --churn-edge-update-probability 0.15 --binary-probability 0.5 --large-row-probability 0.02"
declare -A extra=(
  [orientdb]="--collate-probability 0.5"
  [surrealdb]="--schemafull-probability 0.5"
  [arangodb]="--collate-probability 0.5"
)
MEASURE="--no-known-bugs --no-bundles"

declare -A image=(
  [orientdb]=orientdb:3.2.57
  [surrealdb]=ganda-cov/surrealdb:3.3.4
  [arangodb]=ganda-cov/arangodb:3.12.11
)
{
  echo "phase: $phase  hours: $hours  first seed: $seed  started: $(date -Is)"
  echo "host: $(hostname)  $(uname -srm)  cpus: $(nproc)  load: $(cut -d' ' -f1-3 /proc/loadavg)"
  echo "python: $($PY --version 2>&1)  code: $(cut -d' ' -f1 "$out/code.tgz.sha256")"
  echo "jacoco agent: $(sha256sum "${JACOCO_AGENT:-/home/hakam/ganda/cov-src/jacocoagent.jar}")"
  for e in $engines; do
    echo "$e image: ${image[$e]} $(docker image inspect -f '{{.Id}}' "${image[$e]}")"
  done
} > "$out/env.txt"

for e in $engines; do
  for twin in A B; do
    run="$out/$e-$twin"
    mkdir -p "$run"
    # shellcheck disable=SC2206
    cmd=("$PY" -u -m ganda "$e" run --coverage variant --workers 1 --duration-hours "$hours" --iterations 100000 --seed "$seed"
         --run-tag "$e-$twin" --stats-dir "$run" --out-dir "$run/bugs" $MEASURE $AXES ${extra[$e]})
    echo "$e-$twin: ${cmd[*]}" >> "$out/env.txt"
    (
      export JACOCO_AGENT=${JACOCO_AGENT:-/home/hakam/ganda/cov-src/jacocoagent.jar}
      export SURREAL_IMAGE=${image[surrealdb]} ARANGO_IMAGE=${image[arangodb]}
      setsid nohup "${cmd[@]}" > "$run/console.log" 2>&1 < /dev/null &
    )
  done
done
echo "started: $engines (twins A and B) -> $out"
