#!/usr/bin/env python3
"""Drive one fuzzing campaign end to end, then triage whatever it found.

Four phases, each skippable:

  1. tests    `pytest tests/ -q` -- the core and the provider, no Docker.
  2. probe    `ganda <engine> probe` -- every engine fact the oracle rests on.
              A changed fact invalidates the campaign, so this gates it.
  3. campaign `ganda <engine> run` over `--iterations` seeds with every schema axis on.
  4. triage   re-run each seed that produced a bundle, alone, with `--bundle-all`
              and `--explain`, so the finding is isolated and every variant's plan
              fingerprint is on record.

Findings land in `bugs/<tag>-<seed>-<generator>-<shape>/`, the counters in
`campaign_reports/<tag>-campaign_stats.json`, this driver's log in
`campaign_reports/<tag>-campaign.log`.

Exit code: 0 clean, 1 the campaign found something, 2 a phase failed to run.

    python3 scripts/campaign.py --tag c200 --seed 2000 --iterations 100 --workers 2
    python3 scripts/campaign.py --engine orientdb --tag quick --iterations 5 --skip-probe
    python3 scripts/campaign.py --tag c200 --dry-run
    ORIENT_URL=http://localhost:2480 python3 scripts/campaign.py --tag live
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every schema axis on. Each defaults to 0 in the configuration so that an older seed
# reproduces; a campaign wants them all exercised.
CORE_AXES = {
    "custom-key-probability": 0.3,
    "inheritance-probability": 0.5,
    "narrow-type-probability": 0.3,
    "date-map-probability": 0.5,
    "collate-probability": 0.4,
    "list-index-probability": 0.5,
    "churn-edge-update-probability": 0.2,
}
# the axes only one engine's configuration has
ENGINE_AXES = {"orientdb": {"clusters-probability": 0.3}}


class Log:
    def __init__(self, path: str, enabled: bool = True):
        self.fh = open(path, "a", buffering=1) if enabled else None

    def __call__(self, line: str = "") -> None:
        print(line, flush=True)
        if self.fh:
            self.fh.write(line + "\n")

    def banner(self, text: str) -> None:
        self("")
        self("=" * 72)
        self(text)
        self("=" * 72)


def run(cmd: list, log: Log, dry: bool) -> int:
    log(f"$ {' '.join(cmd)}")
    if dry:
        return 0
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        log(line.rstrip("\n"))
    return proc.wait()


def bundle_names(out_dir: str, tag: str) -> list:
    """Every bundle directory carrying this tag.

    A query finding is `<tag>-<seed>-<template>-<shape>`; a setup problem or a server
    crash is `<tag>-<seed>-<kind>`, so one regex anchored on the tag catches all of
    them."""
    pattern = re.compile(rf"^{re.escape(tag)}-\d+-")
    d = os.path.join(ROOT, out_dir)
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d) if pattern.match(n))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", default="orientdb", help="the provider to run (`ganda list`)")
    ap.add_argument("--tag", required=True, help="run tag: names every bundle and the stats file")
    ap.add_argument("--seed", type=int, default=1, help="first seed (the campaign runs --iterations consecutive seeds)")
    ap.add_argument("--iterations", type=int, default=100, help="instances, one per seed")
    ap.add_argument("--workers", type=int, default=2, help="parallel workers; each gets its own three databases")
    ap.add_argument("--traversal-cases", type=int, default=None, help="sampled traversal questions per instance (default: the configuration's)")
    ap.add_argument("--scale", default=None, help="the scale profile: `large` is the storage-engine stress mode (no reference leg, sampled state check)")
    ap.add_argument("--out-dir", default="bugs")
    ap.add_argument("--stats-dir", default="campaign_reports")
    ap.add_argument("--max-findings", type=int, default=0, help="stop after this many new keys (0: run every seed)")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--skip-probe", action="store_true", help="skip the engine-fact probe (it needs the server)")
    ap.add_argument("--skip-triage", action="store_true")
    ap.add_argument("--no-known-bugs", action="store_true", help="also run the variants a filed bug covers")
    ap.add_argument("--explain", action="store_true", help="fingerprint plans during the campaign too, not only in triage")
    ap.add_argument("--dry-run", action="store_true", help="print the commands and exit")
    ap.add_argument("extra", nargs="*", help="extra flags passed through to `ganda <engine> run`, after --")
    args = ap.parse_args(argv)
    AXES = {**CORE_AXES, **ENGINE_AXES.get(args.engine, {})}

    stats_dir = os.path.join(ROOT, args.stats_dir)
    os.makedirs(stats_dir, exist_ok=True)
    log = Log(os.path.join(stats_dir, f"{args.tag}-campaign.log"), enabled=not args.dry_run)
    started = time.time()
    seeds = f"{args.seed}..{args.seed + args.iterations - 1}"
    log.banner(f"campaign {args.tag}  seeds {seeds}  workers {args.workers}  started {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if os.environ.get("ORIENT_URL"):
        log(f"attaching to the server at {os.environ['ORIENT_URL']} instead of Docker")

    py = sys.executable
    pre_existing = set(bundle_names(args.out_dir, args.tag))
    if pre_existing:
        log(f"warning: {len(pre_existing)} bundle(s) already carry the tag {args.tag}; triage covers the new ones only")

    if not args.skip_tests:
        log.banner("phase 1/4  unit tests")
        if run([py, "-m", "pytest", "tests/", "-q"], log, args.dry_run) != 0:
            log("tests failed: the reference or a renderer is broken, so a campaign would report itself. Stopping.")
            return 2

    if not args.skip_probe:
        log.banner("phase 2/4  engine facts")
        if run([py, "-m", "ganda", args.engine, "probe"], log, args.dry_run) != 0:
            log("a probed engine fact changed: the oracle's assumptions no longer hold on this build.")
            log("read the probe output above, update the fact and the guard, then re-run. Stopping.")
            return 2

    log.banner("phase 3/4  campaign")
    cmd = [py, "-m", "ganda", args.engine, "run",
           "--iterations", str(args.iterations),
           "--seed", str(args.seed),
           "--workers", str(args.workers),
           "--out-dir", args.out_dir,
           "--stats-dir", args.stats_dir,
           "--run-tag", args.tag]
    if args.traversal_cases is not None:
        cmd += ["--traversal-cases", str(args.traversal_cases)]
    if args.scale:
        cmd += ["--scale", args.scale]
    if args.max_findings:
        cmd += ["--max-findings", str(args.max_findings)]
    if args.no_known_bugs:
        cmd.append("--no-known-bugs")
    if args.explain:
        cmd.append("--explain")
    for flag, value in AXES.items():
        cmd += [f"--{flag}", str(value)]
    cmd += args.extra
    rc = run(cmd, log, args.dry_run)
    if rc != 0:
        log(f"the runner exited {rc}: it did not finish, so the counters below are incomplete.")

    new_bundles = [n for n in bundle_names(args.out_dir, args.tag) if n not in pre_existing]
    seed_of = re.compile(rf"^{re.escape(args.tag)}-(\d+)-")
    found = sorted({int(m.group(1)) for n in new_bundles if (m := seed_of.match(n))})

    if not args.skip_triage and found and not args.dry_run:
        log.banner(f"phase 4/4  triage {len(found)} seed(s): {found}")
        for seed in found:
            log(f"--- seed {seed} alone, every occurrence bundled, plans fingerprinted")
            triage = [py, "-m", "ganda", args.engine, "run",
                      "--iterations", "1", "--seed", str(seed), "--workers", "1",
                      "--out-dir", args.out_dir,
                      "--stats-dir", args.stats_dir,
                      "--run-tag", f"{args.tag}t{seed}",
                      "--bundle-all", "--explain"]
            if args.traversal_cases is not None:
                triage += ["--traversal-cases", str(args.traversal_cases)]
            if args.scale:
                triage += ["--scale", args.scale]
            for flag, value in AXES.items():
                triage += [f"--{flag}", str(value)]

            triage += args.extra
            run(triage, log, args.dry_run)
    elif args.dry_run:
        log.banner("phase 4/4  triage")
        log("(triage re-runs each seed that bundled; nothing to show on a dry run)")

    log.banner(f"campaign {args.tag} done in {time.time() - started:.0f}s")
    stats_path = os.path.join(stats_dir, f"{args.tag}-campaign_stats.json")
    if os.path.exists(stats_path) and not args.dry_run:
        with open(stats_path) as fh:
            stats = json.load(fh)
        pct = 100.0 * stats["cases_asserting"] / stats["cases"] if stats["cases"] else 0.0
        log(f"instances {stats['instances']}  cases {stats['cases']}  asserting {pct:.1f}%")
        log(f"new finding keys {stats['findings']}  repeats {stats['findings_repeated']}  by class {stats['per_class']}")
        log(f"setup asymmetries {stats['setup_asymmetries']}  internal errors {stats['internal_errors']}  crashes {stats['crashes']}  state leaks {stats['state_leaks']}")
        log(f"distinct shapes {stats['distinct_shapes']}  traversal shapes {stats['traversal_shapes_visited']}  plan pairs {stats['distinct_plan_pairs']}")
        log(f"stats: {os.path.relpath(stats_path, ROOT)}")
    log(f"log:   {os.path.relpath(log.fh.name, ROOT) if log.fh else '(none)'}")
    if new_bundles:
        log(f"bundles ({len(new_bundles)}) under {args.out_dir}/ -- read failing_pairs in queries.json first:")
        for name in new_bundles:
            log(f"  {args.out_dir}/{name}")
        log(f"replay one with: python3 {args.out_dir}/<bundle>/repro.py")
        return 1
    log("no bundles: the campaign is clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
