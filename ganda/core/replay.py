"""Replay one bundle: what a bundle's `repro.py` runs.

The instance is regenerated from (seed, config) on the named engine, materialized once
per data model on a fresh server, its state verified, and the case re-run with the
known-bug guards off. A write-phase case replays the phase up to its step.
"""
from __future__ import annotations

import json

from ganda.core.data_generator import build
from ganda.core.generators.registry import all_cases
from ganda.core.generators.write_phase import run_write_phase
from ganda.core.oracle import run_case
from ganda.core.state_check import verify_state


def _show(r) -> None:
    for variant in r.variants:
        print("  ", variant, json.dumps(r.rows[variant], ensure_ascii=False, default=repr)[:300], r.errors.get(variant, "")[:200])
    for m in r.reference_mismatches[:5]:
        print("   ref", m.variant, m.record_id, m.got, "expected", m.expected)


def replay(engine_name: str, seed: int, config: dict, key: "dict | None") -> int:
    from ganda.registry import provider

    engine = provider(engine_name)
    cfg = engine.config_class(**config)
    schema, instance, rng = build(seed, cfg, engine)
    try:
        with engine.server(None) as server:
            sessions = {}
            for data_model in engine.data_models:
                s = server.fresh_session(engine.database_name(f"repro{seed}", data_model))
                s.apply_setup(engine.mappers[data_model](instance))
                d = verify_state(s, data_model, instance)
                print(data_model, "state:", "ok" if d.ok else d.problems[:5])
                sessions[data_model] = s
            if key and key.get("write_step") is not None:
                # the write phase up to the step: every write, its answers and state check
                wp = run_write_phase(sessions, instance, rng.for_generator("ir_write"), cfg.write_cases, keep_known_bugs=key["keep_known_bugs"], stop_after=key["write_step"])
                for r in wp.results:
                    print(r.case.generator_name, r.case.params.get("step", r.case.params.get("after_write")), r.case.shape, "ok" if r.ok else r.finding_class, r.failing_pairs)
                    if r.case.generator_name == "ir_write":
                        for data_model in engine.data_models:
                            print("  ", data_model, r.case.params["concrete_syntax"][data_model], "|", r.case.params["statements"][data_model][:300])
                    if not r.ok:
                        _show(r)
                print("phase:", wp.stopped or "ran every step", {s: d.problems[:5] for s, d in wp.diffs.items() if not d.ok})
                return 0
            cases = all_cases(rng, schema, instance, cfg=cfg)
            if key:
                cases = [c for c in cases if c.generator_name == key["generator"] and c.shape == key["shape"] and c.queries.get(key["variant"]) == key["text"]] or cases
            for case in cases:
                r = run_case(case, sessions, instance, keep_known_bugs=True)
                print(case.generator_name, case.shape, "ok" if r.ok else r.finding_class, r.failing_pairs)
                if not r.ok:
                    _show(r)
    finally:
        engine.cleanup()
    return 0
