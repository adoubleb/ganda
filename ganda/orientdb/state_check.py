"""The state verifier: after a data model's setup plan has run, read the database back and
compare it with the `Instance` -- every node with its values, every relationship with
its endpoints and attributes. This is the invariant everything else rests on (the
final state IS the Instance), checked live rather than simulated: a materializer bug,
a write form that silently drops a field, or an engine write-path defect shows up
here as a `StateDiff` before any query template can be misled by it.

A diff on ONE side with the others clean is a setup asymmetry (an engine finding on
the write path); a diff on every data model is the fuzzer's.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ganda.orientdb.model import Instance
from ganda.orientdb.semantics import eq


@dataclass
class StateDiff:
    data_model: str
    problems: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _node_rows(session, table: str) -> dict:
    rows = session.sql(f"SELECT FROM {table}", timeout_ms=0)
    return {r["id"]: r for r in rows if "id" in r}


def _edge_rows(session, data_model: str, instance: Instance, et) -> list:
    """[(eid, from id, to id, {attrs})] as the data model stores them."""
    out = []
    if data_model == "graph":
        rows = session.sql(f"SELECT eid, out.id AS s, in.id AS t, * FROM {et.name}", timeout_ms=0)
        for r in rows:
            out.append((r.get("eid"), r.get("s"), r.get("t"), {f.name: r.get(f.name) for f in et.fields if f.name in r and r.get(f.name) is not None}))
    elif data_model == "rel":
        rows = session.sql(f"SELECT FROM {et.name}", timeout_ms=0)
        for r in rows:
            out.append((r.get("eid"), r.get("src_id"), r.get("dst_id"), {f.name: r.get(f.name) for f in et.fields if f.name in r and r.get(f.name) is not None}))
    else:
        rows = session.sql(f"SELECT id, {et.name} AS links FROM {et.from_type}", timeout_ms=0)
        for r in rows:
            for link in r.get("links") or []:
                target = link.get("target")
                tid = None
                if isinstance(target, str) and target.startswith("#"):
                    got = session.sql("SELECT id FROM " + et.to_type + " WHERE @rid = :r", {"r": target}, timeout_ms=0)
                    tid = got[0]["id"] if got else None
                elif isinstance(target, dict):
                    tid = target.get("id")
                out.append((link.get("eid"), r["id"], tid, {f.name: link.get(f.name) for f in et.fields if f.name in link and link.get(f.name) is not None}))
    return out


def verify_state(session, data_model: str, instance: Instance, tables: "set | None" = None, edge_types: "set | None" = None) -> StateDiff:
    """The whole state, or with `tables` / `edge_types` only those node classes and
    relationship types (the write phase's check after one write)."""
    diff = StateDiff(data_model)
    schema = instance.schema
    for nt in schema.node_types:
        if tables is not None and nt.name not in tables:
            continue
        rows = _node_rows(session, nt.name)
        expected = {n.record_id: n for n in instance.nodes_of(nt.name)}
        for rid in sorted(set(rows) | set(expected)):
            if rid not in rows:
                diff.problems.append(f"{nt.name}: missing node {rid}")
                continue
            if rid not in expected:
                diff.problems.append(f"{nt.name}: unexpected node {rid}")
                continue
            for f in schema.query_fields(nt.name):
                want = expected[rid].values.get(f.name)
                got = rows[rid].get(f.name)
                if want is None and got is None:
                    continue
                if not eq(want, got):
                    diff.problems.append(f"{rid}.{f.name}: expected {want!r}, stored {got!r}")
    for et in schema.edge_types:
        if edge_types is not None and et.name not in edge_types:
            continue
        stored = _edge_rows(session, data_model, instance, et)
        want = {e.record_id: e for e in instance.edges_of(et.name)}
        got = {}
        for eid, s, t, attrs in stored:
            if eid in got:
                diff.problems.append(f"{et.name}: relationship {eid} stored twice")
            got[eid] = (s, t, attrs)
        for eid in sorted(set(got) | set(want)):
            if eid not in got:
                diff.problems.append(f"{et.name}: missing relationship {eid}")
                continue
            if eid not in want:
                diff.problems.append(f"{et.name}: unexpected relationship {eid} {got[eid][:2]}")
                continue
            s, t, attrs = got[eid]
            e = want[eid]
            if s != e.from_record or t != e.to_record:
                diff.problems.append(f"{eid}: endpoints {s}->{t}, expected {e.from_record}->{e.to_record}")
            for f in et.fields:
                w = e.values.get(f.name)
                g = attrs.get(f.name)
                if w is None and g is None:
                    continue
                if not eq(w, g):
                    diff.problems.append(f"{eid}.{f.name}: expected {w!r}, stored {g!r}")
    return diff
