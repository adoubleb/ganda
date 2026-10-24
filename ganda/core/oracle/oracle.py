"""The metamorphic oracle: run a `QueryCase` against every data model and decide whether the
answers are equivalent.

Every unordered *pair* of data models (`doc`, `graph`, `rel`, plus every variant such
as `graph:match` or `rel:flat`) is compared independently, and each `Mismatch` records
which pair saw it (`CaseResult.failing_pairs`) -- the per-pair attribution that says
whether a divergence is cross-model, or a same-database disagreement between two
syntaxes over one storage layout.

Normalization strips only *representation* artifacts:
- a concrete syntax that answers one row per PATH (MATCH, an edge/junction scan, UNWIND) is
  folded into one entry per start id; a start record with no path is then absent,
  which reads as the empty multiset;
- inside a multiset, nested lists are flattened (a multi-hop `e1.target.e1.target.id`
  is a list of lists where `out('e1').out('e1').id` is flat) and nulls are dropped
  (the absent-list data model of a record with no links);
- element order inside a multiset/set.

Numbers are NOT collapsed across types: every column has one declared type on every
data model, so `5` against `5.0` for the same expression is a divergence.

An error on some data models but not all is a finding (`error_asymmetry`); an error on every
data model is consistent rejection. An *internal* error (a raw exception from inside the engine) is a
finding of its own class whether or not the data models agree on it. A variant that TIMES
OUT (`EngineError.timeout`) is recorded in `CaseResult.timeouts` (the campaign counts
them) and, by default, read as the rejection it is on a small instance -- a plan that
never finishes is a finding (O37). With `timeouts_are_errors=False` (the large scale,
where a data model may simply be slower than the question allows) it gave no answer at
all: it is left out of every pair.

The oracle is purely differential: no answer is predicted from the `Instance`, so a
fault every variant shares is not seen.
"""
from __future__ import annotations

import itertools
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ganda.core.data.model import hashable_value
from ganda.core.oracle.case import Comparison, QueryCase, data_model_of
from ganda.core.engine.provider import DEFAULT_SEMANTICS, EngineCrash, EngineError

_BARE = (Comparison.SINGLE_VALUE, Comparison.NESTED_VALUE)


def _flatten(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        out: list = []
        for v in value:
            out.extend(_flatten(v))
        return out
    return [value]


def _key_of(case) -> Any:
    """The value order a case's answers are normalized under: its engine's."""
    engine = getattr(case, "engine", None)
    return (engine.semantics if engine is not None else DEFAULT_SEMANTICS).key


def normalize_multiset(value: Any, key=DEFAULT_SEMANTICS.key) -> list:
    return sorted(_flatten(value), key=key)


def normalize_set(value: Any, key=DEFAULT_SEMANTICS.key) -> list:
    seen: dict = {}
    for v in _flatten(value):
        seen.setdefault(key(v), v)
    return [seen[k] for k in sorted(seen)]


def _canon_row(row: Any) -> Any:
    """A canonical JSON form of a bare row, so two dicts holding the same keys in a
    different order sort together."""
    return json.dumps(row, sort_keys=True, default=repr, ensure_ascii=False)


def normalize_nested(value: Any) -> Any:
    if isinstance(value, list):
        return sorted((normalize_nested(v) for v in value), key=_canon_row)
    return value


def normalize_deep(value: Any) -> Any:
    """Order ignored at EVERY depth, inside a row's values too (a set-valued column has
    no order): a case asks for it with `params["unordered_values"]`."""
    if isinstance(value, list):
        return sorted((normalize_deep(v) for v in value), key=_canon_row)
    if isinstance(value, dict):
        return {k: normalize_deep(v) for k, v in value.items()}
    return value


def normalize_bare(case, value: Any) -> Any:
    """A bare answer's canonical form, under the case's engine's value order and its
    wire form (`Semantics.canonical`)."""
    engine = getattr(case, "engine", None)
    value = (engine.semantics if engine is not None else DEFAULT_SEMANTICS).canonical(value)
    if case.comparison is Comparison.NESTED_VALUE and case.params.get("unordered_values"):
        return normalize_deep(value)
    return normalize(case.comparison, value, _key_of(case))


def normalize(comparison: Comparison, value: Any, key=DEFAULT_SEMANTICS.key) -> Any:
    if comparison is Comparison.MULTISET_BY_ID:
        return normalize_multiset(value, key)
    if comparison is Comparison.SET_BY_ID:
        return normalize_set(value, key)
    if comparison is Comparison.NESTED_VALUE:
        return normalize_nested(value)
    return value


def fold_rows(rows: list, comparison: Comparison, result_field: str) -> "tuple[dict, list]":
    """{id: value} from a data model's rows. For multiset/set comparisons several rows with
    one id are concatenated (list values extended, scalars appended); for a scalar
    comparison a repeated id is a `DuplicateIdRows` finding."""
    out: dict = {}
    dups: list = []
    counts: Counter = Counter()
    for row in rows:
        if not isinstance(row, dict) or "id" not in row:
            continue
        rid = hashable_value(row["id"])
        v = row.get(result_field)
        counts[rid] += 1
        if comparison in (Comparison.MULTISET_BY_ID, Comparison.SET_BY_ID):
            bucket = out.setdefault(rid, [])
            if isinstance(v, list):
                bucket.extend(v)
            else:
                bucket.append(v)
        else:
            out[rid] = v
    for rid, c in counts.items():
        if c > 1 and comparison not in (Comparison.MULTISET_BY_ID, Comparison.SET_BY_ID):
            dups.append(DuplicateIdRows(rid, c))
    return out, dups


def is_empty_value(value) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (list, dict, str)):
        return len(value) == 0
    if isinstance(value, (int, float)):
        return value == 0
    return False


@dataclass
class DuplicateIdRows:
    record_id: Any
    count: int
    variant: str = ""


@dataclass
class Mismatch:
    record_id: Any
    left: str
    right: str
    left_raw: Any
    right_raw: Any
    left_normalized: Any
    right_normalized: Any

    @property
    def pair(self) -> str:
        return f"{self.left}/{self.right}"


@dataclass
class CaseResult:
    case: QueryCase
    ok: bool
    rows: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    internal: dict = field(default_factory=dict)
    mismatches: list = field(default_factory=list)
    duplicate_id_rows: list = field(default_factory=list)
    plans: dict = field(default_factory=dict)
    suppressed: dict = field(default_factory=dict)  # data model -> known bug id
    timeouts: dict = field(default_factory=dict)  # variant -> the timeout's message (no answer: not compared)

    @property
    def variants(self) -> list:
        return [s for s in self.case.variants if s in self.rows]

    @property
    def error_asymmetry(self) -> bool:
        flags = [bool(self.errors.get(s)) for s in self.variants]
        return any(flags) and not all(flags)

    @property
    def symmetric_error(self) -> bool:
        flags = [bool(self.errors.get(s)) for s in self.variants]
        return bool(flags) and all(flags)

    @property
    def internal_error(self) -> bool:
        return any(self.internal.values())

    @property
    def failing_pairs(self) -> list:
        seen = {m.pair for m in self.mismatches}
        return [f"{a}/{b}" for a, b in itertools.combinations(self.variants, 2) if f"{a}/{b}" in seen]

    @property
    def finding_class(self) -> "str | None":
        if self.internal_error:
            return "internal_error"
        if self.error_asymmetry:
            return "error_asymmetry"
        if self.duplicate_id_rows:
            return "duplicate_id_rows"
        if self.mismatches:
            return "mismatch"
        return None

    def compared_values(self) -> list:
        if not self.variants:
            return []
        rows = self.rows.get(self.variants[0], [])
        if self.case.comparison in _BARE:
            return list(rows)
        return [row.get(self.case.result_field) for row in rows if isinstance(row, dict)]

    @property
    def asserts_anything(self) -> bool:
        """Whether the case compared a non-empty answer: at least two variants answered
        (a lone variant has nothing to be compared with) and no variant erred."""
        if any(self.errors.values()) or len(self.variants) < 2:
            return False
        return any(not is_empty_value(v) for v in self.compared_values())


def run_case(case: QueryCase, sessions: dict, explain: bool = False, keep_known_bugs: bool = False, after_variant=None, timeouts_are_errors: bool = True) -> CaseResult:
    """`after_variant(case, data_model)` runs once a data model (and its plan) has been asked --
    a coverage collector closes a window there. `timeouts_are_errors` False leaves a
    timed-out variant out of the comparison (see the module)."""
    rows: dict = {}
    errors: dict = {}
    internal: dict = {}
    plans: dict = {}
    suppressed: dict = {}
    timeouts: dict = {}
    for variant in case.variants:
        if data_model_of(variant) not in sessions:
            continue
        if variant in case.known_bugs and not keep_known_bugs:
            suppressed[variant] = case.known_bugs[variant]
            continue
        session = sessions[data_model_of(variant)]
        text = case.query_for(variant)
        try:
            rows[variant] = session.execute(text, case.bind_vars, case.options_for(variant))
            errors[variant] = ""
            internal[variant] = False
        except EngineError as e:
            if e.timeout:
                timeouts[variant] = str(e)
            if not e.timeout or timeouts_are_errors:
                rows[variant] = []
                errors[variant] = str(e)
                internal[variant] = e.internal
        if explain and variant in rows:

            try:
                plan = session.explain(text, case.bind_vars)
            except (EngineError, EngineCrash):
                plan = ["<explain failed>"]
            if plan is not None:
                plans[variant] = plan
        if after_variant is not None:
            after_variant(case, variant)
    result = build_result(case, rows, errors)
    result.internal = internal
    result.plans = plans
    result.suppressed = suppressed
    result.timeouts = timeouts
    if result.internal_error:
        result.ok = False
    return result



def run_cases(cases: list, sessions: dict, after_case=None, **kw) -> list:
    out = []
    for c in cases:
        out.append(run_case(c, sessions, **kw))
        if after_case is not None:
            after_case(c)
    return out


def build_result(case: QueryCase, rows: dict, errors: dict) -> CaseResult:
    """The comparison. An error on any variant ends it (`ok` only when every variant
    erred alike) -- unless the provider says `compare_despite_errors`: the variants that
    answered are then compared among themselves, so an error asymmetry does not hide
    a wrong answer among the rest (the result carries both)."""
    failed = any(errors.values())
    if failed and not getattr(case.engine, "compare_despite_errors", False):
        return CaseResult(case=case, ok=all(errors.values()), rows=rows, errors=errors)
    asymmetry = failed and not all(errors.values())

    variants = [s for s in case.variants if s in rows and not errors.get(s)]
    pairs = list(itertools.combinations(variants, 2))

    if case.comparison in _BARE:
        normalized = {s: normalize_bare(case, rows[s]) for s in variants}
        mismatches = [
            Mismatch("<single>", left, right, rows[left], rows[right], normalized[left], normalized[right])
            for left, right in pairs
            if normalized[left] != normalized[right]
        ]
        return CaseResult(case=case, ok=not mismatches and not asymmetry, rows=rows, errors=errors, mismatches=mismatches)

    key = _key_of(case)
    maps: dict = {}
    dups: list = []
    for s in variants:
        maps[s], d = fold_rows(rows[s], case.comparison, case.result_field)
        for x in d:
            x.variant = s
        dups.extend(d)
    mismatches = []
    for left, right in pairs:
        for rid in sorted(set(maps[left]) | set(maps[right]), key=repr):
            lv, rv = maps[left].get(rid), maps[right].get(rid)
            ln, rn = normalize(case.comparison, lv, key), normalize(case.comparison, rv, key)
            if ln != rn:
                mismatches.append(Mismatch(rid, left, right, lv, rv, ln, rn))
    return CaseResult(
        case=case,
        ok=not mismatches and not dups and not asymmetry,
        rows=rows,
        errors=errors,
        mismatches=mismatches,
        duplicate_id_rows=dups,
    )
