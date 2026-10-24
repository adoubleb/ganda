"""The state check: after the data models' setup plans have run (and after every write),
read each database back and compare the data models' stored states with each other --
every node with its values, every relationship with its endpoints and attributes. A
materializer bug, a write form that silently drops a field, or an engine write-path
defect shows up here as a `StateDiff` before any query can be misled by it.

The check is differential, like the oracle: no data model is compared with the
`Instance`. For every record (and every field of it) the data models' readings are
grouped by equality under the engine's semantics; the largest group is the consensus
and every data model outside it gets a problem. A tie (two data models, one reading
each) has no consensus, and every data model in it gets the problem. So a diff on ONE
data model with the others in agreement is a setup asymmetry (an engine finding on
the write path); a fault every data model shares is not seen here.

The Instance only says WHAT to read: the classes and relationship types, and at scale
(`sample > 0`) which records -- each class is then checked by its record COUNT and a
sample of records read by id (the heaviest rows, the ones the large-row axis grew, and
`sample` random ones), each relationship type by its count and the relationships
leaving a sample of sources (the biggest hubs and `sample` random ones), so a lost or
duplicated record still shows in the count and a damaged value in the sample. The
sample is drawn once from `rng` (the campaign seeds it per instance) and read on every
data model.
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field

from ganda.core.data.model import Instance

# how many of the heaviest records / biggest sources a sample always holds
HEAVIEST = 8

# a record a data model does not hold; no consensus (a tie)
_ABSENT = object()
_TIE = object()


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


def compare_states(sessions: dict, instance: Instance, tables: "set | None" = None, edge_types: "set | None" = None, sample: int = 0, rng: "random.Random | None" = None) -> dict:
    """{data model: StateDiff} over the data models of `sessions`: the whole state, or
    with `tables` / `edge_types` only those node classes and relationship types (the
    write phase's check after one write); with `sample` the counts and a sample (see
    the module). Fewer than two data models have nothing to compare: every diff is ok."""
    schema = instance.schema
    rng = rng or random.Random(0)
    diffs = {data_model: StateDiff(data_model) for data_model in sessions}
    if len(sessions) < 2:
        return diffs
    same = _value_equality(schema.engine.semantics.eq)
    for nt in schema.node_types:
        if tables is None or nt.name in tables:
            _node_problems(sessions, diffs, instance, nt, sample, rng, same)
    for et in schema.edge_types:
        if edge_types is None or et.name in edge_types:
            _edge_problems(sessions, diffs, instance, et, sample, rng, same)
    return diffs


def _value_equality(eq):
    """Field equality under the engine's semantics, a null equal to a null only."""

    def same(a, b) -> bool:
        if a is None or b is None:
            return a is None and b is None
        return eq(a, b)

    return same


def _equal(a, b) -> bool:
    return a == b


def _consensus(observed: dict, same) -> "tuple[list, object]":
    """(the data models off the consensus, the consensus reading) of {data model:
    reading}. The largest group of equal readings is the consensus; on a tie there is
    none (`_TIE`) and every data model is off it. `_ABSENT` equals only itself."""

    def matches(a, b) -> bool:
        if a is _ABSENT or b is _ABSENT:
            return a is b
        return same(a, b)

    groups: list = []  # [reading, [data models]]
    for data_model, v in observed.items():
        group = next((g for g in groups if matches(g[0], v)), None)
        if group is None:
            groups.append([v, [data_model]])
        else:
            group[1].append(data_model)
    if len(groups) == 1:
        return [], groups[0][0]
    groups.sort(key=lambda g: -len(g[1]))
    if len(groups[0][1]) == len(groups[1][1]):
        return list(observed), _TIE
    return [s for g in groups[1:] for s in g[1]], groups[0][0]


def _report(diffs: dict, observed: dict, same, what: str, show=None) -> None:
    """One problem per data model off the consensus of `observed` ({data model: reading})."""
    show = show or _short
    off, agreed = _consensus(observed, same)
    if not off:
        return
    if agreed is _TIE:
        readings = ", ".join(f"{s}: {'absent' if v is _ABSENT else show(v)}" for s, v in observed.items())
        for s in off:
            diffs[s].problems.append(f"{what}: the data models disagree ({readings})")
        return
    rest = ", ".join(sorted(s for s in observed if s not in off))
    for s in off:
        mine = observed[s]
        if mine is _ABSENT:
            diffs[s].problems.append(f"{what}: missing (held by {rest})")
        elif agreed is _ABSENT:
            diffs[s].problems.append(f"{what}: unexpected (absent from {rest})")
        else:
            diffs[s].problems.append(f"{what}: {show(mine)}, where {rest} store {show(agreed)}")


def _compare_records(diffs: dict, stored: dict, what: str, compare_one) -> None:
    """For every record id any data model holds ({data model: {id: record}}): whether
    each data model holds it, then `compare_one(id, holders)` over the holders."""
    for rid in sorted({rid for records in stored.values() for rid in records}, key=repr):
        held = {s: (rid if rid in records else _ABSENT) for s, records in stored.items()}
        _report(diffs, held, _equal, f"{what} {rid}")
        holders = [s for s, v in held.items() if v is not _ABSENT]
        if len(holders) > 1:
            compare_one(rid, holders)


def _node_problems(sessions: dict, diffs: dict, instance: Instance, nt, sample: int, rng: random.Random, same) -> None:
    if sample > 0:
        _report(diffs, {s: session.count_nodes(nt.name) for s, session in sessions.items()}, _equal, f"{nt.name}: record count", show=str)
        ids = sample_ids([(n.record_id, _weight(n.values)) for n in instance.nodes_of(nt.name)], sample, rng)
        wanted = set(ids)
        stored = {s: {rid: r for rid, r in session.read_nodes(nt.name, ids=ids).items() if rid in wanted} for s, session in sessions.items()}
    else:
        stored = {s: session.read_nodes(nt.name) for s, session in sessions.items()}
    fields = instance.schema.query_fields(nt.name)

    def compare_one(rid, holders: list) -> None:
        for f in fields:
            _report(diffs, {s: stored[s][rid].get(f.name) for s in holders}, same, f"{rid}.{f.name}")

    _compare_records(diffs, stored, f"{nt.name}: node", compare_one)


def _edge_problems(sessions: dict, diffs: dict, instance: Instance, et, sample: int, rng: random.Random, same) -> None:
    if sample > 0:
        _report(diffs, {s: session.count_edges(s, instance, et) for s, session in sessions.items()}, _equal, f"{et.name}: relationship count", show=str)
        fan_out = Counter(e.from_record for e in instance.edges_of(et.name))
        sources = sample_ids(sorted(fan_out.items()), sample, rng)
        chosen = set(sources)
        rows = {s: [row for row in session.read_edges(s, instance, et, sources=sources) if row[1] in chosen] for s, session in sessions.items()}
    else:
        rows = {s: session.read_edges(s, instance, et) for s, session in sessions.items()}

    stored: dict = {}
    for s, data_model_rows in rows.items():
        edges = stored[s] = {}
        for eid, src, dst, attrs in data_model_rows:
            if eid in edges:
                diffs[s].problems.append(f"{et.name}: relationship {eid} stored twice")
            edges[eid] = (src, dst, attrs)

    def compare_one(eid, holders: list) -> None:
        _report(diffs, {s: stored[s][eid][:2] for s in holders}, _equal, f"{eid}: endpoints", show=lambda v: f"{v[0]}->{v[1]}")
        for f in et.fields:
            _report(diffs, {s: stored[s][eid][2].get(f.name) for s in holders}, same, f"{eid}.{f.name}")

    _compare_records(diffs, stored, f"{et.name}: relationship", compare_one)


def _short(v, n: int = 200) -> str:
    """A value's repr, cut for the problem list (a grown value is megabytes)."""
    r = repr(v)
    return r if len(r) <= n else f"{r[:n]}... ({len(r)} chars)"
