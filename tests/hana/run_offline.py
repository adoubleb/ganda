"""Run campaigns of the SAP HANA provider against the offline stand-in (`offline.py`):
`python3 -m tests.hana.run_offline --seeds 1 20` from the project root. Every case's
answers are compared pair by pair, as a live campaign does."""
from __future__ import annotations

import argparse
import collections
import random
import traceback

from ganda.core.data.generator import build
from ganda.core.query.generators.registry import all_cases
from ganda.core.query.generators.write_phase import run_write_phase
from ganda.core.oracle.oracle import run_cases
from ganda.core.engine.state_check import compare_states
from ganda.hana.provider import PROVIDER as E
from tests.hana.offline import OfflineServer

# what the offline stand-in cannot run: HANA's MEDIAN aggregate in SPARQL, and the
# `native` variants, whose answers rest on SPARQL's error propagation through `&&`,
# which rdflib does not implement (`error && false` is an error there, false in the
# standard); a graph text whose openCypher the SQLite translator does not cover
UNSUPPORTED = ("MEDIAN(",)


def _offline(variant: str, text: str, syntax: "str | None" = None) -> bool:
    if variant.startswith("graph"):
        from tests.hana.opencypher_sqlite import translatable

        # a GraphScript procedure called from a SQLScript block (the path `script`
        # syntax) has no stand-in
        return not text.startswith("DO ") and translatable(text)
    if not variant.startswith("rdf"):
        return True
    # a traversal's BASE variant (`rdf`, and its `rdf:pull_up`) may be the native syntax,
    # which only the case's `concrete_syntax` names
    tags = variant.split(":", 1)[-1].split("+") + ([syntax] if syntax else [])
    if "jscan" in tags and "EXISTS {" in text:
        # rdflib: an independent FILTER EXISTS in a group changes its join with a
        # subquery (`tests/hana/test_offline.py::test_rdflib_quirks` shows it)
        return False
    return not any(u in text for u in UNSUPPORTED) and "native" not in tags


def offline_cases(cases: list) -> list:
    import dataclasses

    out = []
    for c in cases:
        syntaxes = c.params.get("concrete_syntax", {})
        keep = {v: t for v, t in c.queries.items() if _offline(v, t, syntaxes.get(v) if isinstance(syntaxes, dict) else None)}
        if len(keep) != len(c.queries):
            if not any(v in keep for v in E.data_models):
                continue
            c = dataclasses.replace(c, queries=keep, params={**c.params, "partial": True})
        out.append(c)
    return out


def _offline_run_case(case, *args, **kw):
    from ganda.core.oracle.oracle import run_case

    cases = offline_cases([case])
    if not cases:
        from ganda.core.oracle.oracle import CaseResult

        return CaseResult(case=case, ok=True)
    return run_case(cases[0], *args, **kw)


def run_seed(seed: int, cfg, writes: bool = True) -> dict:
    schema, instance, rng = build(seed, cfg, E)
    server = OfflineServer()
    sessions = {}
    report = {"seed": seed, "state": {}, "results": [], "write_diffs": {}}
    for dm in E.data_models:
        s = server.fresh_session(E.database_name(f"s{seed}", dm))
        s.apply_setup(E.mappers[dm](instance))
        sessions[dm] = s
    for dm, d in compare_states(sessions, instance).items():
        if not d.ok:
            report["state"][dm] = d.problems[:5]
    if report["state"]:
        return report
    cases = offline_cases(all_cases(rng, schema, instance, cfg=cfg))
    report["results"] = run_cases(cases, sessions)
    if writes and cfg.write_cases:
        # the write phase's read-backs are filtered as the read cases are
        from ganda.core.query.generators import write_phase

        write_phase.run_case = _offline_run_case
        wp = run_write_phase(sessions, instance, rng.for_generator("ir_write"), cfg.write_cases)
        report["results"] += wp.results
        report["write_diffs"] = {k: v.problems[:5] for k, v in wp.diffs.items() if not v.ok}
        report["write_stopped"] = wp.stopped
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", nargs=2, type=int, default=[1, 10])
    ap.add_argument("--show", type=int, default=3)
    ap.add_argument("--no-writes", action="store_true")
    args = ap.parse_args(argv)
    cfg = E.config_class(write_form_probability=0.8, churn_edge_update_probability=0.3, clusters_probability=0.3)
    counts = collections.Counter()
    shown = collections.Counter()
    for seed in range(args.seeds[0], args.seeds[1] + 1):
        try:
            r = run_seed(seed, cfg, writes=not args.no_writes)
        except Exception:
            counts["crash"] += 1
            print(f"[seed {seed}] CRASH\n{traceback.format_exc()[-3000:]}")
            continue
        bad = sum(1 for res in r["results"] if not res.ok)
        print(f"[seed {seed}] {len(r['results'])} cases, {bad} not agreeing" + (", setup state off" if r["state"] else "") + (", write state off" if r.get("write_diffs") else ""), flush=True)
        if r["state"]:
            counts["state"] += 1
            print(f"[seed {seed}] STATE {r['state']}")
        if r.get("write_diffs"):
            counts["write_state"] += 1
            print(f"[seed {seed}] WRITE STATE {r.get('write_stopped')} {r['write_diffs']}")
        for res in r["results"]:
            counts["cases"] += 1
            if res.ok:
                continue
            k = res.finding_class
            counts[k] += 1
            key = (k, res.case.generator_name, tuple(res.failing_pairs[:2]))
            if shown[key] < args.show:
                shown[key] += 1
                print(f"[seed {seed}] {k} {res.case.generator_name} {res.case.shape} pairs={res.failing_pairs[:6]}")
                for v, e in res.errors.items():
                    if e:
                        print(f"   ERROR {v}: {e[:600]}")
                for m in res.mismatches[:2]:
                    print(f"   MISMATCH {m.pair} id={m.record_id}: {str(m.left_raw)[:200]} vs {str(m.right_raw)[:200]}")
                    print(f"     {m.left}: {res.case.queries.get(m.left, '')[:700]}")
                    print(f"     {m.right}: {res.case.queries.get(m.right, '')[:700]}")
    print(dict(counts))
    return 0 if set(counts) <= {"cases"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
