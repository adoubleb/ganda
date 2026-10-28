from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import NamedTuple

from ganda.core.data import scale as _scale

from ganda.core.query.ir.path import COVERAGE_DENOMINATOR as PATH_COVERAGE_DENOMINATOR
from ganda.core.query.ir.select import COVERAGE_DENOMINATOR as SELECT_COVERAGE_DENOMINATOR
from ganda.core.query.ir.traversal import SHAPE_DENOMINATOR
from ganda.core.query.ir.write import COVERAGE_DENOMINATOR as WRITE_COVERAGE_DENOMINATOR
from ganda.core.campaign.config import add_config_arguments, config_from_args
from ganda.core.engine.coverage import measured_server
from ganda.core.data.generator import GeneratorConfig, build, limits_of
from ganda.core.oracle.dedup import BugDeduplicator
from ganda.core.query.generators.registry import all_cases
from ganda.core.query.generators.write_phase import run_write_phase
from ganda.core.oracle.oracle import is_empty_value, run_cases
from ganda.core.engine.provider import EngineCrash, EngineError
from ganda.core.campaign.reporter import _jsonable, report_bug, report_crash, report_setup_problem
from ganda.core.engine.state_check import compare_states


class CampaignStats(NamedTuple):
    instances: int
    setup_failures: int
    setup_asymmetries: int 
    cases: int
    cases_asserting: int
    symmetric_errors: int
    internal_errors: int
    findings: int 
    findings_repeated: int 
    crashes: int
    state_leaks: int 
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
    coverage: "dict | None" = None  # the provider's coverage summary under --coverage
    timeouts: int = 0  # variants that gave no answer in time (left out of their case's comparison)
    scale: str = "default"
    state_sample: int = 0  # 0: the stored state was read whole
    cases_partial: int = 0  # cases some data model had no concrete syntax for (compared among the others)
    # under --no-known-bugs: per defect, the compared pairs it alone marks (one side or
    # both, no other defect) and those that failed (a mismatch, an error on one side
    # only, an internal error, duplicate ids) -- a defect whose pairs never fail needs no
    # guard here; failing pairs two defects mark, and failing pairs no defect marks
    known_bugs_run: dict = {}
    known_bugs_exposed: dict = {}
    failing_pairs_mixed: int = 0
    failing_pairs_unmarked: int = 0


class _Shared:
    def __init__(self):
        self.lock = threading.Lock()
        self.dedup = BugDeduplicator()
        self.per_generator: dict = {}
        self.per_class: Counter = Counter()
        self.known_bugs: Counter = Counter()
        self.known_run: Counter = Counter()
        self.known_exposed: Counter = Counter()
        self.shapes: set = set()
        self.traversal_shapes: set = set()
        self.select_shapes: set = set()
        self.path_shapes: set = set()
        self.write_shapes: set = set()
        self.plan_pairs: set = set()
        self.counters: Counter = Counter()
        self.stop = False
        self.deadline = 0.0  # --duration-hours
        self.coverage = None  # the provider's `coverage.Collector`


def _tpl(shared: _Shared, name: str) -> dict:
    return shared.per_generator.setdefault(name, {"cases": 0, "asserting": 0, "findings": 0, "symmetric_errors": 0, "internal_errors": 0, "suppressed": 0})


def _sample_rng(seed: int) -> random.Random:
    """The state check's sample for one instance: a fresh stream per check, so the
    setup's check and a re-check after the cases read the same records."""
    return random.Random(f"{seed}:statesample")


@dataclass
class _SeedRun:
    engine: object
    server: object
    seed: int
    cfg: GeneratorConfig
    args: argparse.Namespace
    shared: _Shared
    ns: str
    instance: object
    plans: dict

    def check_state(self, sessions: dict) -> dict:
        return compare_states(sessions, self.instance, sample=self.args.state_sample, rng=_sample_rng(self.seed))

    def count(self, counter: str) -> None:
        with self.shared.lock:
            self.shared.counters[counter] += 1

    def report_state_problem(self, errors: dict, diffs: dict, kind: str, instance=None, **kw) -> str:
        if getattr(self.args, "no_bundles", False):
            return "(no bundle: --no-bundles)"
        instance = self.instance if instance is None else instance
        return report_setup_problem(self.engine, self.args.out_dir, self.args.run_tag, self.seed, self.cfg, instance, self.plans, errors, diffs, kind, embed_data=self.args.embed_data, **kw)


def run_instance(engine, server, seed: int, cfg: GeneratorConfig, args, shared: _Shared, ns: str) -> None:
    schema, instance, rng = build(seed, cfg, engine)
    plans = {data_model: engine.mappers[data_model](instance) for data_model in engine.data_models}
    run = _SeedRun(engine, server, seed, cfg, args, shared, ns, instance, plans)
    sessions = _set_up(run)
    if sessions is not None:
        results = _read_phase(run, sessions, schema, rng)
        if cfg.write_cases:
            results += _write_phase(run, sessions, rng)
        _tally(shared, results)
        if args.no_known_bugs and not getattr(args, "no_bundles", False):
            _log_failing_pairs(run, results)
        _report_findings(run, results)
    _drop(engine, server, ns)


def _set_up(run: _SeedRun) -> "dict | None":
    sessions: dict = {}
    errors: dict = {}
    diffs: dict = {}
    for data_model, plan in run.plans.items():
        s = run.server.fresh_session(run.engine.database_name(run.ns, data_model))
        try:
            s.apply_setup(plan)
            errors[data_model] = ""
            sessions[data_model] = s
        except EngineError as e:
            errors[data_model] = str(e)
    if run.args.verify_state:
        diffs = run.check_state(sessions)
    if run.shared.coverage:
        run.shared.coverage.after_setup()
    run.count("instances")

    failed = [data_model for data_model, e in errors.items() if e]
    bad_state = [data_model for data_model, d in diffs.items() if not d.ok]
    if failed or bad_state:
        _report_setup(run, errors, diffs, failed, bad_state)
        return None
    return sessions


def _report_setup(run: _SeedRun, errors: dict, diffs: dict, failed: list, bad_state: list) -> None:
    every_data_model_failed = len(failed) == len(run.engine.data_models)
    with run.shared.lock:
        if every_data_model_failed:
            run.shared.counters["setup_failures"] += 1
            kind = "setup-failure"
        else:
            run.shared.counters["setup_asymmetries"] += 1
            kind = "setup-asymmetry" if failed else "state-diff"
    if not every_data_model_failed or run.args.bundle_setup_failures:
        run.report_state_problem(errors, diffs, kind)
        print(f"[seed {run.seed}] {kind}: {failed or bad_state}", flush=True)


def _read_phase(run: _SeedRun, sessions: dict, schema, rng) -> list:
    args = run.args
    cov = run.shared.coverage
    hooks = {"after_case": cov.after_case, "after_variant": cov.after_variant} if cov else {}
    cases = all_cases(rng, schema, run.instance, cfg=run.cfg)
    results = run_cases(cases, sessions, explain=args.explain, keep_known_bugs=args.no_known_bugs, timeouts_are_errors=args.timeouts_are_errors, repeat_variants=getattr(args, "coverage_repeat", False), **hooks)
    if args.verify_state and any(run.engine.may_write(r.case) for r in results):
        _check_state_leaks(run, sessions)
    return results


def _check_state_leaks(run: _SeedRun, sessions: dict) -> None:
    # a read case that writes inside a transaction rolled it back: the data models
    # must still hold one state
    after = run.check_state(sessions)
    leaked = [data_model for data_model, d in after.items() if not d.ok]
    if leaked:
        run.count("state_leaks")
        d = run.report_state_problem({data_model: "" for data_model in sessions}, after, "state-leak")
        print(f"[seed {run.seed}] state-leak after the cases: {leaked} -> {d}", flush=True)


def _write_phase(run: _SeedRun, sessions: dict, rng) -> list:
    # predicate-targeted writes after every read, each checked and applied to the
    # Instance (`ganda.core.query.generators.write_phase`)
    args = run.args
    wp = run_write_phase(sessions, run.instance, rng.for_generator("ir_write"), run.cfg.write_cases, verify=args.verify_state, keep_known_bugs=args.no_known_bugs, state_sample=args.state_sample, state_rng=lambda: _sample_rng(run.seed), timeouts_are_errors=args.timeouts_are_errors)
    writes = [r.case for r in wp.results if r.case.generator_name == "ir_write"]
    with run.shared.lock:
        run.shared.counters["writes"] += len(writes)
        run.shared.write_shapes.update(case.params["coverage"] for case in writes)
    if any(not d.ok for d in wp.diffs.values()):
        run.count("write_state_diffs")
        last = writes[-1] if writes else None
        wrote = {data_model: f"[{last.params['concrete_syntax'][data_model]}] {last.params['statements'][data_model]}" if last else "" for data_model in sessions}
        d = run.report_state_problem(wrote, wp.diffs, f"write-state-{wp.failed_step}", instance=wp.instance, write_step=wp.failed_step, keep_known_bugs=args.no_known_bugs)
        print(f"[seed {run.seed}] write-state after {wp.stopped} -> {d}", flush=True)
    return wp.results


def _tally(shared: _Shared, results: list) -> None:
    with shared.lock:
        for r in results:
            _tally_case(shared, r)


def _tally_case(shared: _Shared, r) -> None:
    case, counters = r.case, shared.counters
    t = _tpl(shared, case.generator_name)
    t["cases"] += 1
    counters["cases"] += 1
    if case.params.get("partial"):
        counters["cases_partial"] += 1
    shared.shapes.add(case.shape)
    if case.generator_name == "ir_traversal":
        shared.traversal_shapes.add(case.shape)
    elif case.generator_name == "ir_select":
        shared.select_shapes.add(case.params["coverage"])
    elif case.generator_name == "ir_path":
        shared.path_shapes.add(case.params["coverage"])
    for fid in r.suppressed.values():
        shared.known_bugs[fid] += 1
        t["suppressed"] += 1
    for a, b, failed in _compared_pairs(r):
        marks = {case.known_bugs[v] for v in (a, b) if v in case.known_bugs}
        if len(marks) == 1:
            (fid,) = marks
            shared.known_run[fid] += 1
            shared.known_exposed[fid] += failed
        elif failed:
            counters["failing_pairs_mixed" if marks else "failing_pairs_unmarked"] += 1
    counters["timeouts"] += len(r.timeouts)
    if r.plans:
        shared.plan_pairs.add((case.shape, tuple(sorted((s, tuple(p)) for s, p in r.plans.items()))))
    for v in r.compared_values():
        counters["values_compared"] += 1
        if not is_empty_value(v):
            counters["values_nonempty"] += 1
    if r.asserts_anything:
        t["asserting"] += 1
        counters["cases_asserting"] += 1
    if r.symmetric_error and not r.internal_error:
        t["symmetric_errors"] += 1
        counters["symmetric_errors"] += 1
    if r.internal_error:
        t["internal_errors"] += 1
        counters["internal_errors"] += 1


def _compared_pairs(r) -> list:
    """(a, b, failed) for every pair the result compared: failed on a mismatch, an error
    on one side only, an internal error or duplicate ids on either side."""
    from ganda.core.oracle.case import comparable_pairs

    mismatched = {(m.left, m.right) for m in r.mismatches}
    bad = {v for v, internal in r.internal.items() if internal} | {d.variant for d in r.duplicate_id_rows}
    out = []
    for a, b in comparable_pairs(r.case, r.variants):
        failed = (a, b) in mismatched or bool(r.errors.get(a)) != bool(r.errors.get(b)) or a in bad or b in bad
        out.append((a, b, failed))
    return out


def _log_failing_pairs(run: _SeedRun, results: list) -> None:
    """Under --no-known-bugs, every failing compared pair -- the defects marking its
    sides, both texts, binds, answers and errors -- appended to
    `<stats dir>/<tag>-failing-pairs.jsonl`, to read which defect explains which pair."""
    out = []
    for r in results:
        for a, b, failed in _compared_pairs(r):
            if not failed:
                continue
            side = lambda v: {"variant": v, "text": r.case.queries.get(v), "known": r.case.known_bugs.get(v), "error": (r.errors.get(v) or "")[:400], "rows": _jsonable(r.rows.get(v, []))[:20]}  # noqa: E731
            out.append({"seed": run.seed, "generator": r.case.generator_name, "shape": r.case.shape, "binds": _jsonable(r.case.bind_vars), "marks": sorted({r.case.known_bugs[v] for v in (a, b) if v in r.case.known_bugs}), "a": side(a), "b": side(b)})
    if out:
        path = os.path.join(run.args.stats_dir, f"{run.args.run_tag}-failing-pairs.jsonl")
        with run.shared.lock, open(path, "a") as f:
            for rec in out:
                f.write(json.dumps(rec, default=repr) + "\n")


def _report_findings(run: _SeedRun, results: list) -> None:
    shared, args = run.shared, run.args
    for r in results:
        if r.ok:
            continue
        first, n = shared.dedup.admit(r)
        with shared.lock:
            shared.per_class[r.finding_class] += 1
            _tpl(shared, r.case.generator_name)["findings"] += 1
            shared.counters["findings" if first else "findings_repeated"] += 1
            if args.max_findings and shared.counters["findings"] >= args.max_findings:
                shared.stop = True
        if getattr(args, "no_bundles", False):
            if first:
                print(f"[seed {run.seed}] {r.finding_class} {r.case.generator_name} {r.case.shape} pairs={r.failing_pairs} (no bundle)", flush=True)
        elif first or args.bundle_all:
            d = report_bug(run.engine, args.out_dir, args.run_tag, run.seed, run.cfg, run.instance, run.plans, r, times_seen=n, embed_data=args.embed_data, max_value_chars=args.max_value_chars)
            print(f"[seed {run.seed}] {r.finding_class} {r.case.generator_name} {r.case.shape} pairs={r.failing_pairs} -> {d}", flush=True)


def _drop(engine, server, ns: str) -> None:
    for data_model in engine.data_models:
        try:
            server.drop_database(engine.database_name(ns, data_model))
        except (EngineError, EngineCrash):
            pass


def _worker(engine, worker_id: int, seeds: list, cfg: GeneratorConfig, args, shared: _Shared, server_box: dict) -> None:
    ns = f"w{os.getpid()}_{worker_id}"
    for seed in seeds:
        if shared.stop:
            return
        if shared.deadline and time.time() >= shared.deadline:
            return
        for attempt in range(2):
            try:
                run_instance(engine, server_box["server"], seed, cfg, args, shared, ns)
                break
            except EngineCrash as e:
                with shared.lock:
                    shared.counters["crashes"] += 1
                    server = server_box["server"]
                    diag = server.diagnostics()
                    report_crash(engine, args.out_dir, args.run_tag, seed, cfg, str(e), diag)
                    print(f"[seed {seed}] CRASH: {e}", flush=True)
                    if not server.external:
                        if shared.coverage:
                            shared.coverage.before_restart()
                        server.stop()
                        server.start()
                if attempt == 1:
                    break
            except EngineError as e:
                # an engine error outside any compared statement (resetting a database,
                # say): this seed is lost, but the worker goes on to the next one rather
                # than ending its thread for the rest of the campaign
                with shared.lock:
                    shared.counters["seed_errors"] += 1
                print(f"[seed {seed}] SKIPPED: {e}", flush=True)
                _drop(engine, server_box["server"], ns)
                break
        if shared.coverage:
            shared.coverage.after_seed(seed)


def apply_profile(args, cfg) -> None:
    """Fill the campaign settings the scale profile decides wherever the flag was left
    unset (`args.state_sample`, `args.query_timeout_ms`), and the two
    it always decides (`args.embed_data`, `args.max_value_chars`)."""
    p = _scale.profile_of(cfg)
    if getattr(args, "state_sample", None) is None:
        args.state_sample = p.state_sample
    if getattr(args, "query_timeout_ms", None) is None:
        args.query_timeout_ms = p.query_timeout_ms
    args.embed_data = p.embed_data
    args.max_value_chars = p.max_bundle_value_chars
    args.timeouts_are_errors = p.timeouts_are_errors
    if p.name != "default":
        state = "read whole" if not args.state_sample else f"checked by counts and a sample of {args.state_sample}"
        print(f"scale {p.name}: stored state {state}, query timeout {args.query_timeout_ms / 1000:.0f} s ({'a finding' if p.timeouts_are_errors else 'left out of the comparison'}), bundles carry the shape of the data", flush=True)


def _warn_on_unprobed_build(engine, server) -> None:
    """Warn when the server is another build than the one the probe last verified (the
    provider's baseline)."""
    warning = engine.baseline.warning(engine.name, server.version()) if engine.baseline is not None else None
    if warning:
        print(f"warning: {warning}", flush=True)


def run_campaign(engine, args) -> "tuple[CampaignStats, object]":
    cfg = _scale.resolve(engine.configure(args, config_from_args(engine.config_class, args)), limits_of(engine))
    apply_profile(args, cfg) # to switch between small or large data and table size for fuzzing
    shared = _Shared()
    seeds = list(range(args.seed, args.seed + args.iterations))
    start = time.time()
    # --duration-hours: no seed starts after it (the running ones finish)
    shared.deadline = start + args.duration_hours * 3600 if getattr(args, "duration_hours", 0) else 0
    os.makedirs(args.out_dir, exist_ok=True)
    with measured_server(engine, args) as (server, shared.coverage):
        _warn_on_unprobed_build(engine, server)
        box = {"server": server}
        if args.workers <= 1:
            _worker(engine, 0, seeds, cfg, args, shared, box)
        else:
            shards = [seeds[i :: args.workers] for i in range(args.workers)]
            threads = [threading.Thread(target=_worker, args=(engine, i, shard, cfg, args, shared, box), daemon=True) for i, shard in enumerate(shards)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
    c = shared.counters
    stats = CampaignStats(
        instances=c["instances"],
        setup_failures=c["setup_failures"],
        setup_asymmetries=c["setup_asymmetries"],
        cases=c["cases"],
        cases_partial=c["cases_partial"],
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
        timeouts=c["timeouts"],
        scale=cfg.scale,
        state_sample=args.state_sample,
        known_bugs_run=dict(shared.known_run),
        known_bugs_exposed=dict(shared.known_exposed),
        failing_pairs_mixed=c["failing_pairs_mixed"],
        failing_pairs_unmarked=c["failing_pairs_unmarked"],
    )
    return stats, shared.coverage


def print_stats(stats: CampaignStats, dedup_summary: "dict | None" = None, collector=None) -> None:
    print("=" * 72)
    print(f"instances: {stats.instances}  setup failures: {stats.setup_failures}  setup asymmetries: {stats.setup_asymmetries}  crashes: {stats.crashes}  state leaks: {stats.state_leaks}")
    pct = 100.0 * stats.cases_asserting / stats.cases if stats.cases else 0.0
    print(f"cases: {stats.cases}  asserting: {stats.cases_asserting} ({pct:.1f}%)  symmetric errors: {stats.symmetric_errors}  internal errors: {stats.internal_errors}")
    print(f"findings: {stats.findings} new keys, {stats.findings_repeated} repeats  by class: {stats.per_class}")
    print(f"known bugs withheld: {stats.known_bugs_suppressed}")
    if stats.known_bugs_run:
        print("known bugs run (--no-known-bugs), pairs one defect alone marks, failing / compared: " + ", ".join(f"{fid} {stats.known_bugs_exposed.get(fid, 0)}/{n}" for fid, n in sorted(stats.known_bugs_run.items(), key=lambda kv: int(kv[0][1:]) if kv[0][1:].isdigit() else 0)))
        print(f"failing pairs two defects mark: {stats.failing_pairs_mixed}  failing pairs no defect marks: {stats.failing_pairs_unmarked}")
    print(f"distinct shapes: {stats.distinct_shapes}  ir_traversal shape coverage: {stats.traversal_shapes_visited}/{SHAPE_DENOMINATOR}  ir_select coverage: {stats.select_shapes_visited}/{SELECT_COVERAGE_DENOMINATOR}  ir_path coverage: {stats.path_shapes_visited}/{PATH_COVERAGE_DENOMINATOR}  distinct plan pairs: {stats.distinct_plan_pairs}")
    print(f"writes: {stats.writes}  write-state diffs: {stats.write_state_diffs}  ir_write coverage: {stats.write_shapes_visited}/{WRITE_COVERAGE_DENOMINATOR}")
    if stats.scale != "default" or stats.timeouts:
        print(f"scale: {stats.scale}  state: {'read whole' if not stats.state_sample else f'counts + sample of {stats.state_sample}'}  timeouts: {stats.timeouts}")
    print(f"elapsed: {stats.elapsed:.1f}s")
    for name, t in sorted(stats.per_generator.items()):
        a = 100.0 * t["asserting"] / t["cases"] if t["cases"] else 0.0
        print(f"  {name:26s} cases={t['cases']:5d} asserting={a:5.1f}% findings={t['findings']} symerr={t['symmetric_errors']} internal={t['internal_errors']} withheld={t['suppressed']}")
    if dedup_summary:
        print("finding keys:")
        for k, v in list(dedup_summary.items())[:30]:
            print(f"  {v:4d}x {k}")
    if stats.coverage and collector is not None:
        collector.print_summary(stats.coverage)


def add_arguments(ap: argparse.ArgumentParser, engine) -> None:
    """The campaign's flags: its own, the provider's, and one per field of the
    provider's configuration class."""
    ap.add_argument("--iterations", type=int, default=20, help="generated instances to test; each is built in every data model, queried, compared (default: %(default)s)")
    ap.add_argument("--seed", type=int, default=1, help="first seed; iteration i uses seed + i, so a seed reproduces its case exactly (default: %(default)s)")
    ap.add_argument("--workers", type=int, default=1, help="seeds tested in parallel against the one server (default: %(default)s)")
    ap.add_argument("--out-dir", default="bugs", help="directory the finding bundles are written to (default: %(default)s)")
    ap.add_argument("--run-tag", default="run", help="name of this run, used in bundle and report file names (default: %(default)s)")
    ap.add_argument("--stats-dir", default="campaign_reports", help="directory the campaign statistics JSON is written to (default: %(default)s)")
    ap.add_argument("--max-findings", type=int, default=0, help="stop after this many distinct findings; 0 runs every iteration (default: %(default)s)")
    ap.add_argument("--explain", action="store_true", help="fingerprint every variant's plan (extra round trips)")
    ap.add_argument("--no-verify-state", dest="verify_state", action="store_false", help="skip comparing the databases' stored data with each other (after setup and after writes)")
    ap.add_argument("--no-known-bugs", action="store_true", help="run the variants that match a filed bug")
    ap.add_argument("--bundle-all", action="store_true", help="bundle every finding, not only the first per key")
    ap.add_argument("--bundle-setup-failures", action="store_true", help="also write a bundle when setup fails in every data model (a partial failure is always bundled)")
    ap.add_argument("--no-bundles", action="store_true", help="write no finding or state bundle, and no failing-pairs log: findings are only counted (a measurement campaign, e.g. code coverage with --no-known-bugs)")
    ap.add_argument("--coverage", choices=engine.coverage_levels, default="off", help="collect the engine's code coverage: seed = the growth curve; case / variant = also attribute it to the query generator / the variant that ran (--workers 1)")
    ap.add_argument("--coverage-repeat", action="store_true", help="--coverage variant: ask every variant a second time right after it (the A/A check of a coverage window's noise; the repeat is not compared)")
    ap.add_argument("--duration-hours", type=float, default=0, help="start no seed after this many hours (the running seeds finish); 0: run every iteration (default: %(default)s)")
    ap.add_argument("--state-sample", type=int, default=None, help="verify the stored state by counts and this many sampled records per class, 0 reads it whole (default: the scale profile's)")
    ap.add_argument("--query-timeout-ms", type=int, default=None, help="how long one query may run before it is a timeout, left out of the comparison (default: the scale profile's)")
    engine.add_arguments(ap)

    add_config_arguments(ap, engine.config_class)


def main(engine, args) -> int:
    try:
        stats, collector = run_campaign(engine, args)
    finally:
        engine.cleanup()
    print_stats(stats, collector=collector)
    os.makedirs(args.stats_dir, exist_ok=True)
    record = stats._asdict()
    if stats.coverage and collector is not None:
        record["coverage"] = collector.record(stats.coverage, args.stats_dir, args.run_tag)
    path = os.path.join(args.stats_dir, f"{args.run_tag}-campaign_stats.json")
    with open(path, "w") as fh:
        json.dump({**record, "engine": engine.name, "args": {k: v for k, v in vars(args).items() if k != "handler"}}, fh, indent=2, default=repr)
    print(f"stats written to {path}")
    return 0
