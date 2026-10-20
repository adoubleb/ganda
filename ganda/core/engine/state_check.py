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
from collections import Counter
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
    schema = instance.schema
    eq = schema.engine.semantics.eq
    rng = rng or random.Random(0)
    diff = StateDiff(data_model)
    for nt in schema.node_types:
        if tables is None or nt.name in tables:
            diff.problems += _node_problems(session, instance, nt, sample, rng, eq)
    for et in schema.edge_types:
        if edge_types is None or et.name in edge_types:
            diff.problems += _edge_problems(session, data_model, instance, et, sample, rng, eq)
    return diff


def _node_problems(session, instance: Instance, nt, sample: int, rng: random.Random, eq) -> list:
    problems = []
    expected = {n.record_id: n for n in instance.nodes_of(nt.name)}
    if sample > 0:
        problems += _count_problems(nt.name, session.count_nodes(nt.name), len(expected), "records")
        ids = sample_ids([(rid, _weight(n.values)) for rid, n in expected.items()], sample, rng)
        expected = {rid: expected[rid] for rid in ids}
        stored = {rid: r for rid, r in session.read_nodes(nt.name, ids=ids).items() if rid in expected}
    else:
        stored = session.read_nodes(nt.name)

    fields = instance.schema.query_fields(nt.name)
    for rid in sorted(set(stored) | set(expected)):
        if rid not in stored:
            problems.append(f"{nt.name}: missing node {rid}")
        elif rid not in expected:
            problems.append(f"{nt.name}: unexpected node {rid}")
        else:
            problems += _value_problems(rid, fields, expected[rid].values, stored[rid], eq)
    return problems


def _edge_problems(session, data_model: str, instance: Instance, et, sample: int, rng: random.Random, eq) -> list:
    problems = []
    expected = {e.record_id: e for e in instance.edges_of(et.name)}
    if sample > 0:
        problems += _count_problems(et.name, session.count_edges(data_model, instance, et), len(expected), "relationships")
        fan_out = Counter(e.from_record for e in expected.values())
        sources = sample_ids(sorted(fan_out.items()), sample, rng)
        chosen = set(sources)
        expected = {eid: e for eid, e in expected.items() if e.from_record in chosen}
        rows = [row for row in session.read_edges(data_model, instance, et, sources=sources) if row[1] in chosen]
    else:
        rows = session.read_edges(data_model, instance, et)

    stored = {}
    for eid, src, dst, attrs in rows:
        if eid in stored:
            problems.append(f"{et.name}: relationship {eid} stored twice")
        stored[eid] = (src, dst, attrs)

    for eid in sorted(set(stored) | set(expected)):
        if eid not in stored:
            problems.append(f"{et.name}: missing relationship {eid}")
            continue
        src, dst, attrs = stored[eid]
        if eid not in expected:
            problems.append(f"{et.name}: unexpected relationship {eid} {(src, dst)}")
            continue
        e = expected[eid]
        if (src, dst) != (e.from_record, e.to_record):
            problems.append(f"{eid}: endpoints {src}->{dst}, expected {e.from_record}->{e.to_record}")
        problems += _value_problems(eid, et.fields, e.values, attrs, eq)
    return problems


def _count_problems(name: str, stored: int, expected: int, noun: str) -> list:
    return [] if stored == expected else [f"{name}: {stored} {noun} stored, {expected} expected"]


def _value_problems(record_id: str, fields, expected: dict, stored: dict, eq) -> list:
    problems = []
    for f in fields:
        want, got = expected.get(f.name), stored.get(f.name)
        if (want is not None or got is not None) and not eq(want, got):
            problems.append(f"{record_id}.{f.name}: expected {_short(want)}, stored {_short(got)}")
    return problems


def _short(v, n: int = 200) -> str:
    """A value's repr, cut for the problem list (a grown value is megabytes)."""
    r = repr(v)
    return r if len(r) <= n else f"{r[:n]}... ({len(r)} chars)"
