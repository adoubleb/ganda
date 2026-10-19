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

from ganda.core.data import scale as _scale
from ganda.core.campaign.config import add_config_arguments, config_from_args
from ganda.core.data.generator import build, limits_of
from ganda.core.query.generators.write_phase import run_write_phase
from ganda.core.engine.provider import EngineCrash, EngineError
from ganda.core.oracle.oracle import run_case
from ganda.core.query.generators.registry import all_cases


def _setup(engine, server, ns: str, instance, mutant) -> dict:
    sessions = {}
    for data_model in engine.data_models:
        inst = instance
        if mutant is not None and data_model in mutant.instance_for_data_model:
            inst = mutant.instance_for_data_model[data_model](instance)
        s = server.fresh_session(engine.database_name(ns, data_model))
        s.apply_setup(engine.mappers[data_model](inst))
        sessions[data_model] = s
    return sessions


def _drop(engine, server, ns):
    for data_model in engine.data_models:
        try:
            server.drop_database(engine.database_name(ns, data_model))
        except (EngineError, EngineCrash):
            pass


def _write_results(sessions, instance, rng, cfg, args) -> list:
    """The write phase's answers and read-backs, without its state check: a mutant
    that corrupts one side's instance would fail that check before any write ran,
    which says nothing about the write supply."""
    if not cfg.write_cases:
        return []
    return run_write_phase(sessions, instance, rng.for_generator("ir_write"), cfg.write_cases, verify=False, reference=not args.no_reference).results


def run_benchmark(engine, args) -> dict:
    cfg = _scale.resolve(config_from_args(engine.config_class, args), limits_of(engine))

    by_name = {m.name: m for m in engine.mutants}
    mutants = [by_name[n] for n in args.mutants] if args.mutants else list(engine.mutants)
    seeds = list(range(args.seed, args.seed + args.trials))
    report: dict = {"engine": engine.name, "tag": args.tag, "trials": args.trials, "seed": args.seed, "no_reference": args.no_reference, "config": dataclasses.asdict(cfg), "mutants": {}}
    start = time.time()
    ns = f"mut{os.getpid()}"
    with engine.server(None) as server:
        # the healthy run: which seeds are clean, so a mutant is only scored on those
        clean: dict = {}
        for seed in seeds:
            schema, instance, rng = build(seed, cfg, engine)
            try:
                sessions = _setup(engine, server, ns, instance, None)
            except EngineError as e:
                print(f"[seed {seed}] healthy setup failed: {str(e)[:120]}", flush=True)
                _drop(engine, server, ns)
                continue
            cases = all_cases(rng, schema, instance, cfg=cfg)
            bad = [c.generator_name for c in cases if not run_case(c, sessions, None if args.no_reference else instance).ok]
            bad += [r.case.generator_name for r in _write_results(sessions, instance, rng, cfg, args) if not r.ok]
            clean[seed] = not bad
            if bad:
                print(f"[seed {seed}] healthy run not clean: {sorted(set(bad))}", flush=True)
            _drop(engine, server, ns)
        scored = [s for s, ok in clean.items() if ok]
        for mutant in mutants:
            caught_by: dict = defaultdict(int)
            caught_trials = 0
            for seed in scored:
                schema, instance, rng = build(seed, cfg, engine)
                try:
                    sessions = _setup(engine, server, ns, instance, mutant)
                except EngineError:
                    _drop(engine, server, ns)
                    continue
                cases = all_cases(rng, schema, instance, cfg=cfg)
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
                _drop(engine, server, ns)
            rate = caught_trials / len(scored) if scored else 0.0
            report["mutants"][mutant.name] = {"category": mutant.category, "caught": caught_trials, "trials": len(scored), "rate": rate, "caught_by": dict(sorted(caught_by.items(), key=lambda kv: -kv[1]))}
            print(f"{mutant.name:32s} {caught_trials:3d}/{len(scored)} ({rate:.2f})  {', '.join(list(dict(sorted(caught_by.items(), key=lambda kv: -kv[1])))[:5])}", flush=True)
    report["elapsed"] = time.time() - start
    rates = [m["rate"] for m in report["mutants"].values()]
    report["mean_rate"] = sum(rates) / len(rates) if rates else 0.0
    report["caught_any"] = sum(1 for m in report["mutants"].values() if m["caught"] > 0)
    return report


def add_arguments(ap: argparse.ArgumentParser, engine) -> None:
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tag", default="mutation")
    ap.add_argument("--no-reference", action="store_true")
    ap.add_argument("--mutants", nargs="*")
    ap.add_argument("--out-dir", default="mutation_reports")
    add_config_arguments(ap, engine.config_class)


def main(engine, args) -> int:
    try:
        report = run_benchmark(engine, args)
    finally:
        engine.cleanup()
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
