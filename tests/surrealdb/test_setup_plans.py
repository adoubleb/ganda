"""The setup plans: one script per data model, every record addressed by its record id,
every value a typed literal; the doors and the chunking."""
import json

from ganda.core.data.model import WriteForm
from ganda.core.engine.setup_plan import plan_from_dict, plan_to_dict
from ganda.surrealdb.concrete_syntax.setup import MAPPERS
from ganda.surrealdb.concrete_syntax.setup import common
from ganda.surrealdb.engine.dialect import engine_id
from ganda.surrealdb.engine.plan import Surql
from tests.surrealdb.helpers import EVERY_AXIS, GeneratorConfig, build


def _text(plan) -> str:
    return "\n".join(s.text for s in plan)


def test_plans_round_trip_through_json():
    for seed in range(1, 12):
        _, inst, _ = build(seed, GeneratorConfig(**EVERY_AXIS))
        for mapper in MAPPERS.values():
            plan = mapper(inst)
            assert all(isinstance(s, Surql) for s in plan)
            encoded = json.dumps(plan_to_dict(plan), ensure_ascii=False)
            assert plan_to_dict(plan_from_dict(json.loads(encoded))) == plan_to_dict(plan)


def test_every_record_of_the_instance_is_written_on_every_data_model():
    for seed in range(1, 16):
        _, inst, _ = build(seed, GeneratorConfig(**EVERY_AXIS))
        for data_model, mapper in MAPPERS.items():
            text = _text(mapper(inst))
            for n in inst.nodes:
                assert engine_id(n.record_id) in text, (seed, data_model, n.record_id)
            for e in inst.edges:
                needle = f'"{e.record_id}"' if data_model == "doc" else engine_id(e.record_id)
                assert needle in text, (seed, data_model, e.record_id)


def test_a_schemafull_class_declares_every_field_and_a_schemaless_one_none():
    found_full = found_less = False
    for seed in range(1, 12):
        schema, inst, _ = build(seed, GeneratorConfig(**EVERY_AXIS))
        for data_model, mapper in MAPPERS.items():
            ddl = mapper(inst)[0].text
            for nt in schema.node_types:
                if data_model in nt.option("schemafull", ()):
                    found_full = True
                    assert f"DEFINE TABLE {nt.name} SCHEMAFULL;" in ddl
                    for f in schema.query_fields(nt.name):
                        assert f"DEFINE FIELD {f.name} ON {nt.name} TYPE option<" in ddl, (seed, data_model, nt.name, f.name)
                else:
                    found_less = True
                    assert f"DEFINE TABLE {nt.name} SCHEMALESS;" in ddl
                    assert f"ON {nt.name} TYPE" not in ddl
    assert found_full and found_less


def test_the_schemafull_axis_is_per_class_and_data_model():
    schema, _, _ = build(3, GeneratorConfig(schemafull_probability=0.5))
    modes = {nt.option("schemafull", ()) for nt in schema.node_types}
    assert len(modes) > 1 and any(0 < len(m) < 3 for m in modes)
    schema0, _, _ = build(3, GeneratorConfig(schemafull_probability=0.0))
    assert all(nt.option("schemafull") is None for nt in schema0.node_types)


def test_doors_and_chunking():
    statements = [f"CREATE n1:r{i} SET f = {i};" for i in range(12)]
    plain = common.wrap_data(statements, WriteForm(transaction="none"))
    assert len(plain) == 1 and "BEGIN" not in plain[0].text
    tx = common.wrap_data(statements, WriteForm(transaction="tx"))
    assert len(tx) == 1 and tx[0].text.startswith("BEGIN TRANSACTION;") and tx[0].text.endswith("COMMIT TRANSACTION;")
    chunked = common.wrap_data(statements, WriteForm(transaction="none", chunk_size=5))
    assert [len(s.text.splitlines()) for s in chunked] == [5, 5, 2]
    chunked_tx = common.wrap_data(statements, WriteForm(transaction="tx", chunk_size=5))
    assert all(s.text.startswith("BEGIN TRANSACTION;") for s in chunked_tx) and [len(s.text.splitlines()) - 2 for s in chunked_tx] == [5, 5, 2]
    # the request budget splits a chunk too: a statement too big for it travels alone
    big = "CREATE n1:big SET f = '" + "x" * (common._REQUEST_BUDGET + 10) + "';"
    pieces = common.wrap_data(statements[:3] + [big] + statements[3:6], WriteForm(chunk_size=100))
    assert [len(s.text.splitlines()) for s in pieces] == [3, 1, 3]


def test_junk_transaction_and_index_rebuild_follow_the_data():
    for seed in range(1, 10):
        _, inst, _ = build(seed, GeneratorConfig(**EVERY_AXIS))
        for data_model, mapper in MAPPERS.items():
            form = inst.write_form(data_model)
            text = _text(mapper(inst))
            assert ("CANCEL TRANSACTION;" in text) == form.cancelled_junk, (seed, data_model)
            if form.cancelled_junk:
                assert f"{inst.nodes[0].type_name}:{common.JUNK_MARKER}" in text or f"DELETE {engine_id(inst.nodes[0].record_id)};\nCANCEL" in text
            has_index = any(ix.applies_to(data_model) for ix in inst.schema.indexes)
            assert ("REBUILD INDEX" in text) == (form.rebuild_indexes and has_index), (seed, data_model)


def test_typed_literals():
    from ganda.core.data.model import FieldType
    from ganda.surrealdb.engine.dialect import surql_literal

    assert surql_literal("2024-02-29 23:59:59", FieldType.DATETIME) == 'd"2024-02-29T23:59:59Z"'
    assert surql_literal(123.456, FieldType.DECIMAL) == "123.456dec"
    assert surql_literal("AAE=", FieldType.BINARY) == 'b"0001"'
    assert surql_literal([1, 2], FieldType.LIST) == "[1, 2]"
    assert surql_literal({"k1": -1}, FieldType.MAP) == "{ k1: -1 }"
    assert surql_literal(None, FieldType.INT) == "NULL" and surql_literal(True, FieldType.BOOL) == "true"
    assert surql_literal(1.0, FieldType.FLOAT) == "1.0" and surql_literal(-0.0, FieldType.FLOAT) == "-0.0"
