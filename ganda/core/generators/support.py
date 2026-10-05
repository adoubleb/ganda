"""Engine-neutral building blocks of the query generators: the AND over a composite
index's fields, the transaction door's write and the rows it leaves, and the BFS
distances of the path AST."""
from __future__ import annotations

import dataclasses

from ganda.core.ast import predicates
from ganda.core.ast.predicates import Compound
from ganda.core.model import Instance, NodeRow, Schema


def composite_and(rng, fields, values_of, engine) -> "Compound | None":
    children = []
    for k, f in enumerate(fields):
        leaf = predicates.draw(rng, "source", f, values_of(f), f"p_{k}", engine)
        if leaf is None:
            return None
        children.append(leaf)
    return Compound("source", "AND", tuple(children)) if len(children) >= 2 else None




def tx_rows(instance: Instance, p: dict) -> list:
    """The class's rows with the transaction's write applied."""
    rows = list(instance.nodes_of(p["table"]))
    write, tid = p["write"], p["tid"]
    if write == "delete":
        return [n for n in rows if n.record_id != tid]
    if write == "insert":
        return rows + [NodeRow(p["table"], -1, {p["field"]: p["tv"]}, custom_key="tx0")]
    out = []
    for n in rows:
        if n.record_id != tid:
            out.append(n)
            continue
        values = dict(n.values)
        if p["remove"]:
            values.pop(p["field"], None)
        else:
            values[p["field"]] = p["tv"]
        out.append(dataclasses.replace(n, values=values))
    return out


def bfs_distances(instance: Instance, start: str, direction: str, edge_type: "str | None") -> dict:
    """Record id -> hops of a shortest path from `start` over the edges of
    `edge_type` (every type when None) in `direction`; the start itself at 0."""
    adj: dict = {}
    for e in instance.edges:
        if edge_type is not None and e.type_name != edge_type:
            continue
        if direction in ("OUT", "BOTH"):
            adj.setdefault(e.from_record, []).append(e.to_record)
        if direction in ("IN", "BOTH"):
            adj.setdefault(e.to_record, []).append(e.from_record)
    dist = {start: 0}
    frontier = [start]
    while frontier:
        nxt = []
        for u in frontier:
            for w in adj.get(u, []):
                if w not in dist:
                    dist[w] = dist[u] + 1
                    nxt.append(w)
        frontier = nxt
    return dist


TX_WRITES = ("update", "update", "insert", "delete")


def _unique_on_any_data_model(schema: Schema, table: str, field_name: str) -> bool:
    engine = schema.engine
    return any(engine.physical_design.is_unique(ix) for data_model in engine.data_models for ix in schema.node_indexes(table, field_name, data_model))


def draw_tx(rng, schema: Schema, instance: Instance, table: str, where) -> "dict | None":
    """One write for the transaction door (`SelectQuery.tx`): a record of `table` updated
    (a field set -- most often one the filter reads -- set to null, or removed),
    inserted with one field, or deleted. A value reused from another record is never
    written to a column a UNIQUE index of any data model covers (a duplicate key inside
    the transaction raises nothing before COMMIT, and the unique lookup answers one
    of the two)."""
    from ganda.core.data_generator import random_value

    value_key = schema.engine.semantics.key

    fields = schema.query_fields(table)
    rows = instance.nodes_of(table)
    if schema.is_base(table) or not fields or not rows:
        return None
    write = rng.choice(TX_WRITES)
    target = rng.choice(rows)
    mentioned = [f for f in fields if f.name in predicates.fields_of(where)]
    f = rng.choice(mentioned) if mentioned and rng.random() < 0.75 else rng.choice(fields)
    tx = {"table": table, "write": write, "field": f.name, "tid": target.record_id, "tv": None, "remove": False}
    if write == "delete":
        return tx
    roll = rng.random()
    present = [n.values.get(f.name) for n in rows if n.values.get(f.name) is not None]
    unique = _unique_on_any_data_model(schema, table, f.name)
    if write == "update" and roll < 0.15:
        tx["remove"] = True
    elif write == "update" and roll < 0.25:
        pass
    elif present and roll < 0.55 and not unique:
        tx["tv"] = rng.choice(present)
    else:
        v = random_value(rng, f.type)
        if unique:
            for _ in range(10):
                if all(value_key(v) != value_key(x) for x in present):
                    break
                v = random_value(rng, f.type)
            else:
                v, tx["remove"] = None, True
        tx["tv"] = v
    if write == "insert":
        tx["tid"] = f"{table}/tx0"
        tx["remove"] = False
    return tx


def tx_binds(tx: dict) -> dict:
    return {"tid": tx["tid"]} if tx["write"] == "delete" else {"tid": tx["tid"], "tv": tx["tv"]}
