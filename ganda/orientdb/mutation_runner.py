"""The mutation benchmark: for each mutant and each trial seed, materialize the
instance with the mutant's corruption on its data model(s), run every case, and record
which cases caught it. A mutant caught in no trial exits non-zero for the structural
category. The report (`mutation_reports/<tag>.json`) is the evidence a change to the
query supply did not lose discrimination: compare catch rates over the SAME mutant set
and seeds, never the headline mean across sets of different size.

`--no-semantics` scores pure cross-model detection (the reference leg switched off),
which is the mode that isolates what the pairing itself sees.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from collections import defaultdict

from ganda.orientdb.data_generator import GeneratorConfig, build
from ganda.orientdb.connection import OrientCrash, OrientError, OrientServer, cleanup_orphans, db_name
from ganda.orientdb.mapping import MAPPERS
from ganda.orientdb.mutants import MUTANTS, MUTANTS_BY_NAME
from ganda.orientdb.oracle import run_case
from ganda.core.generators.registry import all_cases


def _setup(server, ns: str, instance, mutant) -> dict:
    sessions = {}
    for data_model in ("doc", "graph", "rel"):
        inst = instance
        if mutant is not None and data_model in mutant.instance_for_data_model:
            inst = mutant.instance_for_data_model[data_model](instance)
        s = server.fresh_session(db_name(ns, data_model))
        s.apply_setup(MAPPERS[data_model](inst))
        sessions[data_model] = s
    return sessions


def _drop(server, ns):
    for data_model in ("doc", "graph", "rel"):
        try:
            server.drop_database(db_name(ns, data_model))
        except (OrientError, OrientCrash):
            pass


def _write_results(sessions, instance, rng, cfg, args) -> list:
    """The write phase's answers and read-backs, without its state check: a mutant
    that corrupts one side's instance would fail that check before any write ran,
    which says nothing about the write supply."""
    if not cfg.write_cases:
        return []
    from ganda.orientdb.write_phase import run_write_phase

    return run_write_phase(sessions, instance, rng.for_generator("ir_write"), cfg.write_cases, verify=False, reference=not args.no_reference).results


def run_benchmark(args) -> dict:
    cfg = GeneratorConfig(**{k: v for k, v in vars(args).items() if k in {f.name for f in dataclasses.fields(GeneratorConfig)}})
    mutants = [MUTANTS_BY_NAME[n] for n in args.mutants] if args.mutants else list(MUTANTS)
    seeds = list(range(args.seed, args.seed + args.trials))
    report: dict = {"tag": args.tag, "trials": args.trials, "seed": args.seed, "no_reference": args.no_reference, "config": dataclasses.asdict(cfg), "mutants": {}}
    start = time.time()
    ns = f"mut{os.getpid()}"
    with OrientServer() as server:
        # the healthy run: which seeds are clean, so a mutant is only scored on those
        clean: dict = {}
        for seed in seeds:
            schema, instance, rng = build(seed, cfg)
            try:
                sessions = _setup(server, ns, instance, None)
            except OrientError as e:
                print(f"[seed {seed}] healthy setup failed: {str(e)[:120]}", flush=True)
                _drop(server, ns)
                continue
            cases = all_cases(rng, schema, instance, traversal_cases=cfg.traversal_cases, select_cases=cfg.select_cases, path_cases=cfg.path_cases, card_cases=cfg.card_cases, card_corpus=cfg.card_corpus)
            bad = [c.generator_name for c in cases if not run_case(c, sessions, None if args.no_reference else instance).ok]
            bad += [r.case.generator_name for r in _write_results(sessions, instance, rng, cfg, args) if not r.ok]
            clean[seed] = not bad
            if bad:
                print(f"[seed {seed}] healthy run not clean: {sorted(set(bad))}", flush=True)
            _drop(server, ns)
        scored = [s for s, ok in clean.items() if ok]
        for mutant in mutants:
            caught_by: dict = defaultdict(int)
            caught_trials = 0
            for seed in scored:
                schema, instance, rng = build(seed, cfg)
                try:
                    sessions = _setup(server, ns, instance, mutant)
                except OrientError:
                    _drop(server, ns)
                    continue
                cases = all_cases(rng, schema, instance, traversal_cases=cfg.traversal_cases, select_cases=cfg.select_cases, path_cases=cfg.path_cases, card_cases=cfg.card_cases, card_corpus=cfg.card_corpus)
                hit = False
                for case in cases:
                    if mutant.rewrite_case is not None:
                        case = mutant.rewrite_case(case)
                    if mutant.rewrite_case_schema is not None:
                        case = mutant.rewrite_case_schema(case, schema)
                    r = run_case(case, sessions, None if args.no_reference else instance)
                    if not r.ok:
                        caught_by[case.generator_name] += 1
                        hit = True
                for r in _write_results(sessions, instance, rng, cfg, args):
                    if not r.ok:
                        caught_by[r.case.generator_name] += 1
                        hit = True
                caught_trials += int(hit)
                _drop(server, ns)
            rate = caught_trials / len(scored) if scored else 0.0
            report["mutants"][mutant.name] = {"category": mutant.category, "caught": caught_trials, "trials": len(scored), "rate": rate, "caught_by": dict(sorted(caught_by.items(), key=lambda kv: -kv[1]))}
            print(f"{mutant.name:32s} {caught_trials:3d}/{len(scored)} ({rate:.2f})  {', '.join(list(dict(sorted(caught_by.items(), key=lambda kv: -kv[1])))[:5])}", flush=True)
    report["elapsed"] = time.time() - start
    rates = [m["rate"] for m in report["mutants"].values()]
    report["mean_rate"] = sum(rates) / len(rates) if rates else 0.0
    report["caught_any"] = sum(1 for m in report["mutants"].values() if m["caught"] > 0)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tag", default="mutation")
    ap.add_argument("--no-reference", action="store_true")
    ap.add_argument("--mutants", nargs="*")
    ap.add_argument("--out-dir", default="mutation_reports")
    for f in dataclasses.fields(GeneratorConfig):
        flag = "--" + f.name.replace("_", "-")
        if f.type is bool or isinstance(f.default, bool):
            ap.add_argument(flag, type=lambda s: s.lower() in ("1", "true", "yes"), default=f.default)
        else:
            ap.add_argument(flag, type=type(f.default), default=f.default)
    args = ap.parse_args(argv)
    try:
        report = run_benchmark(args)
    finally:
        cleanup_orphans()
    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, f"{args.tag}.json")
    with open(path, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"caught {report['caught_any']}/{len(report['mutants'])} mutants, mean rate {report['mean_rate']:.3f}; written to {path}")
    structural_missed = [n for n, m in report["mutants"].items() if m["category"] == "structural" and m["caught"] == 0]
    if structural_missed:
        print(f"STRUCTURAL MUTANTS NEVER CAUGHT: {structural_missed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
