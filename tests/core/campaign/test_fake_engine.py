"""The core end to end on the in-memory engine: no DBMS, no engine-specific package."""
import argparse
import json
import os

from ganda.core.campaign import run as campaign
from ganda.core.data.generator import build
from ganda.core.oracle.dedup import BugDeduplicator
from ganda.core.query.generators.registry import all_cases
from ganda.core.query.generators.write_phase import run_write_phase
from ganda.core.mutation import runner as mutation
from ganda.core.oracle.oracle import run_case
from ganda.core.campaign.reporter import report_bug
from ganda.core.engine.state_check import compare_states
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


def test_every_generator_emits_cases_that_agree():
    engine = FakeProvider()
    cfg = engine.config_class(**SMALL)
    seen = set()
    for seed in range(1, 6):
        schema, instance, rng = build(seed, cfg, engine)
        sessions = _sessions(engine, instance)
        assert all(d.ok for d in compare_states(sessions, instance).values())
        for case in all_cases(rng, schema, instance, cfg=cfg):
            assert case.engine is engine and set(engine.data_models) <= set(case.variants)
            r = run_case(case, sessions)
            assert r.ok, (case.generator_name, case.shape, r.failing_pairs)
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
            r = run_case(case, sessions)
            if r.ok:
                continue
            assert r.failing_pairs and all("graph" in pair.split("/") for pair in r.failing_pairs)
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


def test_a_campaign_warns_on_a_server_of_another_build_than_the_baseline(tmp_path, monkeypatch, capsys):
    from ganda.core.rules.baseline import Baseline
    from tests.fake_engine import FakeServer

    engine = FakeProvider()
    engine.baseline = Baseline(tmp_path / "baseline.json")
    engine.baseline.record("1.0", ["X1"], {}, [])
    argv = ["--iterations", "1", "--out-dir", str(tmp_path / "bugs"), "--stats-dir", str(tmp_path / "stats")]
    for version, warned in (("1.0", False), ("1.1", True)):
        monkeypatch.setattr(FakeServer, "version", lambda self: version)
        stats, _ = campaign.run_campaign(engine, _args(campaign.add_arguments, engine, argv))
        out = capsys.readouterr().out
        assert stats.instances == 1 and ("warning: fake: the server is 1.1, the baseline 1.0" in out) == warned


def test_mutation_benchmark_catches_instance_mutants():
    engine = FakeProvider()
    picked = ["graph_drop_first_edge", "rel_swap_direction"]
    report = mutation.run_benchmark(engine, _args(mutation.add_arguments, engine, ["--trials", "4", "--mutants", *picked]))
    assert report["engine"] == "fake" and set(report["mutants"]) == set(picked)
    assert report["mutants"]["graph_drop_first_edge"]["caught"] > 0


def test_every_finding_class_has_a_deduplication_key():
    engine = FakeProvider()
    cfg = engine.config_class(**SMALL)
    tampers = {
        "duplicate_id_rows": (lambda rows: rows + rows[:1], ("graph",)),
        "mismatch": (lambda rows: rows[1:], ("graph",)),
    }
    seen = set()
    for seed in range(1, 8):
        schema, instance, rng = build(seed, cfg, engine)
        cases = all_cases(rng, schema, instance, cfg=cfg)
        for tamper, data_models in tampers.values():
            sessions = _sessions(engine, instance)
            for dm in data_models:
                sessions[dm].tamper = tamper
            for case in cases:
                r = run_case(case, sessions)
                if not r.ok:
                    key = BugDeduplicator.key(r)
                    assert key[0] == case.generator_name and key[3] == r.finding_class and key[2]
                    seen.add(r.finding_class)
    assert set(tampers) <= seen



def test_the_oracle_compares_one_spelling_across_data_models():
    from ganda.core.oracle.case import Comparison, QueryCase, comparable_pairs
    from ganda.core.oracle.oracle import build_result

    engine = FakeProvider()
    case = QueryCase("ir_select", "", Comparison.SET_BY_ID, {"doc": "q", "graph": "q", "rel": "q", "graph:match": "q", "doc:rw": "q", "rel:rw": "q", "rel:lone": "q"},
                     spellings={"doc:rw": "rw", "rel:rw": "rw", "rel:lone": "lone"}, engine=engine)
    pairs = {f"{a}/{b}" for a, b in comparable_pairs(case, case.variants)}
    # the plain spelling across data models (whatever the model's syntax), the rewrite
    # across data models; never two variants of one data model, never two spellings
    assert pairs == {"doc/graph", "doc/rel", "doc/graph:match", "graph/rel", "graph:match/rel", "doc:rw/rel:rw"}
    row = [{"id": 1, "v": 1}]
    plain = {"doc": row, "graph": row, "rel": row}
    # doc:rw and rel:rw refuse alike: no finding, the plain spelling still compared
    r = build_result(case, {**plain, "graph:match": [{"id": 1, "v": 2}], "doc:rw": [], "rel:rw": []}, {"doc:rw": "refused", "rel:rw": "refused"})
    assert not r.error_asymmetry and r.symmetric_error and r.failing_pairs == ["doc/graph:match", "graph:match/rel"]
    # one side of a compared pair erred: an error asymmetry
    r = build_result(case, {**plain, "graph:match": row, "doc:rw": [], "rel:rw": row}, {"doc:rw": "refused"})
    assert r.error_asymmetry and not r.ok
