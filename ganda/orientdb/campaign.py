"""The campaign driver: for each seed, generate an instance, materialize it three ways
into fresh databases on one OrientDB server, verify the state, run every case, and
write a bundle for every NEW finding key (`dedup.BugDeduplicator`). Crashes are
autopsied and the server is restarted. `--workers N` runs N seed shards as threads
against the same server (the work is HTTP-bound).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import threading
import time
from collections import Counter
from typing import NamedTuple

import ganda.orientdb.coverage as coverage
import ganda.orientdb.known_bugs as guards
from ganda.orientdb.generate import GeneratorConfig, build
from ganda.orientdb.connection import OrientCrash, OrientError, OrientServer, cleanup_orphans, db_name
from ganda.orientdb.mapping import MAPPERS
from ganda.core.oracle import is_empty_value, run_cases
from ganda.orientdb.connection import is_script
from ganda.core.dedup import BugDeduplicator
from ganda.core.ast.traversal import SHAPE_DENOMINATOR
from ganda.core.ast.path import COVERAGE_DENOMINATOR as PATH_COVERAGE_DENOMINATOR
from ganda.core.ast.select import COVERAGE_DENOMINATOR as SELECT_COVERAGE_DENOMINATOR
from ganda.core.ast.write import COVERAGE_DENOMINATOR as WRITE_COVERAGE_DENOMINATOR
from ganda.core.generators.registry import all_cases
from ganda.orientdb.reporter import report_bug, report_crash, report_setup_problem
from ganda.core.state_check import verify_state


class CampaignStats(NamedTuple):
    instances: int
    setup_failures: int  # every data model rejected the setup: consistent rejection, skipped
    setup_asymmetries: int  # some data models rejected the setup, or one side's state differs
    cases: int
    cases_asserting: int
    symmetric_errors: int
    internal_errors: int
    findings: int  # new finding keys (bundled)
    findings_repeated: int  # occurrences of an already-bundled key
    crashes: int
    state_leaks: int  # an instance whose live state differed AFTER the cases ran (a transaction door leaked past its ROLLBACK)
    known_bugs_suppressed: dict
    values_compared: int
    values_nonempty: int
    per_generator: dict
    per_class: dict
    distinct_shapes: int
    traversal_shapes_visited: int
    select_shapes_visited: int
    path_shapes_visited: int
    writes: int
    write_state_diffs: int
    write_shapes_visited: int
    distinct_plan_pairs: int
    elapsed: float
    coverage: "dict | None" = None  # `coverage.Collector.summary()` under --coverage


class _Shared:
    def __init__(self):
        self.lock = threading.Lock()
        self.dedup = BugDeduplicator()
        self.per_generator: dict = {}
        self.per_class: Counter = Counter()
        self.known_bugs: Counter = Counter()
        self.shapes: set = set()
        self.traversal_shapes: set = set()
        self.select_shapes: set = set()
        self.path_shapes: set = set()
        self.write_shapes: set = set()
        self.plan_pairs: set = set()
        self.counters: Counter = Counter()
        self.stop = False
        self.coverage: "coverage.Collector | None" = None


def _tpl(shared: _Shared, name: str) -> dict:
    return shared.per_generator.setdefault(name, {"cases": 0, "asserting": 0, "findings": 0, "symmetric_errors": 0, "internal_errors": 0, "suppressed": 0})


def run_instance(server: OrientServer, seed: int, cfg: GeneratorConfig, args, shared: _Shared, ns: str) -> None:
    schema, instance, rng = build(seed, cfg)
    plans = {data_model: MAPPERS[data_model](instance) for data_model in ("doc", "graph", "rel")}
    sessions: dict = {}
    setup_errors: dict = {}
    diffs: dict = {}
    for data_model, plan in plans.items():
        s = server.fresh_session(db_name(ns, data_model))
        try:
            s.apply_setup(plan)
            setup_errors[data_model] = ""
            sessions[data_model] = s
            if args.verify_state:
                diffs[data_model] = verify_state(s, data_model, instance)
        except OrientError as e:
            setup_errors[data_model] = str(e)
    cov = shared.coverage
    if cov:
        cov.after_setup()
    with shared.lock:
        shared.counters["instances"] += 1
    failed = [s for s, e in setup_errors.items() if e]
    bad_state = [s for s, d in diffs.items() if not d.ok]
    if failed or bad_state:
        with shared.lock:
            if len(failed) == 3:
                shared.counters["setup_failures"] += 1
                kind = "setup-failure"
            else:
                shared.counters["setup_asymmetries"] += 1
                kind = "setup-asymmetry" if failed else "state-diff"
        if len(failed) < 3 or args.bundle_setup_failures:
            report_setup_problem(args.out_dir, args.run_tag, seed, cfg, instance, plans, setup_errors, diffs, kind)
            print(f"[seed {seed}] {kind}: {failed or bad_state}", flush=True)
        _drop(server, ns)
        return

    cases = all_cases(rng, schema, instance, traversal_cases=cfg.traversal_cases, select_cases=cfg.select_cases, path_cases=cfg.path_cases, card_cases=cfg.card_cases, card_corpus=cfg.card_corpus)
    hooks = {"after_case": cov.after_case, "after_variant": cov.after_variant} if cov else {}
    results = run_cases(cases, sessions, instance, explain=args.explain, keep_known_bugs=args.no_known_bugs, **hooks)
    if args.verify_state and any(is_script(t) for r in results for t in r.case.queries.values()):
        # the transaction door rolled every write back: the live state must still be
        # the Instance on every data model
        after = {data_model: verify_state(s, data_model, instance) for data_model, s in sessions.items()}
        leaked = [data_model for data_model, d in after.items() if not d.ok]
        if leaked:
            with shared.lock:
                shared.counters["state_leaks"] += 1
            d = report_setup_problem(args.out_dir, args.run_tag, seed, cfg, instance, plans, {data_model: "" for data_model in sessions}, after, "state-leak")
            print(f"[seed {seed}] state-leak after the cases: {leaked} -> {d}", flush=True)
    if cfg.write_cases:
        # the write phase: predicate-targeted writes after every read, each checked
        # and applied to the Instance (`ganda.core.generators.write_phase`)
        from ganda.core.generators.write_phase import run_write_phase

        wp = run_write_phase(sessions, instance, rng.for_generator("ir_write"), cfg.write_cases, verify=args.verify_state, keep_known_bugs=args.no_known_bugs)
        results += wp.results
        with shared.lock:
            shared.counters["writes"] += sum(1 for r in wp.results if r.case.generator_name == "ir_write")
            for r in wp.results:
                if r.case.generator_name == "ir_write":
                    shared.write_shapes.add(r.case.params["coverage"])
        if any(not d.ok for d in wp.diffs.values()):
            with shared.lock:
                shared.counters["write_state_diffs"] += 1
            last = next((r.case for r in reversed(wp.results) if r.case.generator_name == "ir_write"), None)
            wrote = {data_model: f"[{last.params['concrete_syntax'][data_model]}] {last.params['statements'][data_model]}" for data_model in sessions} if last else {data_model: "" for data_model in sessions}
            d = report_setup_problem(args.out_dir, args.run_tag, seed, cfg, wp.instance, plans, wrote, wp.diffs, f"write-state-{wp.failed_step}", write_step=wp.failed_step)
            print(f"[seed {seed}] write-state after {wp.stopped} -> {d}", flush=True)
    with shared.lock:
        for r in results:
            t = _tpl(shared, r.case.generator_name)
            t["cases"] += 1
            shared.counters["cases"] += 1
            shared.shapes.add(r.case.shape)
            if r.case.generator_name == "ir_traversal":
                shared.traversal_shapes.add(r.case.shape)
            elif r.case.generator_name == "ir_select":
                shared.select_shapes.add(r.case.params["coverage"])
            elif r.case.generator_name == "ir_path":
                shared.path_shapes.add(r.case.params["coverage"])
            for data_model, fid in r.suppressed.items():
                shared.known_bugs[fid] += 1
                t["suppressed"] += 1
            if r.plans:
                shared.plan_pairs.add((r.case.shape, tuple(sorted((s, tuple(p)) for s, p in r.plans.items()))))
            for v in r.compared_values():
                shared.counters["values_compared"] += 1
                if not is_empty_value(v):
                    shared.counters["values_nonempty"] += 1
            if r.asserts_anything:
                t["asserting"] += 1
                shared.counters["cases_asserting"] += 1
            if r.symmetric_error and not r.internal_error:
                t["symmetric_errors"] += 1
                shared.counters["symmetric_errors"] += 1
            if r.internal_error:
                t["internal_errors"] += 1
                shared.counters["internal_errors"] += 1
    for r in results:
        if r.ok:
            continue
        first, n = shared.dedup.admit(r)
        with shared.lock:
            shared.per_class[r.finding_class] += 1
            _tpl(shared, r.case.generator_name)["findings"] += 1
            if first:
                shared.counters["findings"] += 1
            else:
                shared.counters["findings_repeated"] += 1
            if args.max_findings and shared.counters["findings"] >= args.max_findings:
                shared.stop = True
        if first or args.bundle_all:
            d = report_bug(args.out_dir, args.run_tag, seed, cfg, instance, plans, r, times_seen=n)
            print(f"[seed {seed}] {r.finding_class} {r.case.generator_name} {r.case.shape} pairs={r.failing_pairs} -> {d}", flush=True)
    _drop(server, ns)


def _drop(server: OrientServer, ns: str) -> None:
    for data_model in ("doc", "graph", "rel"):
        try:
            server.drop_database(db_name(ns, data_model))
        except (OrientError, OrientCrash):
            pass


def _worker(worker_id: int, seeds: list, cfg: GeneratorConfig, args, shared: _Shared, server_box: dict) -> None:
    ns = f"w{os.getpid()}_{worker_id}"
    for seed in seeds:
        if shared.stop:
            return
        for attempt in range(2):
            try:
                run_instance(server_box["server"], seed, cfg, args, shared, ns)
                break
            except OrientCrash as e:
                with shared.lock:
                    shared.counters["crashes"] += 1
                    server = server_box["server"]
                    diag = server.diagnostics()
                    report_crash(args.out_dir, args.run_tag, seed, cfg, str(e), diag)
                    print(f"[seed {seed}] CRASH: {e}", flush=True)
                    if not server.external:
                        if shared.coverage:
                            shared.coverage.before_restart()
                        server.stop()
                        server.start()
                if attempt == 1:
                    break
        if shared.coverage:
            shared.coverage.after_seed(seed)


def run_campaign(args) -> CampaignStats:
    cfg = GeneratorConfig(**{k: v for k, v in vars(args).items() if k in {f.name for f in dataclasses.fields(GeneratorConfig)}})
    guards.DICTIONARY_DUPLICATES = args.keep_dictionary_duplicates
    if args.keep_dictionary_duplicates:
        cfg = cfg.with_(demote_colliding_indexes=False)
    shared = _Shared()
    seeds = list(range(args.seed, args.seed + args.iterations))
    start = time.time()
    os.makedirs(args.out_dir, exist_ok=True)
    if args.coverage in ("case", "variant") and args.workers > 1:
        raise SystemExit(f"--coverage {args.coverage} attributes each window to what ran since the last dump: it needs --workers 1")
    with OrientServer(coverage=args.coverage != "off") as server:
        if args.coverage != "off":
            shared.coverage = coverage.Collector(server, args.coverage)
        box = {"server": server}
        if args.workers <= 1:
            _worker(0, seeds, cfg, args, shared, box)
        else:
            shards = [seeds[i :: args.workers] for i in range(args.workers)]
            threads = [threading.Thread(target=_worker, args=(i, shard, cfg, args, shared, box), daemon=True) for i, shard in enumerate(shards)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
    if shared.coverage:
        shared.coverage.write_exec(args.stats_dir, args.run_tag)
    c = shared.counters
    return CampaignStats(
        instances=c["instances"],
        setup_failures=c["setup_failures"],
        setup_asymmetries=c["setup_asymmetries"],
        cases=c["cases"],
        cases_asserting=c["cases_asserting"],
        symmetric_errors=c["symmetric_errors"],
        internal_errors=c["internal_errors"],
        findings=c["findings"],
        findings_repeated=c["findings_repeated"],
        crashes=c["crashes"],
        state_leaks=c["state_leaks"],
        known_bugs_suppressed=dict(shared.known_bugs),
        values_compared=c["values_compared"],
        values_nonempty=c["values_nonempty"],
        per_generator=shared.per_generator,
        per_class=dict(shared.per_class),
        distinct_shapes=len(shared.shapes),
        traversal_shapes_visited=len(shared.traversal_shapes),
        select_shapes_visited=len(shared.select_shapes),
        path_shapes_visited=len(shared.path_shapes),
        writes=shared.counters["writes"],
        write_state_diffs=shared.counters["write_state_diffs"],
        write_shapes_visited=len(shared.write_shapes),
        distinct_plan_pairs=len(shared.plan_pairs),
        elapsed=time.time() - start,
        coverage=shared.coverage.summary() if shared.coverage else None,
    )


def print_stats(stats: CampaignStats, dedup_summary: "dict | None" = None) -> None:
    print("=" * 72)
    print(f"instances: {stats.instances}  setup failures: {stats.setup_failures}  setup asymmetries: {stats.setup_asymmetries}  crashes: {stats.crashes}  state leaks: {stats.state_leaks}")
    pct = 100.0 * stats.cases_asserting / stats.cases if stats.cases else 0.0
    print(f"cases: {stats.cases}  asserting: {stats.cases_asserting} ({pct:.1f}%)  symmetric errors: {stats.symmetric_errors}  internal errors: {stats.internal_errors}")
    print(f"findings: {stats.findings} new keys, {stats.findings_repeated} repeats  by class: {stats.per_class}")
    print(f"known findings withheld: {stats.known_bugs_suppressed}")
    print(f"distinct shapes: {stats.distinct_shapes}  ir_traversal shape coverage: {stats.traversal_shapes_visited}/{SHAPE_DENOMINATOR}  ir_select coverage: {stats.select_shapes_visited}/{SELECT_COVERAGE_DENOMINATOR}  ir_path coverage: {stats.path_shapes_visited}/{PATH_COVERAGE_DENOMINATOR}  distinct plan pairs: {stats.distinct_plan_pairs}")
    print(f"writes: {stats.writes}  write-state diffs: {stats.write_state_diffs}  ir_write coverage: {stats.write_shapes_visited}/{WRITE_COVERAGE_DENOMINATOR}")
    print(f"elapsed: {stats.elapsed:.1f}s")
    for name, t in sorted(stats.per_generator.items()):
        a = 100.0 * t["asserting"] / t["cases"] if t["cases"] else 0.0
        print(f"  {name:26s} cases={t['cases']:5d} asserting={a:5.1f}% findings={t['findings']} symerr={t['symmetric_errors']} internal={t['internal_errors']} withheld={t['suppressed']}")
    if dedup_summary:
        print("finding keys:")
        for k, v in list(dedup_summary.items())[:30]:
            print(f"  {v:4d}x {k}")
    if stats.coverage:
        coverage.print_summary(stats.coverage)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OrientDB multi-model metamorphic fuzzer")
    ap.add_argument("--iterations", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--out-dir", default="bugs")
    ap.add_argument("--run-tag", default="run")
    ap.add_argument("--stats-dir", default="campaign_reports")
    ap.add_argument("--max-findings", type=int, default=0)
    ap.add_argument("--explain", action="store_true", help="fingerprint every side's plan (extra round trips)")
    ap.add_argument("--no-verify-state", dest="verify_state", action="store_false")
    ap.add_argument("--no-known-bugs", action="store_true", help="run the sides that match a filed finding")
    ap.add_argument("--keep-dictionary-duplicates", action="store_true")
    ap.add_argument("--bundle-all", action="store_true", help="bundle every finding, not only the first per key")
    ap.add_argument("--bundle-setup-failures", action="store_true")
    ap.add_argument("--coverage", choices=coverage.LEVELS, default="off",
                    help="run the server under the JaCoCo agent: seed = the growth curve; case / side = also attribute "
                         "each probe to the template / the template's side that hit it (--workers 1)")
    for f in dataclasses.fields(GeneratorConfig):
        flag = "--" + f.name.replace("_", "-")
        if f.type is bool or isinstance(f.default, bool):
            ap.add_argument(flag, type=lambda s: s.lower() in ("1", "true", "yes"), default=f.default)
        else:
            ap.add_argument(flag, type=type(f.default), default=f.default)
    args = ap.parse_args(argv)
    try:
        stats = run_campaign(args)
    finally:
        cleanup_orphans()
    print_stats(stats)
    os.makedirs(args.stats_dir, exist_ok=True)
    record = stats._asdict()
    if stats.coverage:
        cov_path = coverage.write_summary(stats.coverage, args.stats_dir, args.run_tag)
        record["coverage"] = {**{k: stats.coverage[k] for k in coverage.HEADLINE}, "json": cov_path}
        print(f"coverage written to {cov_path} (execution data: {args.run_tag}-coverage.exec; scripts/coverage_report.py reads it)")
    path = os.path.join(args.stats_dir, f"{args.run_tag}-campaign_stats.json")
    with open(path, "w") as fh:
        json.dump({**record, "args": {k: v for k, v in vars(args).items()}}, fh, indent=2, default=repr)
    print(f"stats written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
