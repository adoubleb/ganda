"""The large scale on the in-memory engine: the profile, the limits every grown value
and hub respects, the tail axes at zero, the campaign without the reference leg, the
sampled state check, slim bundles and timeouts."""
import argparse
import base64
import dataclasses
import json
import os
from collections import Counter

import pytest

from ganda.core import campaign, scale
from ganda.core.case import Comparison, QueryCase
from ganda.core.data_generator import GeneratorConfig, build
from ganda.core.generators.registry import all_cases
from ganda.core.model import BLOB_TYPES, GROWABLE_TYPES, FieldType
from ganda.core.oracle import run_case
from ganda.core.provider import EngineError
from ganda.core.reporter import report_bug
from ganda.core.state_check import verify_state
from tests.fake_engine import SMALL, FakeProvider

# the large profile's mechanisms at a size a unit test can afford: every size flag is
# explicit (and wins), the axes come from the profile
LARGE_SMALL = dict(
    scale="large", min_node_types=2, max_node_types=3, min_edge_types=2, max_edge_types=3,
    min_nodes_per_type=60, max_nodes_per_type=120, min_edges_per_type=100, max_edges_per_type=300,
    large_row_probability=0.3, traversal_cases=6, select_cases=15, path_cases=1, write_cases=2,
)


def _args(parser_setup, engine, flags: dict, argv):
    ap = argparse.ArgumentParser()
    parser_setup(ap, engine)
    return ap.parse_args([x for k, v in flags.items() for x in ("--" + k.replace("_", "-"), str(v))] + argv)


def _sessions(engine, instance):
    server = engine.server()
    sessions = {}
    for data_model in engine.data_models:
        sessions[data_model] = server.fresh_session(engine.database_name("t", data_model))
        sessions[data_model].apply_setup(engine.mappers[data_model](instance))
    return sessions


def test_resolve_applies_the_profile_where_a_flag_was_left_at_its_default():
    limits = FakeProvider().limits
    cfg = scale.resolve(GeneratorConfig(scale="large", max_nodes_per_type=77), limits)
    assert cfg.max_nodes_per_type == 77  # explicit
    assert cfg.min_nodes_per_type == 1000 and cfg.min_fields_per_type == 6 and cfg.empty_table_probability == 0.0
    assert cfg.binary_probability == 0.3 and cfg.large_row_probability > 0 and cfg.statements_per_request == limits.max_statements_per_request
    assert cfg.write_cases == 4 and cfg.select_cases == 40
    assert scale.resolve(cfg, limits) == cfg  # idempotent: a bundle's configuration resolves to itself
    assert scale.resolve(GeneratorConfig(), limits) == GeneratorConfig()  # the default scale changes nothing
    assert scale.resolve(GeneratorConfig(scale="large"), dataclasses.replace(limits, max_binary_bytes=0)).binary_probability == 0.0
    with pytest.raises(ValueError):
        scale.profile("huge")
    p = scale.profile_of(cfg)
    assert p.name == "large" and not p.reference and p.state_sample > 0 and not p.embed_data


def test_the_large_scale_stays_inside_the_engine_limits():
    engine = FakeProvider()
    limits = engine.limits
    cfg = engine.config_class(**LARGE_SMALL)
    seen_binary, grown, hubs_capped = False, 0, 0
    for seed in range(1, 7):
        schema, instance, _ = build(seed, cfg, engine)
        assert all(len(nt.fields) >= 6 for nt in schema.node_types)  # the profile's shape, where not overridden
        assert all(form.chunk_size == limits.max_statements_per_request for form in instance.write_forms.values())
        # a blob column carries no index, and an indexed value stays under the key bound
        for ix in schema.indexes:
            if ix.target != "node":
                continue
            for f in ix.all_fields:
                assert schema.field(ix.table, f).type not in BLOB_TYPES
                for n in instance.nodes if schema.is_base(ix.table) else instance.nodes_of(ix.table):
                    v = n.values.get(f)
                    if isinstance(v, str):
                        assert len(v.encode()) <= limits.max_index_key_bytes
        for n in instance.nodes:
            fields = {f.name: f for f in schema.query_fields(n.type_name)}
            size = 0
            for k, v in n.values.items():
                f = fields[k]
                if v is None:
                    continue
                if f.type is FieldType.BINARY:
                    seen_binary = True
                    raw = base64.b64decode(v, validate=True)
                    assert raw and len(raw) <= limits.max_binary_bytes and base64.b64encode(raw).decode() == v
                elif f.type is FieldType.STRING:
                    assert len(v) <= limits.max_string_chars
                elif f.type is FieldType.LIST:
                    assert len(v) <= limits.max_list_items
                if f.type in GROWABLE_TYPES:
                    size += len(v)
                    grown += len(v) >= 256
            assert size <= limits.max_record_bytes
        fan_out = Counter(e.from_record for e in instance.edges)
        assert max(fan_out.values()) <= limits.max_edges_per_node
        hubs_capped += max(fan_out.values()) == limits.max_edges_per_node
    assert seen_binary and grown > 20 and hubs_capped > 0


def test_the_tail_axes_at_zero_reproduce_the_default_instance_and_a_grown_row_moves_only_its_grown_values():
    engine = FakeProvider()
    base_schema, base, _ = build(3, engine.config_class(**SMALL), engine)
    chunked = build(3, engine.config_class(**SMALL, statements_per_request=7), engine)[1]
    assert [n.values for n in chunked.nodes] == [n.values for n in base.nodes] and chunked.edges == base.edges
    assert all(form.chunk_size == 7 for form in chunked.write_forms.values()) and all(form.chunk_size == 0 for form in base.write_forms.values())
    moved = 0
    for seed in range(1, 7):
        base_schema, base, _ = build(seed, engine.config_class(**SMALL), engine)
        grown_schema, grown, _ = build(seed, engine.config_class(**SMALL, large_row_probability=1.0), engine)
        assert grown_schema.indexes == base_schema.indexes and grown.edges == base.edges
        assert [n.record_id for n in grown.nodes] == [n.record_id for n in base.nodes]
        for a, b in zip(base.nodes, grown.nodes):
            assert set(a.values) == set(b.values)
            for k, v in a.values.items():
                if b.values[k] != v:
                    assert base_schema.field(a.type_name, k).type in GROWABLE_TYPES and len(b.values[k]) >= 256
                    moved += 1
    assert moved > 0
    base_schema, base, _ = build(3, engine.config_class(**SMALL), engine)

    binary = build(3, engine.config_class(**SMALL, binary_probability=0.7), engine)
    for nt, bt in zip(base_schema.node_types, binary[0].node_types):
        for f, g in zip(nt.fields, bt.fields):
            assert g.type == f.type or (f.type is FieldType.STRING and g.type is FieldType.BINARY)
    for a, b in zip(base.nodes, binary[1].nodes):
        for k, v in a.values.items():
            if base_schema.field(a.type_name, k).type == binary[0].field(a.type_name, k).type:
                assert b.values.get(k) == v  # only the retyped column's values move


def test_a_large_campaign_runs_without_the_reference_on_a_sampled_state(tmp_path):
    engine = FakeProvider()
    argv = ["--iterations", "2", "--seed", "1", "--out-dir", str(tmp_path / "bugs"), "--stats-dir", str(tmp_path / "stats")]
    args = _args(campaign.add_arguments, engine, LARGE_SMALL, argv)
    stats, _ = campaign.run_campaign(engine, args)
    assert stats.scale == "large" and stats.reference is False and stats.state_sample == scale.profile("large").state_sample
    assert stats.instances == 2 and stats.findings == 0 and stats.setup_asymmetries == 0 and stats.cases > 0 and stats.writes > 0
    assert args.embed_data is False and args.max_value_chars > 0 and args.query_timeout_ms == scale.profile("large").query_timeout_ms
    # an explicit flag wins over the profile
    args = _args(campaign.add_arguments, engine, LARGE_SMALL, argv + ["--reference", "--state-sample", "0"])
    stats, _ = campaign.run_campaign(engine, args)
    assert stats.reference is True and stats.state_sample == 0 and stats.findings == 0
    # the default scale: the reference leg on, the state read whole
    args = _args(campaign.add_arguments, engine, SMALL, argv)
    stats, _ = campaign.run_campaign(engine, args)
    assert stats.scale == "default" and stats.reference is True and stats.state_sample == 0 and args.embed_data is True


def test_the_sampled_state_check_sees_counts_and_the_heaviest_rows():
    engine = FakeProvider()
    cfg = engine.config_class(**SMALL, large_row_probability=0.5)
    schema, instance, _ = build(2, cfg, engine)
    sessions = _sessions(engine, instance)
    for dm in engine.data_models:
        assert verify_state(sessions[dm], dm, instance, sample=3).ok
    # a lost record shows in the count although the sample never reads it
    table = max(schema.node_types, key=lambda nt: len(instance.nodes_of(nt.name))).name
    victim = instance.nodes_of(table)[-1]
    s = sessions["graph"]
    s.instance = dataclasses.replace(s.instance, nodes=[n for n in s.instance.nodes if n.record_id != victim.record_id])
    d = verify_state(s, "graph", instance, sample=1)
    assert any("records stored" in p for p in d.problems), d.problems
    # the heaviest row of its class is always sampled: its damaged value is seen
    s = sessions["rel"]
    heavy = max(instance.nodes, key=lambda n: sum(len(v) for v in n.values.values() if isinstance(v, (str, list))))
    field_name = next(k for k, v in heavy.values.items() if v is not None)
    rows = [dataclasses.replace(n, values={**n.values, field_name: "damaged"}) if n.record_id == heavy.record_id else n for n in s.instance.nodes]

    s.instance = dataclasses.replace(s.instance, nodes=rows)
    d = verify_state(s, "rel", instance, sample=1)
    assert any(heavy.record_id in p and field_name in p for p in d.problems), d.problems
    # a lost relationship shows in the relationship count
    s = sessions["doc"]
    s.instance = dataclasses.replace(s.instance, edges=s.instance.edges[1:])
    assert any("relationships stored" in p for p in verify_state(s, "doc", instance, sample=1).problems)


def test_the_write_phase_samples_the_state_from_a_fresh_stream_per_check():
    import random

    from ganda.core.generators.write_phase import run_write_phase

    engine = FakeProvider()
    cfg = engine.config_class(**{**LARGE_SMALL, "write_cases": 3})

    schema, instance, rng = build(2, cfg, engine)
    sessions = _sessions(engine, instance)
    # a sample smaller than the classes, so the stream is drawn from on every check
    wp = run_write_phase(sessions, instance, rng.for_generator("ir_write"), cfg.write_cases, reference=False, state_sample=3, state_rng=lambda: random.Random("s"))
    assert wp.steps == cfg.write_cases and not wp.stopped and all(r.ok for r in wp.results), wp.stopped
    wp = run_write_phase(sessions, instance, rng.for_generator("ir_write"), 1, reference=False, state_sample=3, state_rng=random.Random("s"))
    assert not wp.stopped


def test_a_slim_bundle_carries_the_shape_of_the_data(tmp_path):

    engine = FakeProvider()
    cfg = engine.config_class(**SMALL, large_row_probability=0.5)
    schema, instance, rng = build(4, cfg, engine)
    sessions = _sessions(engine, instance)
    sessions["graph"].tamper = lambda rows: rows[1:]
    plans = {dm: engine.mappers[dm](instance) for dm in engine.data_models}
    failing = next(r for c in all_cases(rng, schema, instance, cfg=cfg) if not (r := run_case(c, sessions, None)).ok)
    d = report_bug(engine, str(tmp_path), "t", 4, cfg, instance, plans, failing, embed_data=False, max_value_chars=24)
    recorded = json.load(open(os.path.join(d, "instance.json")))
    assert "summary" in recorded and recorded["nodes"] == {nt.name: len(instance.nodes_of(nt.name)) for nt in schema.node_types} and "schema" in recorded
    assert not isinstance(recorded["nodes"], list)
    plan = json.load(open(os.path.join(d, "graph_setup.json")))
    assert "first_steps" in plan and plan["steps"]
    text = open(os.path.join(d, "queries.json")).read()
    assert not any(len(line) > 2000 for line in text.splitlines())
    full = report_bug(engine, str(tmp_path / "full"), "t", 4, cfg, instance, plans, failing)
    assert isinstance(json.load(open(os.path.join(full, "instance.json")))["nodes"], list)


class _SlowSession:
    """A data model that gives up: every query times out."""

    def execute(self, text, binds=None, options=None):
        raise EngineError("gave up", timeout=True)


def test_a_timed_out_variant_is_left_out_of_the_comparison_not_read_as_a_rejection():
    engine = FakeProvider()
    cfg = engine.config_class(**SMALL)
    schema, instance, rng = build(1, cfg, engine)
    sessions = _sessions(engine, instance)
    cases = all_cases(rng, schema, instance, cfg=cfg)
    sessions["graph"] = _SlowSession()
    results = [run_case(c, sessions, instance, timeouts_are_errors=False) for c in cases]
    assert results and all(r.ok for r in results)
    assert all("graph" in r.timeouts and "graph" not in r.variants and not r.error_asymmetry for r in results)
    # at the default scale a timeout is the rejection it is: an error asymmetry, and counted
    strict = [run_case(c, sessions, instance) for c in cases]
    assert all(r.finding_class == "error_asymmetry" and "graph" in r.timeouts for r in strict)
    # a rejection on the same data model is still a finding
    rejecting = type("R", (), {"execute": lambda self, text, binds=None, options=None: (_ for _ in ()).throw(EngineError("no"))})()
    sessions["graph"] = rejecting
    assert any(run_case(c, sessions, instance).finding_class == "error_asymmetry" for c in cases)


def test_a_case_with_every_variant_timed_out_asserts_nothing():
    engine = FakeProvider()
    case = QueryCase("ir_select", "", Comparison.SINGLE_VALUE, {"doc": "x", "graph": "x", "rel": "x"}, engine=engine)
    r = run_case(case, {dm: _SlowSession() for dm in engine.data_models}, None, timeouts_are_errors=False)
    assert r.ok and set(r.timeouts) == set(engine.data_models) and not r.asserts_anything and not r.variants
    assert run_case(case, {dm: _SlowSession() for dm in engine.data_models}, None).symmetric_error

