"""The write phase: after every read case of an instance, `write_cases` writes run one
after another on every data model, each followed by checks, and the Instance the
reference uses evolves with them.

One step:
1. draw a write and each data model's concrete syntax (`generators.write`);
2. run it on every data model (the provider's `write_syntax.execute`) and compare the
   answers -- a count, or the returned records -- pair by pair and against the
   reference over the Instance BEFORE the write (`ir_write`, an ordinary `CaseResult`);
3. apply the write to the Instance and read the touched classes back on every data
   model (`state_check.verify_state`): a data model whose stored state differs is a
   write-path finding;
4. read the written field back through the select generator (`READBACKS` questions
   over the written class, their filter over the written field -- a stale index entry
   is an index-vs-scan disagreement), or the relationship type through its filtered
   `ids`.

A step that errors on some data model, or leaves some data model's state off the
reference, ends the phase: the databases no longer hold one state.
"""
from __future__ import annotations

import dataclasses
import random
from dataclasses import dataclass, field

from ganda.core.model import Instance
from ganda.core.oracle import build_result, run_case
from ganda.core.provider import EngineCrash, EngineError
from ganda.core.case import QueryCase
from ganda.core.ast.write import apply, comparison, coverage_signature, shape_signature, touched
from ganda.core.generators.select import build_case, sample_select
from ganda.core.generators.write import sample_write
from ganda.core.state_check import StateDiff, verify_state

READBACKS = 2


@dataclass
class WritePhase:
    results: list = field(default_factory=list)
    instance: "Instance | None" = None  # the Instance after the last applied write
    diffs: dict = field(default_factory=dict)  # data model -> StateDiff of the check that failed
    failed_step: "int | None" = None
    stopped: str = ""  # why the phase ended early ("" when it ran every step)
    steps: int = 0


def _readbacks(q, rng: random.Random, instance: Instance, step: int) -> list:
    out = []
    for _ in range(READBACKS):
        # an edge write is read back through the select IR's edge-type target (the
        # E class, the junction, the doc data model's link documents)
        if q.edge:
            sq = sample_select(rng, instance.schema, instance, table=q.edge_type, focus=q.field, edge=True)
        else:
            sq = sample_select(rng, instance.schema, instance, table=q.table, focus=q.field)
        if sq is not None:
            out.append(build_case(sq, rng, instance.schema, instance))
    return [dataclasses.replace(c, shape=f"after-write {c.shape}", params={**c.params, "after_write": step}) for c in out]


def build_write_case(q, rewrites, syntaxes: dict, step: int, before: Instance, after: Instance, keep_known_bugs: bool) -> "tuple[QueryCase, dict]":
    """(the compared case, data model -> Statements). The doc data model of an edge write answers
    no relationship count: the case is `partial` there and only its state is checked."""
    engine = before.schema.engine
    syn = engine.write_syntax
    statements = {data_model: syn.render(q, syntaxes[data_model][0], rewrites, data_model, before, after) for data_model in engine.data_models}
    queries = {data_model: st.display() for data_model, st in statements.items() if st.main is not None}
    bind: dict = {}
    for st in statements.values():
        bind.update(syn.shared_binds(st))
    params = {
        "shape": shape_signature(q, before.schema),
        "coverage": coverage_signature(q),
        "write": q.to_params(),
        "step": step,
        "keep_known_bugs": keep_known_bugs,
        "concrete_syntax": {data_model: sp.tag for data_model, (sp, _k) in syntaxes.items()},
        "statements": {data_model: st.display() for data_model, st in statements.items()},
        "reference": q.where is None or q.where.reference,
    }
    if set(queries) != set(engine.data_models):
        params["partial"] = True
    params.update(syn.rewrite_params(rewrites))
    known_bugs = {data_model: fid for data_model, (_sp, fid) in syntaxes.items() if fid and data_model in queries}
    case = QueryCase("ir_write", q.edge_type, comparison(q), queries, bind, params, shape=params["shape"], known_bugs=known_bugs, engine=engine)
    return case, statements


def run_write_phase(sessions: dict, instance: Instance, rng: random.Random, steps: int, verify: bool = True, keep_known_bugs: bool = False, stop_after: "int | None" = None, reference: bool = True) -> WritePhase:
    """`verify` reads the touched classes back after every write; `semantics` False
    compares the data models only (the mutation benchmark's `--no-semantics`)."""
    phase = WritePhase(instance=instance)
    inst = instance
    schema = instance.schema
    engine = schema.engine
    data_models = engine.data_models
    for step in range(steps):
        if stop_after is not None and step > stop_after:
            break
        drawn = sample_write(rng, schema, inst, step, keep_known_bugs)
        if drawn is None:
            continue
        q, rewrites, syntaxes = drawn
        after = apply(q, inst)
        case, statements = build_write_case(q, rewrites, syntaxes, step, inst, after, keep_known_bugs)
        rows: dict = {}
        errors: dict = {}
        internal: dict = {}
        state_errors: dict = {}
        for data_model in data_models:
            try:
                answer = engine.write_syntax.execute(sessions[data_model], statements[data_model])
                if data_model in case.queries:
                    rows[data_model], errors[data_model], internal[data_model] = answer, "", False
            except (EngineError, EngineCrash) as e:
                if data_model in case.queries:
                    rows[data_model], errors[data_model], internal[data_model] = [], str(e), getattr(e, "internal", False)
                else:
                    state_errors[data_model] = str(e)
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
            diffs = {data_model: verify_state(sessions[data_model], data_model, inst, tables=tables, edge_types=edge_types) for data_model in data_models}
            if any(not d.ok for d in diffs.values()):
                phase.diffs = diffs
                phase.failed_step = step
                phase.stopped = f"step {step}: state off the reference on {sorted(s for s, d in diffs.items() if not d.ok)}"
                return phase
        for c in _readbacks(q, rng, inst, step):
            phase.results.append(run_case(c, sessions, inst if reference else None, keep_known_bugs=keep_known_bugs))
    return phase
