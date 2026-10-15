"""The campaign driver: for each seed, generate an instance, materialize it once per
data model into fresh databases on one server of the engine, verify the state, run
every case, and write a bundle for every NEW finding key (`dedup.BugDeduplicator`).
Crashes are autopsied and the server is restarted. `--workers N` runs N seed shards as
threads against the same server (the work is I/O-bound).

The configuration's scale profile (`ganda.core.scale`, `--scale`) decides what a flag
left unset does: whether the reference leg runs (`--reference` / `--no-reference`),
whether the stored state is read whole or checked by counts and a sample
(`--state-sample`), how long a query may run (`--query-timeout-ms`), and whether a
bundle embeds the data or its shape.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from collections import Counter
from typing import NamedTuple

from ganda.core import scale as _scale

from ganda.core.ast.path import COVERAGE_DENOMINATOR as PATH_COVERAGE_DENOMINATOR
from ganda.core.ast.select import COVERAGE_DENOMINATOR as SELECT_COVERAGE_DENOMINATOR
from ganda.core.ast.traversal import SHAPE_DENOMINATOR
from ganda.core.ast.write import COVERAGE_DENOMINATOR as WRITE_COVERAGE_DENOMINATOR
from ganda.core.config import add_config_arguments, config_from_args
from ganda.core.data_generator import GeneratorConfig, build, limits_of
from ganda.core.dedup import BugDeduplicator
from ganda.core.generators.registry import all_cases
from ganda.core.generators.write_phase import run_write_phase
from ganda.core.oracle import is_empty_value, run_cases
from ganda.core.provider import EngineCrash, EngineError
from ganda.core.reporter import report_bug, report_crash, report_setup_problem
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
    coverage: "dict | None" = None  # the provider's coverage summary under --coverage
    timeouts: int = 0  # variants that gave no answer in time (left out of their case's comparison)
    reference: bool = True  # whether the reference leg ran
    scale: str = "default"
    state_sample: int = 0  # 0: the stored state was read whole
    cases_partial: int = 0  # cases some data model had no concrete syntax for (compared among the others)


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
        self.coverage = None  # the provider's `coverage.Collector`


def _tpl(shared: _Shared, name: str) -> dict:
    return shared.per_generator.setdefault(name, {"cases": 0, "asserting": 0, "findings": 0, "symmetric_errors": 0, "internal_errors": 0, "suppressed": 0})


def _sample_rng(seed: int) -> random.Random:
    """The state check's sample for one instance: a fresh stream per call, so every
    data model is checked on the same records."""
    return random.Random(f"{seed}:statesample")


def run_instance(engine, server, seed: int, cfg: GeneratorConfig, args, shared: _Shared, ns: str) -> None:
    schema, instance, rng = build(seed, cfg, engine)
    reference = instance if args.reference else None
    data_models = engine.data_models
    plans = {data_model: engine.mappers[data_model](instance) for data_model in data_models}
    sessions: dict = {}
    setup_errors: dict = {}
    diffs: dict = {}
    for data_model, plan in plans.items():
        s = server.fresh_session(engine.database_name(ns, data_model))
        try:
            s.apply_setup(plan)
            setup_errors[data_model] = ""
            sessions[data_model] = s
            if args.verify_state:
                diffs[data_model] = verify_state(s, data_model, instance, sample=args.state_sample, rng=_sample_rng(seed))
        except EngineError as e:
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
            if len(failed) == len(data_models):
                shared.counters["setup_failures"] += 1
                kind = "setup-failure"
            else:
                shared.counters["setup_asymmetries"] += 1
                kind = "setup-asymmetry" if failed else "state-diff"
        if len(failed) < len(data_models) or args.bundle_setup_failures:
            report_setup_problem(engine, args.out_dir, args.run_tag, seed, cfg, instance, plans, setup_errors, diffs, kind, embed_data=args.embed_data)
            print(f"[seed {seed}] {kind}: {failed or bad_state}", flush=True)
        _drop(engine, server, ns)
        return

    cases = all_cases(rng, schema, instance, cfg=cfg)
    hooks = {"after_case": cov.after_case, "after_variant": cov.after_variant} if cov else {}
    results = run_cases(cases, sessions, reference, explain=args.explain, keep_known_bugs=args.no_known_bugs, timeouts_are_errors=args.timeouts_are_errors, **hooks)
    if args.verify_state and any(engine.may_write(r.case) for r in results):
        # a read case that writes inside a transaction rolled it back: the live state
        # must still be the Instance on every data model
        after = {data_model: verify_state(s, data_model, instance, sample=args.state_sample, rng=_sample_rng(seed)) for data_model, s in sessions.items()}
        leaked = [data_model for data_model, d in after.items() if not d.ok]
        if leaked:
            with shared.lock:
                shared.counters["state_leaks"] += 1
            d = report_setup_problem(engine, args.out_dir, args.run_tag, seed, cfg, instance, plans, {data_model: "" for data_model in sessions}, after, "state-leak", embed_data=args.embed_data)
            print(f"[seed {seed}] state-leak after the cases: {leaked} -> {d}", flush=True)
    if cfg.write_cases:
        # the write phase: predicate-targeted writes after every read, each checked
        # and applied to the Instance (`ganda.core.generators.write_phase`)
        wp = run_write_phase(sessions, instance, rng.for_generator("ir_write"), cfg.write_cases, verify=args.verify_state, keep_known_bugs=args.no_known_bugs, reference=args.reference, state_sample=args.state_sample, state_rng=lambda: _sample_rng(seed), timeouts_are_errors=args.timeouts_are_errors)

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
            d = report_setup_problem(engine, args.out_dir, args.run_tag, seed, cfg, wp.instance, plans, wrote, wp.diffs, f"write-state-{wp.failed_step}", write_step=wp.failed_step, embed_data=args.embed_data)
            print(f"[seed {seed}] write-state after {wp.stopped} -> {d}", flush=True)
    with shared.lock:
        for r in results:
            t = _tpl(shared, r.case.generator_name)
            t["cases"] += 1
            shared.counters["cases"] += 1
            if r.case.params.get("partial"):
                shared.counters["cases_partial"] += 1
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
            shared.counters["timeouts"] += len(r.timeouts)
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
            d = report_bug(engine, args.out_dir, args.run_tag, seed, cfg, instance, plans, r, times_seen=n, embed_data=args.embed_data, max_value_chars=args.max_value_chars)
            print(f"[seed {seed}] {r.finding_class} {r.case.generator_name} {r.case.shape} pairs={r.failing_pairs} -> {d}", flush=True)
    _drop(engine, server, ns)


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
    unset (`args.reference`, `args.state_sample`, `args.query_timeout_ms`), and the two
    it always decides (`args.embed_data`, `args.max_value_chars`)."""
    p = _scale.profile_of(cfg)
    if getattr(args, "reference", None) is None:
        args.reference = p.reference
    if getattr(args, "state_sample", None) is None:
        args.state_sample = p.state_sample
    if getattr(args, "query_timeout_ms", None) is None:
        args.query_timeout_ms = p.query_timeout_ms
    args.embed_data = p.embed_data
    args.max_value_chars = p.max_bundle_value_chars
    args.timeouts_are_errors = p.timeouts_are_errors
    if p.name != "default":
        state = "read whole" if not args.state_sample else f"checked by counts and a sample of {args.state_sample}"
        print(f"scale {p.name}: reference leg {'on' if args.reference else 'off'}, stored state {state}, query timeout {args.query_timeout_ms / 1000:.0f} s ({'a finding' if p.timeouts_are_errors else 'left out of the comparison'}), bundles carry the shape of the data", flush=True)


def run_campaign(engine, args) -> "tuple[CampaignStats, object]":
    """(the campaign's statistics, the coverage collector or None)."""
    cfg = _scale.resolve(engine.configure(args, config_from_args(engine.config_class, args)), limits_of(engine))
    apply_profile(args, cfg)
    shared = _Shared()
    seeds = list(range(args.seed, args.seed + args.iterations))
    start = time.time()
    os.makedirs(args.out_dir, exist_ok=True)
    if args.coverage in ("case", "variant") and args.workers > 1:
        raise SystemExit(f"--coverage {args.coverage} attributes each window to what ran since the last dump: it needs --workers 1")
    with engine.server(args) as server:
        shared.coverage = engine.coverage(server, args)
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
    if shared.coverage:
        shared.coverage.write(args.stats_dir, args.run_tag)
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
        reference=bool(args.reference),
        scale=cfg.scale,
        state_sample=args.state_sample,
    )
    return stats, shared.coverage


def print_stats(stats: CampaignStats, dedup_summary: "dict | None" = None, collector=None) -> None:
    print("=" * 72)
    print(f"instances: {stats.instances}  setup failures: {stats.setup_failures}  setup asymmetries: {stats.setup_asymmetries}  crashes: {stats.crashes}  state leaks: {stats.state_leaks}")
    pct = 100.0 * stats.cases_asserting / stats.cases if stats.cases else 0.0
    print(f"cases: {stats.cases}  asserting: {stats.cases_asserting} ({pct:.1f}%)  symmetric errors: {stats.symmetric_errors}  internal errors: {stats.internal_errors}")
    print(f"findings: {stats.findings} new keys, {stats.findings_repeated} repeats  by class: {stats.per_class}")
    print(f"known bugs withheld: {stats.known_bugs_suppressed}")
    print(f"distinct shapes: {stats.distinct_shapes}  ir_traversal shape coverage: {stats.traversal_shapes_visited}/{SHAPE_DENOMINATOR}  ir_select coverage: {stats.select_shapes_visited}/{SELECT_COVERAGE_DENOMINATOR}  ir_path coverage: {stats.path_shapes_visited}/{PATH_COVERAGE_DENOMINATOR}  distinct plan pairs: {stats.distinct_plan_pairs}")
    print(f"writes: {stats.writes}  write-state diffs: {stats.write_state_diffs}  ir_write coverage: {stats.write_shapes_visited}/{WRITE_COVERAGE_DENOMINATOR}")
    if stats.scale != "default" or stats.timeouts or not stats.reference:
        print(f"scale: {stats.scale}  reference leg: {'on' if stats.reference else 'off'}  state: {'read whole' if not stats.state_sample else f'counts + sample of {stats.state_sample}'}  timeouts: {stats.timeouts}")
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
    ap.add_argument("--no-verify-state", dest="verify_state", action="store_false", help="skip comparing each database's stored data with the generated instance (after setup and after writes)")
    ap.add_argument("--no-known-bugs", action="store_true", help="run the variants that match a filed bug")
    ap.add_argument("--bundle-all", action="store_true", help="bundle every finding, not only the first per key")
    ap.add_argument("--bundle-setup-failures", action="store_true", help="also write a bundle when setup fails in every data model (a partial failure is always bundled)")
    ap.add_argument("--coverage", choices=engine.coverage_levels, default="off", help="collect the engine's code coverage: seed = the growth curve; case / variant = also attribute it to the query generator / the variant that ran (--workers 1)")
    ap.add_argument("--reference", action=argparse.BooleanOptionalAction, default=None, help="run the reference leg (default: the scale profile's: on at the default scale, off at the large one)")
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
