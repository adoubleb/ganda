"""The write phase: after every read case of an instance, `write_cases` writes run
one after another on the three databases, each followed by checks, and the Instance
the reference uses evolves with them.

One step:
1. draw a write and each side's spelling (`queries.write_generator`);
2. run it on every side -- a cascade prelude and the compared statement, or all of
   it in `BEGIN; ...; COMMIT;` -- and compare the answers (a count, or the returned
   records) pair by pair and against the reference over the Instance BEFORE the
   write (`ir_write`, an ordinary `CaseResult`);
3. apply the write to the Instance and read the touched classes back on every side
   (`state.verify_state`): a side whose stored state differs is a write-path finding;
4. read the written field back through the select IR (`READBACKS` questions over the
   written class, their filter over the written field: plain, `scan`, `nest` ... --
   a stale index entry is an index-vs-scan disagreement), or the relationship type
   through its filtered `ids`.

A step that errors on some side, or leaves some side's state off the reference,
ends the phase: the databases no longer hold one state. Every UPDATE carries a
server-side TIMEOUT (a runaway update would otherwise hold the worker: O37).
"""
from __future__ import annotations

import dataclasses
import random
import re
from dataclasses import dataclass, field

from ganda.orientdb.harness import OrientCrash, OrientError
from ganda.orientdb.model import Instance
from ganda.orientdb.oracle import build_result, run_case, unwrap_script
from ganda.orientdb.queries.base import SIDES, QueryCase
from ganda.orientdb.queries.write_ir import apply, comparison, coverage_signature, shape_signature, touched
from ganda.orientdb.queries.write_generator import sample_write
from ganda.orientdb.queries.write_spellings import render
from ganda.orientdb.state import StateDiff, verify_state

READBACKS = 2


@dataclass
class WritePhase:
    results: list = field(default_factory=list)
    instance: "Instance | None" = None  # the Instance after the last applied write
    diffs: dict = field(default_factory=dict)  # side -> StateDiff of the check that failed
    failed_step: "int | None" = None
    stopped: str = ""  # why the phase ended early ("" when it ran every step)
    steps: int = 0


def _execute(session, st) -> list:
    from ganda.orientdb.materialize.common import resolve_refs

    if st.door == "tx":
        if not st.prelude and not st.main:
            return []
        rows = session.script(st.script(), resolve_refs(st.params, session.rids))
        return unwrap_script(rows) if st.main else []
    for text, params in st.prelude:
        session.sql(text, resolve_refs(params, session.rids), timeout_ms=0)
    if st.main:
        return session.sql(st.main[0], resolve_refs(st.main[1], session.rids), timeout_ms=0)
    return []


def _readbacks(q, rng: random.Random, instance: Instance, step: int) -> list:
    from ganda.orientdb.queries.select_generator import build_case, sample_select

    out = []
    for _ in range(READBACKS):
        # an edge write is read back through the select IR's edge-type target (the
        # E class, the junction, the doc side's link documents)
        if q.edge:
            sq = sample_select(rng, instance.schema, instance, table=q.edge_type, focus=q.field, edge=True)
        else:
            sq = sample_select(rng, instance.schema, instance, table=q.table, focus=q.field)
        if sq is not None:
            out.append(build_case(sq, rng, instance.schema, instance))
    return [dataclasses.replace(c, shape=f"after-write {c.shape}", params={**c.params, "after_write": step}) for c in out]


def build_write_case(q, twins, spellings: dict, step: int, before: Instance, after: Instance, keep_known: bool) -> "tuple[QueryCase, dict]":
    """(the compared case, side -> Statements). The doc side of an edge write answers
    no relationship count: the case is `partial` there and only its state is checked."""
    statements = {side: render(q, spellings[side][0], twins, side, before, after) for side in SIDES}
    queries = {side: st.display() for side, st in statements.items() if st.main is not None}
    bind: dict = {}
    for st in statements.values():
        # the doc side's per-source lists stay out of the shared bind map
        bind.update({k: v for k, v in st.params.items() if not re.fullmatch(r"w[li]\d+", k)})
    params = {
        "shape": shape_signature(q, before.schema),
        "coverage": coverage_signature(q),
        "write": q.to_params(),
        "step": step,
        "keep_known": keep_known,
        "spellings": {side: sp.tag for side, (sp, _k) in spellings.items()},
        "statements": {side: st.display() for side, st in statements.items()},
        "reference": q.where is None or q.where.reference,
    }
    if set(queries) != set(SIDES):
        params["partial"] = True
    for name in ("rw", "rev", "ar"):
        twin = getattr(twins, name)
        if twin is not None:
            params[name] = twin.to_dict()
    known = {side: fid for side, (_sp, fid) in spellings.items() if fid and side in queries}
    case = QueryCase("ir_write", q.edge_type, comparison(q), queries, bind, params, shape=params["shape"], known=known)
    return case, statements


def run_write_phase(sessions: dict, instance: Instance, rng: random.Random, steps: int, verify: bool = True, keep_known: bool = False, stop_after: "int | None" = None, reference: bool = True) -> WritePhase:
    """`verify` reads the touched classes back after every write; `reference` False
    compares the sides only (the mutation benchmark's `--no-reference`)."""
    phase = WritePhase(instance=instance)
    inst = instance
    schema = instance.schema
    for step in range(steps):
        if stop_after is not None and step > stop_after:
            break
        drawn = sample_write(rng, schema, inst, step, keep_known)
        if drawn is None:
            continue
        q, twins, spellings = drawn
        after = apply(q, inst)
        case, statements = build_write_case(q, twins, spellings, step, inst, after, keep_known)
        rows: dict = {}
        errors: dict = {}
        internal: dict = {}
        state_errors: dict = {}
        for side in SIDES:
            try:
                answer = _execute(sessions[side], statements[side])
                if side in case.queries:
                    rows[side], errors[side], internal[side] = answer, "", False
            except (OrientError, OrientCrash) as e:
                if side in case.queries:
                    rows[side], errors[side], internal[side] = [], str(e), getattr(e, "internal", False)
                else:
                    state_errors[side] = str(e)
        result = build_result(case, rows, errors, inst if reference else None)
        result.internal = internal
        if result.internal_error:
            result.ok = False
        phase.results.append(result)
        phase.steps = step + 1
        if any(errors.values()) or state_errors:
            phase.stopped = f"step {step}: the write failed on {sorted([s for s, e in errors.items() if e] + list(state_errors))}"
            phase.failed_step = step
            phase.diffs = {s: StateDiff(s, [f"write failed: {e[:300]}"]) for s, e in state_errors.items()}
            return phase
        inst = after
        phase.instance = inst
        if verify:
            tables, edge_types = touched(q, inst)
            diffs = {side: verify_state(sessions[side], side, inst, tables=tables, edge_types=edge_types) for side in SIDES}
            if any(not d.ok for d in diffs.values()):
                phase.diffs = diffs
                phase.failed_step = step
                phase.stopped = f"step {step}: state off the reference on {sorted(s for s, d in diffs.items() if not d.ok)}"
                return phase
        for c in _readbacks(q, rng, inst, step):
            phase.results.append(run_case(c, sessions, inst if reference else None, keep_known=keep_known))
    return phase
