"""The state verifier: after a data model's setup plan has run, read the database back and
compare it with the `Instance` -- every node with its values, every relationship with
its endpoints and attributes. This is the invariant everything else rests on (the
final state IS the Instance), checked live rather than simulated: a materializer bug,
a write form that silently drops a field, or an engine write-path defect shows up
here as a `StateDiff` before any query template can be misled by it.

A diff on ONE side with the others clean is a setup asymmetry (an engine finding on
the write path); a diff on every data model is the fuzzer's.

At scale (`sample > 0`) the whole state is not read back: each class is checked by
its record COUNT and a sample of records read by id -- the heaviest rows (the ones the
large-row axis grew) and `sample` random ones -- and each relationship type by its
count and the relationships leaving a sample of sources (the biggest hubs and `sample`
random ones), so a lost or duplicated record still shows in the count and a damaged
value in the sample. The sample is drawn from `rng` (the campaign seeds it per
instance) and is the same on every data model.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from ganda.core.data.model import Instance

# how many of the heaviest records / biggest sources a sample always holds
HEAVIEST = 8


@dataclass
class StateDiff:
    data_model: str
    problems: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _weight(values: dict) -> int:
    return sum(len(v) if isinstance(v, (str, list, dict)) else 1 for v in values.values())


def sample_ids(rows: list, sample: int, rng: random.Random, weight=None) -> list:
    """`HEAVIEST` rows by `weight` (a record's size; a source's fan-out) and `sample`
    random ones, as ids, in a stable order."""
    if sample <= 0 or len(rows) <= sample + HEAVIEST:
        return [r for r, _w in rows]
    heavy = [r for r, _w in sorted(rows, key=lambda x: -x[1])[:HEAVIEST]]
    rest = [r for r, _w in rows if r not in set(heavy)]
    return heavy + rng.sample(rest, min(sample, len(rest)))


def verify_state(session, data_model: str, instance: Instance, tables: "set | None" = None, edge_types: "set | None" = None, sample: int = 0, rng: "random.Random | None" = None) -> StateDiff:
    """The whole state, or with `tables` / `edge_types` only those node classes and
    relationship types (the write phase's check after one write); with `sample` the
    counts and a sample (see the module)."""
    diff = StateDiff(data_model)
    schema = instance.schema
    eq = schema.engine.semantics.eq
    rng = rng or random.Random(0)
    for nt in schema.node_types:
        if tables is not None and nt.name not in tables:
            continue
        expected = {n.record_id: n for n in instance.nodes_of(nt.name)}
        if sample > 0:
            count = session.count_nodes(nt.name)
            if count != len(expected):
                diff.problems.append(f"{nt.name}: {count} records stored, {len(expected)} expected")
            ids = sample_ids([(rid, _weight(n.values)) for rid, n in expected.items()], sample, rng)
            rows = session.read_nodes(nt.name, ids=ids)
            wanted = set(ids)
            rows = {rid: r for rid, r in rows.items() if rid in wanted}
            expected = {rid: expected[rid] for rid in ids}
        else:
            rows = session.read_nodes(nt.name)
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
                    diff.problems.append(f"{rid}.{f.name}: expected {_short(want)}, stored {_short(got)}")
    for et in schema.edge_types:
        if edge_types is not None and et.name not in edge_types:
            continue
        want = {e.record_id: e for e in instance.edges_of(et.name)}
        if sample > 0:
            count = session.count_edges(data_model, instance, et)
            if count != len(want):
                diff.problems.append(f"{et.name}: {count} relationships stored, {len(want)} expected")
            fan_out: dict = {}
            for e in want.values():
                fan_out[e.from_record] = fan_out.get(e.from_record, 0) + 1
            sources = sample_ids(sorted(fan_out.items()), sample, rng)
            chosen = set(sources)
            stored = [x for x in session.read_edges(data_model, instance, et, sources=sources) if x[1] in chosen]
            want = {eid: e for eid, e in want.items() if e.from_record in chosen}
        else:
            stored = session.read_edges(data_model, instance, et)
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
                    diff.problems.append(f"{eid}.{f.name}: expected {_short(w)}, stored {_short(g)}")
    return diff


def _short(v, n: int = 200) -> str:
    """A value's repr, cut for the problem list (a grown value is megabytes)."""
    r = repr(v)
    return r if len(r) <= n else f"{r[:n]}... ({len(r)} chars)"
