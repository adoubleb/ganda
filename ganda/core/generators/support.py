"""Engine-neutral building blocks of the query generators: the AND over a composite
index's fields, the transaction door's write and the rows it leaves, and the BFS
distances of the path IR."""
from __future__ import annotations

import dataclasses

from ganda.core.ir import predicates
from ganda.core.generators import predicates as sampler
from ganda.core.ir.predicates import Compound
from ganda.core.ir.select import TX_WRITES, TxWrite
from ganda.core.model import Instance, NodeRow, Schema


def composite_and(rng, fields, values_of, engine) -> "Compound | None":
    children = []
    for k, f in enumerate(fields):
        leaf = sampler.draw(rng, "source", f, values_of(f), f"p_{k}", engine)
        if leaf is None:
            return None
        children.append(leaf)
    return Compound("source", "AND", tuple(children)) if len(children) >= 2 else None




def tx_rows(instance: Instance, tx: TxWrite) -> list:
    """The class's rows with the transaction's write applied."""
    rows = list(instance.nodes_of(tx.table))
    write, tid = tx.write, tx.tid
    if write == "delete":
        return [n for n in rows if n.record_id != tid]
    if write == "insert":
        return rows + [NodeRow(tx.table, -1, {tx.field: tx.tv}, custom_key="tx0")]
    out = []
    for n in rows:
        if n.record_id != tid:
            out.append(n)
            continue
        values = dict(n.values)
        if tx.remove:
            values.pop(tx.field, None)
        else:
            values[tx.field] = tx.tv
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


def _unique_on_any_data_model(schema: Schema, table: str, field_name: str) -> bool:
    engine = schema.engine
    return any(engine.physical_design.is_unique(ix) for data_model in engine.data_models for ix in schema.node_indexes(table, field_name, data_model))


def draw_tx(rng, schema: Schema, instance: Instance, table: str, where) -> "TxWrite | None":
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
    tid, tv, remove = target.record_id, None, False
    if write == "delete":
        return TxWrite(table, write, f.name, tid)
    roll = rng.random()
    present = [n.values.get(f.name) for n in rows if n.values.get(f.name) is not None]
    unique = _unique_on_any_data_model(schema, table, f.name)
    if write == "update" and roll < 0.15:
        remove = True
    elif write == "update" and roll < 0.25:
        pass
    elif present and roll < 0.55 and not unique:
        tv = rng.choice(present)
    else:
        v = random_value(rng, f.type)
        if unique:
            for _ in range(10):
                if all(value_key(v) != value_key(x) for x in present):
                    break
                v = random_value(rng, f.type)
            else:
                v, remove = None, True
        tv = v
    if write == "insert":
        tid, remove = f"{table}/tx0", False
    return TxWrite(table, write, f.name, tid, tv, remove)


def tx_binds(tx: TxWrite) -> dict:
    return {"tid": tx.tid} if tx.write == "delete" else {"tid": tx.tid, "tv": tx.tv}
