import dataclasses
from collections import Counter

from ganda.orientdb.facade.generate import GeneratorConfig, build, initial_node_values, write_order_states

from ganda.core.data.model import FieldType, NodeDelete, NodeUpdate
from ganda.orientdb.storage.dialect import is_valid_key


def test_build_is_deterministic():
    a = build(7, GeneratorConfig())
    b = build(7, GeneratorConfig())
    assert a[0] == b[0]
    assert [n.record_id for n in a[1].nodes] == [n.record_id for n in b[1].nodes]
    assert [(e.record_id, e.from_record, e.to_record) for e in a[1].edges] == [(e.record_id, e.from_record, e.to_record) for e in b[1].edges]


def test_tail_axes_do_not_move_the_instance():
    base = build(11, GeneratorConfig(custom_key_probability=0.0, write_form_probability=0.0, id_index_probability=0.0))[1]
    tail = build(11, GeneratorConfig(custom_key_probability=0.0, write_form_probability=0.9, id_index_probability=0.9))[1]
    assert [n.values for n in base.nodes] == [n.values for n in tail.nodes]
    assert [(e.from_record, e.to_record, e.values) for e in base.edges] == [(e.from_record, e.to_record, e.values) for e in tail.edges]
    assert base.schema.indexes == tail.schema.indexes


def test_values_have_declared_types():
    for seed in range(1, 15):
        schema, inst, _ = build(seed, GeneratorConfig(boundary_value_probability=0.5))
        for n in inst.nodes:
            for f in schema.node_type(n.type_name).fields:
                v = n.values.get(f.name)
                if v is None:
                    continue
                if f.type in (FieldType.INT, FieldType.INT32):
                    assert isinstance(v, int) and not isinstance(v, bool)
                elif f.type in (FieldType.FLOAT, FieldType.FLOAT32, FieldType.DECIMAL):
                    assert isinstance(v, float)
                elif f.type is FieldType.STRING:
                    assert isinstance(v, str) and "#" not in v[:1]
                elif f.type is FieldType.BOOL:
                    assert isinstance(v, bool)
                elif f.type is FieldType.LIST:
                    assert isinstance(v, list) and all(isinstance(x, int) for x in v)
                elif f.type in (FieldType.DATETIME, FieldType.DATE):
                    assert isinstance(v, str) and len(v) == 19 and (f.type is FieldType.DATETIME or v.endswith(" 00:00:00"))
                elif f.type is FieldType.MAP:
                    assert isinstance(v, dict) and all(isinstance(x, int) for x in v.values())


def test_custom_keys_are_valid_and_unique():
    schema, inst, _ = build(5, GeneratorConfig(custom_key_probability=1.0))
    ids = [n.record_id for n in inst.nodes]
    assert len(ids) == len(set(ids))
    assert all(is_valid_key(n.key) for n in inst.nodes)


def test_unique_and_dictionary_indexes_are_demoted_on_collisions():
    for seed in range(1, 40):
        schema, inst, _ = build(seed, GeneratorConfig(index_probability=0.9, cardinality_control_probability=0.9))
        states = list(write_order_states(inst))
        for ix in schema.indexes:
            if ix.target != "node" or ix.kind not in ("UNIQUE", "UNIQUE_HASH_INDEX", "DICTIONARY"):
                continue
            for state in states:
                seen = set()
                for rid, values in state.items():
                    if not rid.startswith(ix.table + "/"):
                        continue
                    key = tuple(repr(values.get(f)) for f in ix.all_fields)
                    if ix.ignore_nulls and any(values.get(f) is None for f in ix.all_fields):
                        continue
                    assert key not in seen, (seed, ix)
                    seen.add(key)


def test_node_delete_removes_incident_edges():
    for seed in range(1, 30):
        schema, inst, _ = build(seed, GeneratorConfig(churn_node_delete_probability=0.5))
        alive = {n.record_id for n in inst.nodes}
        for e in inst.edges:
            assert e.from_record in alive and e.to_record in alive
        for op in inst.churn:
            if isinstance(op, NodeDelete):
                assert op.node.record_id not in alive
                assert all(op.node.record_id in (e.from_record, e.to_record) for e in op.incident_edges)


def test_initial_values_differ_from_final_for_updated_nodes():
    schema, inst, _ = build(3, GeneratorConfig(churn_update_probability=0.9))
    initial = initial_node_values(inst)
    updates = [op for op in inst.churn if isinstance(op, NodeUpdate)]
    assert updates
    for op in updates:
        assert initial[op.node.record_id] == op.initial_values


def test_config_with():
    cfg = GeneratorConfig()
    assert cfg.with_(max_nodes_per_type=3).max_nodes_per_type == 3
    assert dataclasses.replace(cfg) == cfg


def test_inheritance_axis_is_a_tail_axis():
    base = build(21, GeneratorConfig(inheritance_probability=0.0))[1]
    tail = build(21, GeneratorConfig(inheritance_probability=1.0))[1]
    assert base.schema.base is None and tail.schema.base is not None
    own = {f.name for nt in tail.schema.node_types for f in nt.fields}
    assert all(f.name.startswith("b") and f.name not in own for f in tail.schema.base.fields)
    for a, b in zip(base.nodes, tail.nodes):
        assert a.record_id == b.record_id
        assert {k: v for k, v in b.values.items() if not k.startswith("b")} == a.values
    assert [(e.from_record, e.to_record) for e in base.edges] == [(e.from_record, e.to_record) for e in tail.edges]
    schema = tail.schema
    for nt in schema.node_types:
        assert set(schema.query_fields(nt.name)) == set(nt.fields) | set(schema.base.fields)
    assert tail.nodes_of(schema.base.name) == tail.nodes
    assert schema.base in schema.query_tables()
    for ix in schema.indexes:
        if ix.table == schema.base.name:
            assert schema.node_indexes("n1", ix.field, ix.data_model if ix.data_model != "both" else "doc")
            assert schema.node_indexes(schema.base.name, ix.field, ix.data_model if ix.data_model != "both" else "doc")


def test_inheritance_ddl_and_cases():
    from ganda.orientdb.storage.mapping import MAPPERS
    from ganda.core.query.generators.registry import all_cases, reference_for

    found_poly = False
    for seed in range(1, 12):
        schema, inst, rng = build(seed, GeneratorConfig(inheritance_probability=1.0))
        for data_model, mapper in MAPPERS.items():
            plan = mapper(inst)
            texts = [s.command for s in plan if hasattr(s, "command")]
            assert any(t.startswith("CREATE CLASS N0") and t.endswith("ABSTRACT") for t in texts)
            assert all(f"EXTENDS N0" in t for t in texts if t.startswith("CREATE CLASS n"))
            assert not any(t.startswith("CREATE PROPERTY n") and t.endswith(".id STRING") for t in texts)
        for c in all_cases(rng, schema, inst, traversal_cases=0, select_cases=30):
            has, ref = reference_for(c, inst)
            assert has or c.params.get("reference") is False
            if c.params.get("table") == "N0":
                found_poly = True
    assert found_poly


def test_new_axes_are_tail_axes():
    import struct
    from decimal import Decimal

    from ganda.core.data.model import EdgeUpdate

    base = build(31, GeneratorConfig())[1]
    tail = build(31, GeneratorConfig(narrow_type_probability=1.0, collate_probability=1.0, clusters_probability=1.0, list_index_probability=1.0, churn_edge_update_probability=0.5, write_form_probability=1.0))[1]
    assert [n.record_id for n in base.nodes] == [n.record_id for n in tail.nodes]
    assert [(e.from_record, e.to_record) for e in base.edges] == [(e.from_record, e.to_record) for e in tail.edges]
    schema = tail.schema
    types = {f.type for nt in schema.node_types for f in nt.fields}
    assert FieldType.INT not in types and FieldType.FLOAT not in types
    for nt in schema.node_types:
        assert nt.clusters in (2, 3)
        for f in nt.fields:
            if f.type is FieldType.STRING:
                assert f.ci
            if f.type is FieldType.LIST:
                assert schema.node_indexes(nt.name, f.name, "doc") or schema.node_indexes(nt.name, f.name, "graph") or schema.node_indexes(nt.name, f.name, "rel")
    for n in tail.nodes:
        for f in schema.query_fields(n.type_name):
            v = n.values.get(f.name)
            if v is None:
                continue
            if f.type is FieldType.INT32:
                assert -(2**31) <= v < 2**31
            elif f.type is FieldType.FLOAT32:
                assert struct.unpack("f", struct.pack("f", v))[0] == v and float(repr(v)) == v
            elif f.type is FieldType.DECIMAL:
                assert float(Decimal(repr(v))) == v and repr(v) != "-0.0"
    assert any(isinstance(op, EdgeUpdate) for op in tail.churn)
    for data_model in ("doc", "graph", "rel"):
        form = tail.write_form(data_model)
        assert form.edge_update_form in ("set", "merge", "content")
    # the untouched fields keep their values
    for a, b in zip(base.nodes, tail.nodes):
        for k, v in a.values.items():
            f = schema.field(a.type_name, k)
            if f.type in (FieldType.INT, FieldType.BOOL, FieldType.LIST):
                assert b.values.get(k) == v


def test_new_axes_ddl_and_churn():
    from ganda.orientdb.storage.mapping import MAPPERS

    schema, inst, _ = build(32, GeneratorConfig(narrow_type_probability=1.0, collate_probability=1.0, clusters_probability=1.0, churn_edge_update_probability=0.5, write_form_probability=0.0))
    for data_model, mapper in MAPPERS.items():
        texts = [s.command for s in mapper(inst) if hasattr(s, "command")]
        assert any("CLUSTERS" in t for t in texts if t.startswith("CREATE CLASS n"))
        assert any(t.startswith("ALTER PROPERTY") and t.endswith("COLLATE ci") for t in texts)
        assert any(" INTEGER" in t or " FLOAT" in t or " DECIMAL" in t for t in texts if t.startswith("CREATE PROPERTY"))
        if data_model == "graph":
            assert any(t.startswith("UPDATE EDGE") for t in texts)
        elif data_model == "rel":
            assert any(t.startswith("UPDATE e") and "WHERE eid = :k" in t for t in texts)


def test_date_and_map_axes():
    import datetime as _dt

    from ganda.orientdb.storage.mapping import MAPPERS
    from ganda.core.query.generators.registry import all_cases, reference_for

    base = build(41, GeneratorConfig())[1]
    tail = build(41, GeneratorConfig(date_map_probability=1.0, list_index_probability=1.0))[1]
    assert [n.record_id for n in base.nodes] == [n.record_id for n in tail.nodes]
    schema = tail.schema
    types = {f.type for nt in schema.node_types for f in nt.fields}
    assert FieldType.INT not in types and FieldType.LIST not in types
    for n in tail.nodes:
        for f in schema.query_fields(n.type_name):
            v = n.values.get(f.name)
            if v is None:
                continue
            if f.type in (FieldType.DATETIME, FieldType.DATE):
                _dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
            elif f.type is FieldType.MAP:
                assert set(v) <= {"k1", "k2", "k3", "k4"}
    # a seed whose schema holds a LIST field, now a map with an index on it
    mapped = next(build(seed, GeneratorConfig(date_map_probability=1.0, list_index_probability=1.0))[1] for seed in range(41, 80) if any(f.type is FieldType.LIST for nt in build(seed, GeneratorConfig())[0].node_types for f in nt.fields))
    assert any(ix.by in ("KEY", "VALUE") for ix in mapped.schema.indexes)
    ddl = []
    for data_model, mapper in MAPPERS.items():
        texts = [s.command for s in mapper(tail) if hasattr(s, "command")]
        assert any(" DATETIME" in t or " DATE" in t for t in texts if t.startswith("CREATE PROPERTY"))
        texts = [s.command for s in mapper(mapped) if hasattr(s, "command")]
        assert any("EMBEDDEDMAP LONG" in t for t in texts)
        ddl += texts
    # the map index sits on the data model(s) its spec names
    assert any(" BY KEY)" in t or " BY VALUE)" in t for t in ddl if t.startswith("CREATE INDEX"))
    seen = set()
    for seed in range(41, 47):
        schema, inst, rng = build(seed, GeneratorConfig(date_map_probability=0.7, list_index_probability=1.0, index_probability=0.6, inheritance_probability=0.5))
        for c in all_cases(rng, schema, inst, select_cases=30):
            has, ref = reference_for(c, inst)
            assert has or c.params.get("reference") is False, c.generator_name
            seen |= {s_.split(":", 1)[1] for s_ in c.queries if ":" in s_}
    assert {"index", "poly", "polyin"} <= seen


def test_binary_columns_hold_base64_values_and_carry_no_index():
    import base64

    seen = False
    for seed in range(1, 25):
        schema, inst, _ = build(seed, GeneratorConfig(binary_probability=0.6, index_probability=0.9, composite_index_probability=0.5, inheritance_probability=0.5))
        for nt in schema.query_tables():
            for f in schema.query_fields(nt.name):
                if f.type is not FieldType.BINARY:
                    continue
                seen = True
                assert not any(ix.target == "node" and f.name in ix.all_fields and ix.table in schema.poly_tables(nt.name) for ix in schema.indexes), (seed, nt.name, f.name)
                for n in inst.nodes_of(nt.name):
                    v = n.values.get(f.name)
                    if v is not None:
                        raw = base64.b64decode(v, validate=True)
                        assert raw and base64.b64encode(raw).decode() == v
    assert seen
    # a tail axis: a column the axis leaves alone keeps its values
    base_schema, base, _ = build(5, GeneratorConfig())
    bin_schema, binary, _ = build(5, GeneratorConfig(binary_probability=0.6))
    for a, b in zip(base.nodes, binary.nodes):
        for k, v in a.values.items():
            if base_schema.field(a.type_name, k).type == bin_schema.field(a.type_name, k).type:
                assert b.values.get(k) == v


def test_the_incremental_collision_check_matches_the_state_walk():
    from ganda.core.data.generator import _collides, index_key

    def by_states(instance, ix) -> bool:
        fields = ix.all_fields
        base = instance.schema.is_base(ix.table)
        for state in write_order_states(instance):
            seen = set()
            for rid, values in state.items():
                if not base and not rid.startswith(ix.table + "/"):
                    continue
                if ix.ignore_nulls and any(values.get(f) is None for f in fields):
                    continue
                key = index_key(instance.schema, ix, values)
                if key in seen:
                    return True
                seen.add(key)
        return False

    judged = Counter()
    colliding = dict(index_probability=0.9, composite_index_probability=0.5, cardinality_control_probability=0.9, demote_colliding_indexes=False, churn_update_probability=0.4, churn_node_delete_probability=0.2, inheritance_probability=0.5)
    sparse = dict(colliding, cardinality_control_probability=0.0, boundary_value_probability=0.0, value_reuse_probability=0.0)
    for seed in range(1, 30):
        for kw in (colliding, sparse):
            schema, inst, _ = build(seed, GeneratorConfig(**kw))
            for ix in schema.indexes:
                if ix.target == "node":
                    got = _collides(inst, ix)
                    assert got == by_states(inst, ix), (seed, ix)
                    judged[got] += 1
    assert judged[True] > 10 and judged[False] > 10

