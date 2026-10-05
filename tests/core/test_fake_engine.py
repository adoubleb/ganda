"""The core end to end on the in-memory engine: no DBMS, no engine-specific package."""
import argparse
import json
import os

from ganda.core import campaign
from ganda.core.data_generator import build
from ganda.core.dedup import BugDeduplicator
from ganda.core.generators.registry import all_cases, reference_for
from ganda.core.generators.write_phase import run_write_phase
from ganda.core.mutation import runner as mutation
from ganda.core.oracle import run_case
from ganda.core.reporter import report_bug
from ganda.core.state_check import verify_state
from tests.fake_engine import SMALL, FakeProvider


def _args(parser_setup, engine, argv):
    ap = argparse.ArgumentParser()
    parser_setup(ap, engine)
    flags = [x for k, v in SMALL.items() for x in ("--" + k.replace("_", "-"), str(v))]
    return ap.parse_args(flags + argv)


def _sessions(engine, instance):
    server = engine.server()
    sessions = {}
    for data_model in engine.data_models:
        sessions[data_model] = server.fresh_session(engine.database_name("t", data_model))
        sessions[data_model].apply_setup(engine.mappers[data_model](instance))
    return sessions


def test_generation_is_deterministic_and_bound_to_the_engine():
    engine = FakeProvider()
    cfg = engine.config_class(**SMALL)
    schema, instance, rng = build(7, cfg, engine)
    assert schema.engine is engine and instance.schema.engine is engine
    again = build(7, cfg, engine)
    assert again[1].nodes == instance.nodes and again[1].edges == instance.edges
    texts = [c.queries for c in all_cases(rng, schema, instance, cfg=cfg)]
    assert texts and texts == [c.queries for c in all_cases(again[2], again[0], again[1], cfg=cfg)]


def test_every_generator_emits_cases_that_agree_and_have_a_reference():
    engine = FakeProvider()
    cfg = engine.config_class(**SMALL)
    seen = set()
    for seed in range(1, 6):
        schema, instance, rng = build(seed, cfg, engine)
        sessions = _sessions(engine, instance)
        assert all(verify_state(sessions[dm], dm, instance).ok for dm in engine.data_models)
        for case in all_cases(rng, schema, instance, cfg=cfg):
            assert case.engine is engine and set(engine.data_models) <= set(case.variants)
            r = run_case(case, sessions, instance)
            assert r.ok, (case.generator_name, case.shape, r.failing_pairs, r.reference_mismatches[:2])
            if case.params.get("reference") is not False:
                assert reference_for(case, instance)[0]
            seen.add(case.generator_name)
        wp = run_write_phase(sessions, instance, rng.for_generator("ir_write"), cfg.write_cases)
        assert not wp.stopped and all(r.ok for r in wp.results), wp.stopped
        seen |= {r.case.generator_name for r in wp.results}
    assert seen == {"ir_traversal", "ir_select", "ir_path", "ir_write"}


def test_a_corrupted_data_model_is_a_finding_with_a_bundle(tmp_path):
    engine = FakeProvider()
    cfg = engine.config_class(**SMALL)
    dedup = BugDeduplicator()
    bundles = []
    for seed in range(1, 8):
        schema, instance, rng = build(seed, cfg, engine)
        sessions = _sessions(engine, instance)
        sessions["graph"].tamper = lambda rows: rows[1:]  # the graph data model loses its first row
        plans = {dm: engine.mappers[dm](instance) for dm in engine.data_models}
        for case in all_cases(rng, schema, instance, cfg=cfg):
            r = run_case(case, sessions, instance)
            if r.ok:
                continue
            assert all("graph" in pair.split("/") for pair in r.failing_pairs) and {m.variant for m in r.reference_mismatches} <= {"graph"}
            first, n = dedup.admit(r)
            if first:
                bundles.append(report_bug(engine, str(tmp_path), "t", seed, cfg, instance, plans, r, times_seen=n))
    assert bundles
    d = bundles[0]
    assert {"queries.json", "instance.json", "repro.py", "README.md", "graph_setup.json"} <= set(os.listdir(d))
    recorded = json.load(open(os.path.join(d, "queries.json")))
    assert recorded["engine"] == "fake" and recorded["failing_pairs"]
    assert "replay('fake'" in open(os.path.join(d, "repro.py")).read()


def test_campaign_runs_clean_and_counts(tmp_path):
    engine = FakeProvider()
    args = _args(campaign.add_arguments, engine, ["--iterations", "4", "--seed", "1", "--workers", "2", "--out-dir", str(tmp_path / "bugs"), "--stats-dir", str(tmp_path / "stats")])
    stats, collector = campaign.run_campaign(engine, args)
    assert collector is None
    assert stats.instances == 4 and stats.findings == 0 and stats.setup_asymmetries == 0 and stats.crashes == 0
    assert stats.cases > 0 and stats.cases_asserting > 0 and stats.writes > 0
    assert set(stats.per_generator) == {"ir_traversal", "ir_select", "ir_path", "ir_write"}
    assert all(not server.databases for server in engine.servers)  # every database dropped


def test_mutation_benchmark_catches_instance_mutants_and_needs_the_reference_for_the_blind_one():
    engine = FakeProvider()
    picked = ["graph_drop_first_edge", "rel_swap_direction", "all_sides_drop_edge"]
    report = mutation.run_benchmark(engine, _args(mutation.add_arguments, engine, ["--trials", "4", "--mutants", *picked]))
    assert report["engine"] == "fake" and set(report["mutants"]) == set(picked)
    assert report["mutants"]["graph_drop_first_edge"]["caught"] > 0
    assert report["mutants"]["all_sides_drop_edge"]["caught"] > 0  # only the reference sees it
    blind = mutation.run_benchmark(engine, _args(mutation.add_arguments, engine, ["--trials", "4", "--no-reference", "--mutants", "all_sides_drop_edge"]))
    assert blind["mutants"]["all_sides_drop_edge"]["caught"] == 0
