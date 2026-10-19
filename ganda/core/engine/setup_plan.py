"""What every provider's setup plan shares.

A setup plan is the list of steps that materializes an `Instance` in one data model.
The STEPS are the provider's (a statement, a script, an API call): a step class
registers itself with `@step("<op>")`, serializes with `to_dict()` and is rebuilt by
`plan_from_dict`, so a bug bundle carries the exact plan that ran.

A step that must name a record the engine identifies by its own id (a record id the engine assigns)
carries a `Ref(logical id)` instead; the session resolves it from the ids it captured
while applying earlier steps (`resolve_refs`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ganda.core.data.model import EdgeDelete, EdgeUpdate, Instance, NodeDelete


@dataclass(frozen=True)
class Ref:
    """A placeholder for the engine's own id of the record whose logical id is `id`."""

    id: str


def encode_refs(value: Any) -> Any:
    """JSON form: a `Ref` becomes `{"$ref": id}`."""
    if isinstance(value, Ref):
        return {"$ref": value.id}
    if isinstance(value, list):
        return [encode_refs(v) for v in value]
    if isinstance(value, dict):
        return {k: encode_refs(v) for k, v in value.items()}
    return value


def decode_refs(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return Ref(value["$ref"])
        return {k: decode_refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_refs(v) for v in value]
    return value


def resolve_refs(value: Any, rids: dict) -> Any:
    """Substitute every `Ref` with the engine id the session captured."""
    if isinstance(value, Ref):
        try:
            return rids[value.id]
        except KeyError:
            raise KeyError(f"no engine id captured for {value.id!r}; the plan references a record before inserting it") from None
    if isinstance(value, list):
        return [resolve_refs(v, rids) for v in value]
    if isinstance(value, dict):
        return {k: resolve_refs(v, rids) for k, v in value.items()}
    return value


# --- the step registry --------------------------------------------------------------------

_STEP_TYPES: dict = {}


def step(op: str):
    """Register a step class under `op`: it has `to_dict()` (carrying `"op": op`) and a
    `from_dict(d)` classmethod. The registry is shared by every provider, so an op name
    another class already holds is refused (a provider prefixes its own: `hana_sql`)."""

    def register(cls):
        held = _STEP_TYPES.get(op)
        if held is not None and (held.__module__, held.__qualname__) != (cls.__module__, cls.__qualname__):
            raise ValueError(f"setup step {op!r} is already {held.__module__}.{held.__qualname__}")
        _STEP_TYPES[op] = cls
        return cls

    return register


def plan_to_dict(plan: list) -> list:
    return [s.to_dict() for s in plan]


def step_from_dict(d: dict):
    try:
        cls = _STEP_TYPES[d["op"]]
    except KeyError:
        raise ValueError(f"unknown setup step {d['op']!r} (is the provider imported?)") from None
    return cls.from_dict(d)


def plan_from_dict(items: list) -> list:
    return [step_from_dict(d) for d in items]


# --- the write history of the relationships -----------------------------------------------


def initial_edge_values(instance: Instance) -> dict:
    """edge id -> the attribute values the relationship was FIRST written with."""
    out = {e.record_id: e.values for e in initial_edges(instance)}
    for op in instance.churn:
        if isinstance(op, EdgeUpdate):
            out[op.edge.record_id] = op.initial_values
    return out


def initial_edges(instance: Instance) -> list:
    """Every edge that was ever written, including ones churn later removes."""
    gone = churned_away_edges(instance)
    both = list(instance.edges) + gone
    return sorted(both, key=lambda e: (e.type_name, e.local_id))


def churned_away_edges(instance: Instance) -> list:
    out = [op.edge for op in instance.churn if isinstance(op, EdgeDelete)]
    for op in instance.churn:
        if isinstance(op, NodeDelete):
            out.extend(op.incident_edges)
    return out
