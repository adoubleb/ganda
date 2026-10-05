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
data model is consistent rejection. An *internal* error (a raw Java exception, HTTP 501) is a
finding of its own class whether or not the data models agree on it. The Python reference
leg is compared against each data model under the same normalization.
"""
from __future__ import annotations

import itertools
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ganda.orientdb.connection import strip_meta, OrientCrash, OrientError, OrientSession
from ganda.core.model import Instance, hashable_value
from ganda.core.case import DATA_MODELS, Comparison, QueryCase, data_model_of
from ganda.orientdb.semantics import orient_key

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


def normalize_multiset(value: Any) -> list:
    return sorted(_flatten(value), key=orient_key)


def normalize_set(value: Any) -> list:
    seen: dict = {}
    for v in _flatten(value):
        seen.setdefault(orient_key(v), v)
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
    """Order ignored at EVERY depth, inside a row's values too: a card's NESTED_VALUE
    (a set-valued column -- `asSet()`, `set()` -- has no order; round 2's card 0185)."""
    if isinstance(value, list):
        return sorted((normalize_deep(v) for v in value), key=_canon_row)
    if isinstance(value, dict):
        return {k: normalize_deep(v) for k, v in value.items()}
    return value


def normalize_bare(case, value: Any) -> Any:
    """A bare answer's canonical form: the cards compare NESTED_VALUE deep."""
    if case.comparison is Comparison.NESTED_VALUE and case.params.get("card"):
        return normalize_deep(value)
    return normalize(case.comparison, value)


def normalize(comparison: Comparison, value: Any) -> Any:
    if comparison is Comparison.MULTISET_BY_ID:
        return normalize_multiset(value)
    if comparison is Comparison.SET_BY_ID:
        return normalize_set(value)
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
class ReferenceMismatch:
    variant: str
    record_id: Any
    got: Any
    expected: Any


@dataclass
class CaseResult:
    case: QueryCase
    ok: bool
    rows: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    internal: dict = field(default_factory=dict)
    mismatches: list = field(default_factory=list)
    reference_mismatches: list = field(default_factory=list)
    duplicate_id_rows: list = field(default_factory=list)
    plans: dict = field(default_factory=dict)
    suppressed: dict = field(default_factory=dict)  # data model -> known bug id

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
        if self.reference_mismatches:
            return "reference_mismatch"
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
        if any(self.errors.values()):
            return False
        return any(not is_empty_value(v) for v in self.compared_values())


def is_script(text: str) -> bool:
    return text.lstrip().upper().startswith("BEGIN;")


def unwrap_script(result: list) -> list:
    """A script's `RETURN $r` comes back as `[{"value": [rows...]}]` (probed); the
    rows are the answer."""
    if len(result) == 1 and isinstance(result[0], dict) and set(result[0]) == {"value"}:
        v = result[0]["value"]
        return [strip_meta(r) if isinstance(r, dict) else r for r in v] if isinstance(v, list) else [v]
    return result


def run_case(case: QueryCase, sessions: dict, instance: "Instance | None" = None, explain: bool = False, keep_known_bugs: bool = False, after_variant=None) -> CaseResult:
    """`after_variant(case, data_model)` runs once a data model (and its plan) has been asked --
    `ganda.orientdb.coverage` closes a coverage window there."""
    rows: dict = {}
    errors: dict = {}
    internal: dict = {}
    plans: dict = {}
    suppressed: dict = {}
    for variant in case.variants:
        if data_model_of(variant) not in sessions:
            continue
        if variant in case.known_bugs and not keep_known_bugs:
            suppressed[variant] = case.known_bugs[variant]
            continue
        session: OrientSession = sessions[data_model_of(variant)]
        text = case.query_for(variant)
        script = is_script(text)
        try:
            rows[variant] = unwrap_script(session.script(text, case.bind_vars)) if script else session.sql(text, case.bind_vars)
            errors[variant] = ""
            internal[variant] = False
        except OrientError as e:
            rows[variant] = []
            errors[variant] = str(e)
            internal[variant] = e.internal
        if explain and not script:
            try:
                plans[variant] = session.explain(text, case.bind_vars)
            except (OrientError, OrientCrash):
                plans[variant] = ["<explain failed>"]
        if after_variant is not None:
            after_variant(case, variant)
    result = build_result(case, rows, errors, instance)
    result.internal = internal
    result.plans = plans
    result.suppressed = suppressed
    if result.internal_error:
        result.ok = False
    return result


def run_cases(cases: list, sessions: dict, instance: "Instance | None" = None, after_case=None, **kw) -> list:
    out = []
    for c in cases:
        out.append(run_case(c, sessions, instance, **kw))
        if after_case is not None:
            after_case(c)
    return out


def _expected(case: QueryCase, instance: Instance):
    from ganda.core.generators.registry import reference_for

    return reference_for(case, instance)


def _reference_check(case: QueryCase, instance: "Instance | None", data_model_values: dict) -> list:
    if instance is None or case.params.get("reference") is False:
        return []
    has_ref, ref = _expected(case, instance)
    if not has_ref:
        return []
    out = []
    if case.comparison in _BARE:
        ref_n = normalize(case.comparison, ref)
        for variant, value in data_model_values.items():
            if normalize(case.comparison, value) != ref_n:
                out.append(ReferenceMismatch(variant=variant, record_id="<single>", got=value, expected=ref))
        return out
    ref_map = {hashable_value(k): v for k, v in ref.items()}
    for variant, data_model_map in data_model_values.items():
        for rid in sorted(set(data_model_map) | set(ref_map), key=repr):
            got, exp = data_model_map.get(rid), ref_map.get(rid)
            if normalize(case.comparison, got) != normalize(case.comparison, exp):
                out.append(ReferenceMismatch(variant=variant, record_id=rid, got=got, expected=exp))
    return out


def build_result(case: QueryCase, rows: dict, errors: dict, instance: "Instance | None") -> CaseResult:
    if any(errors.values()):
        return CaseResult(case=case, ok=all(errors.values()), rows=rows, errors=errors)

    variants = [s for s in case.variants if s in rows]
    pairs = list(itertools.combinations(variants, 2))
    base_data_models = [s for s in variants if ":" not in s]

    if case.comparison in _BARE:
        normalized = {s: normalize_bare(case, rows[s]) for s in variants}
        mismatches = [
            Mismatch("<single>", left, right, rows[left], rows[right], normalized[left], normalized[right])
            for left, right in pairs
            if normalized[left] != normalized[right]
        ]
        ref_mismatches = _reference_check(case, instance, {s: rows[s] for s in base_data_models})
        return CaseResult(case=case, ok=not mismatches and not ref_mismatches, rows=rows, errors=errors, mismatches=mismatches, reference_mismatches=ref_mismatches)

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
            ln, rn = normalize(case.comparison, lv), normalize(case.comparison, rv)
            if ln != rn:
                mismatches.append(Mismatch(rid, left, right, lv, rv, ln, rn))
    ref_mismatches = _reference_check(case, instance, {s: maps[s] for s in base_data_models})
    return CaseResult(
        case=case,
        ok=not mismatches and not ref_mismatches and not dups,
        rows=rows,
        errors=errors,
        mismatches=mismatches,
        reference_mismatches=ref_mismatches,
        duplicate_id_rows=dups,
    )
