"""The oracle's provider-set options (`core/engine/provider.py`): comparing the variants that
answered when another one errored, the error signature in the finding key, and a
traversal case left partial by a data model with no syntax -- all off or inert on the
default provider."""
from __future__ import annotations

from ganda.core.oracle.case import Comparison, QueryCase
from ganda.core.data.generator import build
from ganda.core.oracle.dedup import BugDeduplicator
from ganda.core.query.generators.registry import all_cases
from ganda.core.oracle.oracle import build_result
from tests.fake_engine import SMALL, FakeProvider


class _Despite(FakeProvider):
    compare_despite_errors = True

    def error_signature(self, message: str) -> str:
        return message.split("]", 1)[0] + "]" if message.startswith("[") else ""


def _case(engine) -> QueryCase:
    return QueryCase("ir_select", "", Comparison.SCALAR_BY_ID, {"doc": "d", "graph": "g", "rel": "r", "rel:scan": "s"}, engine=engine)


def _rows(v):
    return [{"id": "a", "v": v}]


def test_an_error_ends_the_comparison_by_default_and_not_under_the_option():
    rows = {"doc": _rows(1), "graph": _rows(2), "rel": [], "rel:scan": _rows(1)}
    errors = {"doc": "", "graph": "", "rel": "[7] refused", "rel:scan": ""}
    plain = build_result(_case(FakeProvider()), rows, errors)
    assert plain.finding_class == "error_asymmetry" and not plain.mismatches and not plain.ok
    despite = build_result(_case(_Despite()), rows, errors)
    assert despite.finding_class == "error_asymmetry" and not despite.ok
    # the answering variants were compared: doc and rel:scan agree, graph differs
    assert {m.pair for m in despite.mismatches} == {"doc/graph", "graph/rel:scan"}
    # a symmetric error stays ok either way, with nothing to compare
    every = {s: "[7] refused" for s in rows}
    assert build_result(_case(_Despite()), {s: [] for s in rows}, every).ok
    assert build_result(_case(FakeProvider()), {s: [] for s in rows}, every).ok
    # no error: the same comparison as before
    clean = build_result(_case(_Despite()), {**rows, "rel": _rows(1)}, {s: "" for s in rows})
    assert clean.finding_class == "mismatch" and {m.pair for m in clean.mismatches} == {"doc/graph", "graph/rel", "graph/rel:scan"}


def test_the_finding_key_carries_the_error_signature_and_a_hidden_mismatch():
    rows = {"doc": _rows(1), "graph": _rows(1), "rel": [], "rel:scan": _rows(1)}
    seven = build_result(_case(_Despite()), rows, {"doc": "", "graph": "", "rel": "[7] refused", "rel:scan": ""})
    eight = build_result(_case(_Despite()), rows, {"doc": "", "graph": "", "rel": "[8] other", "rel:scan": ""})
    assert BugDeduplicator.key(seven) != BugDeduplicator.key(eight)
    assert BugDeduplicator.key(seven)[2] == ("rel#[7]",)
    hidden = build_result(_case(_Despite()), {**rows, "graph": _rows(2)}, {"doc": "", "graph": "", "rel": "[7] refused", "rel:scan": ""})
    assert BugDeduplicator.key(hidden) != BugDeduplicator.key(seven) and "doc/graph" in BugDeduplicator.key(hidden)[2]
    # the default provider: the key names the variants alone, as the baseline records
    plain = build_result(_case(FakeProvider()), rows, {"doc": "", "graph": "", "rel": "[7] refused", "rel:scan": ""})
    assert BugDeduplicator.key(plain)[2] == ("rel",)


class _Partial(FakeProvider):
    partial_traversals = True


def test_a_data_model_without_a_traversal_syntax_leaves_the_case_partial_where_the_provider_says(monkeypatch):
    engine = FakeProvider()
    cfg = engine.config_class(**SMALL)
    schema, instance, rng = build(7, cfg, engine)
    whole = [c for c in all_cases(rng, schema, instance, cfg=cfg) if c.generator_name == "ir_traversal"]
    assert whole and not any(c.params.get("partial") for c in whole)
    syn = engine.traversal_syntax
    original = syn.syntaxes_for

    def without_doc(q, schema, data_model):
        return [] if data_model == "doc" else original(q, schema, data_model)

    monkeypatch.setattr(syn, "syntaxes_for", without_doc)
    # the default: a question doc cannot ask is dropped on every data model
    schema, instance, rng = build(7, cfg, engine)
    dropped = [c for c in all_cases(rng, schema, instance, cfg=cfg) if c.generator_name == "ir_traversal"]
    assert not dropped
    # the option: the case is partial, asked of the other data models
    partial_engine = _Partial()
    schema, instance, rng = build(7, partial_engine.config_class(**SMALL), partial_engine)
    partial = [c for c in all_cases(rng, schema, instance, cfg=cfg) if c.generator_name == "ir_traversal"]
    assert partial
    assert all(c.params.get("partial") and set(v.split(":")[0] for v in c.queries) == {"graph", "rel"} for c in partial)
