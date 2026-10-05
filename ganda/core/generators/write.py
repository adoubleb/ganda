"""Samples `WriteQuery`s and each data model's concrete syntax of them (the `ir_write` query generator).

The grammar: with `EDGE_PROBABILITY` an edge type with attributes and relationships,
else a class with records and fields; an op among those the target admits
(`write_ast.NODE_OPS` / `EDGE_OPS`); the written field among the columns the op admits
(arithmetic: an integer column; MERGE: a scalar one); a filter with
`WHERE_PROBABILITY` (`predicates.draw_over`, as the select IR draws it) else every
record; a value from the column's own values or a fresh one of its type
(`generator.random_value`, exact on the wire); a `RETURN BEFORE|AFTER` for a node
update with `RETURNING_PROBABILITY`. A write is kept only if it is safe by
construction:

- arithmetic stays exact on every matched value (`predicates._arith_exact`: a long
  never wraps, an INTEGER stays int32; `*` on longs only);
- no unique or dictionary index of any data model ends up holding a key twice, and none
  covering the written field is written through more than one record (a unique
  index is checked per row, in an order that differs per data model, and a failed
  statement keeps what it wrote);
- an UPSERT names an existing record or a fresh id.

Each data model then draws its concrete syntax (`write_syntax`) among the applicable ones no
guard names (`known_bugs.write_known`); if some data model has none, the write is drawn
again. Every draw comes from the `ir_write` sub-stream.
"""
from __future__ import annotations

import random

from ganda.core.model import COLLECTION_TYPES, INT_TYPES, FieldType, Instance, Schema
from ganda.core.ast import predicates
from ganda.core.ast.write import EDGE_OPS, NODE_OPS, RETURNING_OPS, WriteQuery, apply, edge_hits, node_hits

EDGE_PROBABILITY = 0.3
WHERE_PROBABILITY = 0.85
RETURNING_PROBABILITY = 0.4
PRESENT_VALUE_PROBABILITY = 0.3
ATTEMPTS = 12
_ARITH = {FieldType.INT: (("+", 1), ("+", 7), ("-", 3), ("+", 1000), ("*", 2), ("*", -1)), FieldType.INT32: (("+", 1), ("+", 7), ("-", 3), ("+", 1000))}


def _present(values: list) -> list:
    return [v for v in values if v is not None]


def _value(rng: random.Random, ftype: FieldType, column: list):
    from ganda.orientdb.data_generator import random_value  # TODO(core): the data generator

    present = _present(column)
    if present and rng.random() < PRESENT_VALUE_PROBABILITY:
        return rng.choice(present)
    return random_value(rng, ftype)


def _field_pool(op: str, fields) -> list:
    base = op.removeprefix("edge_")
    if base == "arith":
        return [f for f in fields if f.type in INT_TYPES]
    if base == "merge":
        return [f for f in fields if f.type not in COLLECTION_TYPES]
    if base in ("delete",):
        return []
    return list(fields)


def _unique_indexes(schema: Schema, q: WriteQuery) -> list:
    one_per_key = schema.engine.physical_design.holds_one_record_per_key
    if q.edge:
        return [ix for ix in schema.indexes if ix.target == "edge" and ix.table == q.edge_type and one_per_key(ix)]
    return [ix for ix in schema.indexes if ix.target == "node" and one_per_key(ix) and ix.table in schema.poly_tables(q.table)]


def unique_safe(q: WriteQuery, before: Instance, after: Instance) -> bool:
    from ganda.orientdb.data_generator import index_key  # TODO(core): the data generator

    schema = before.schema
    ixs = _unique_indexes(schema, q)
    if not ixs:
        return True
    hits = edge_hits(q, before) if q.edge else node_hits(q, before)
    for ix in ixs:
        if q.field in ix.all_fields and len(hits) > 1 and q.op not in ("delete", "edge_delete"):
            return False
        rows = [e.values for e in after.edges_of(ix.table)] if q.edge else [n.values for n in after.nodes_of(ix.table)]
        seen = set()
        for values in rows:
            if ix.ignore_nulls and any(values.get(f) is None for f in ix.all_fields):
                continue
            key = index_key(schema, ix, values, edge=q.edge)
            if key in seen:
                return False
            seen.add(key)
    return True


def _arith_ok(engine, ftype: FieldType, sym: str, c: int, values: list) -> bool:
    return all(engine.rewrites.arith_exact(v, sym, c, ftype) for v in values if v is not None)


def _draw(rng: random.Random, schema: Schema, instance: Instance, step: int) -> "WriteQuery | None":
    edge_types = [et for et in schema.edge_types if et.fields and instance.edges_of(et.name)]
    if edge_types and rng.random() < EDGE_PROBABILITY:
        et = rng.choice(edge_types)
        op = rng.choice(EDGE_OPS)
        rows = [e.values for e in instance.edges_of(et.name)]
        fields = et.fields
        on, target = "edge", {"edge_type": et.name}
    else:
        tables = [nt for nt in schema.query_tables() if schema.query_fields(nt.name) and instance.nodes_of(nt.name)]
        if not tables:
            return None
        nt = rng.choice(tables)
        op = rng.choice([o for o in NODE_OPS if not (o == "upsert" and schema.is_base(nt.name))])
        rows = [n.values for n in instance.nodes_of(nt.name)]
        fields = schema.query_fields(nt.name)
        on, target = "source", {"table": nt.name}
    field = None
    if op not in ("delete", "edge_delete"):
        pool = _field_pool(op, fields)
        if not pool:
            return None
        field = rng.choice(pool)
    values_of = lambda f: [r.get(f.name) for r in rows if f.name in r]  # noqa: E731
    where = None
    if op != "upsert" and rng.random() < WHERE_PROBABILITY:
        where = predicates.draw_over(rng, on, fields, values_of, "p", schema.engine)
    kw: dict = dict(target, op=op, where=where, field=field.name if field else None)
    if op in ("set", "merge", "upsert", "edge_set"):
        kw["value"] = _value(rng, field.type, values_of(field))
        if op == "merge" and kw["value"] is None:
            return None  # a null in a MERGE literal is stripped, not written
    elif op in ("arith", "edge_arith"):
        sym, c = rng.choice(_ARITH[field.type])
        kw["sym"], kw["value"] = sym, c
    if op == "upsert":
        if rng.random() < 0.5:
            kw["target_id"] = rng.choice(instance.nodes_of(target["table"])).record_id
        else:
            kw["new_key"] = f"w{step}"
            kw["target_id"] = f"{target['table']}/w{step}"
            if instance.node_by_id(kw["target_id"]) is not None:
                return None
    if op in RETURNING_OPS and rng.random() < RETURNING_PROBABILITY:
        kw["returning"] = rng.choice(("before", "after"))
    q = WriteQuery(**kw)
    if op in ("arith", "edge_arith"):
        hits = edge_hits(q, instance) if q.edge else node_hits(q, instance)
        if not _arith_ok(schema.engine, field.type, q.sym, q.value, [(e.values if q.edge else e.values).get(q.field) for e in hits]):
            return None
    if not unique_safe(q, instance, apply(q, instance)):
        return None
    return q


def choose_syntaxes(q: WriteQuery, rewrites, rng: random.Random, schema: Schema, instance: Instance, keep_known_bugs: bool = False) -> "dict | None":
    """data model -> (concrete syntax, known bug or None): a concrete syntax no guard names, drawn
    uniformly; with `keep_known_bugs` any applicable one (the finding recorded)."""
    syn, write_known = schema.engine.write_syntax, schema.engine.known_bugs.write_known
    out = {}
    for data_model in schema.engine.data_models:
        options = [sp for sp in syn.all_syntaxes(q, data_model) if syn.applicable(q, sp, rewrites, data_model)]
        rng.shuffle(options)
        judged = [(sp, write_known(q, sp, rewrites, data_model, schema, instance)) for sp in options]
        safe = [x for x in judged if x[1] is None]
        if keep_known_bugs and judged:
            out[data_model] = judged[0]
        elif safe:
            out[data_model] = safe[0]
        else:
            return None
    return out


def sample_write(rng: random.Random, schema: Schema, instance: Instance, step: int, keep_known_bugs: bool = False) -> "tuple | None":
    """(query, rewrites, data model -> (concrete syntax, known)), or None after `ATTEMPTS` draws."""
    for _ in range(ATTEMPTS):
        q = _draw(rng, schema, instance, step)
        if q is None:
            continue
        rewrites = schema.engine.write_syntax.build_rewrites(q, rng, schema, instance)
        syntaxes = choose_syntaxes(q, rewrites, rng, schema, instance, keep_known_bugs)
        if syntaxes is None:
            continue
        return q, rewrites, syntaxes
    return None
