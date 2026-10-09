"""The SAP HANA provider without a server: its connection layer's pure parts, its value
semantics, its rewrites, and that every plan and every case renders."""
from __future__ import annotations

import itertools
import json
import random
from decimal import Decimal

import pytest

from ganda.core.ast.predicates import Compound, Predicate
from ganda.core.ast.write import apply
from ganda.core.data_generator import build
from ganda.core.generators.registry import all_cases
from ganda.core.generators.write import sample_write
from ganda.core.model import FieldType
from ganda.hana import rewrites
from ganda.hana.config import UNSUPPORTED_AXES
from ganda.hana.connection import decode_column, decode_row, positional, sparql_rows
from ganda.hana.dialect import list_text, record_iri, sparql_literal, sparql_string
from ganda.hana.physical_design import BOUNDARY_VALUES
from ganda.hana.provider import PROVIDER as E
from ganda.hana.semantics import UNKNOWN, evaluate, mean, tri

CFG = E.config_class(write_form_probability=0.8, churn_edge_update_probability=0.3, clusters_probability=0.3)


# --- the connection layer's pure parts --------------------------------------------------------


def test_positional_binds_in_order_and_expands_lists():
    text, params = positional("SELECT x FROM t WHERE a = :p AND b IN :q AND c = 'it:s' AND \"d:x\" = :p", {"p": 1, "q": [2, 3]})
    assert text == "SELECT x FROM t WHERE a = ? AND b IN (?, ?) AND c = 'it:s' AND \"d:x\" = ?"
    assert params == [1, 2, 3, 1]


def test_positional_derives_json_decimal_and_elements():
    text, params = positional("f = :p__json AND g = CAST(:x__dec AS DECIMAL) AND h = :p__1 AND k IN :e", {"p": [1, 2], "x": 0.30000000000000004, "e": []})
    assert params == ["[1,2]", Decimal("0.30000000000000004"), 2]
    assert text.endswith("k IN (NULL)")
    with pytest.raises(KeyError):
        positional("f = :missing", {})


def test_result_columns_decode_their_suffix():
    assert decode_column("V__JSON".lower(), "[1, 2]") == ("v", [1, 2])
    assert decode_column("v__bool", "true") == ("v", True)
    assert decode_column("v__bool", 0) == ("v", False)
    assert decode_column("v__int", "9007199254740993") == ("v", 9007199254740993)
    assert decode_column("v__num", Decimal("2.5")) == ("v", 2.5)
    assert decode_column("v__num", "4") == ("v", 4)
    assert decode_row(["ID", "V"], ["n1/r0", Decimal("3.0")]) == {"id": "n1/r0", "v": 3}


def test_sparql_json_results_decode_per_datatype():
    doc = {"head": {"vars": ["id", "v", "w__json", "b"]}, "results": {"bindings": [{
        "id": {"type": "literal", "value": "n1/r0"},
        "v": {"type": "literal", "value": "9223372036854775807", "datatype": "http://www.w3.org/2001/XMLSchema#long"},
        "w__json": {"type": "literal", "value": "[1,2]"},
        "b": {"type": "literal", "value": "true", "datatype": "http://www.w3.org/2001/XMLSchema#boolean"},
    }]}}
    assert sparql_rows(json.dumps(doc)) == [{"id": "n1/r0", "v": 9223372036854775807, "w": [1, 2], "b": True}]
    assert sparql_rows(json.dumps({"head": {}, "boolean": True})) == [{"v": True}]


def test_sparql_literals():
    assert sparql_string('a"b\\c\nd\x01') == '"a\\"b\\\\c\\nd\\u0001"'
    assert sparql_literal(5) == '"5"^^xsd:long'
    assert sparql_literal(0.1) == '"0.1"^^xsd:double'
    assert sparql_literal(True) == "true"
    assert sparql_literal([1, 2]) == '"[1,2]"'
    assert record_iri("n1/a b") == "<urn:ganda:r:n1/a%20b>"
    assert list_text([3, -1]) == "[3,-1]"


# --- semantics and rewrites -------------------------------------------------------------------


def _leaf(field, op, pivot=None, ftype="int"):
    return Predicate("source", field, op, "" if op in ("IS NULL", "IS NOT NULL") else f"b{field}", pivot, True, ftype)


def test_three_valued_logic():
    gt = _leaf("f", ">", 5)
    assert tri({}, gt) is UNKNOWN
    assert tri({}, Compound("source", "NOT", (gt,))) is UNKNOWN
    assert tri({}, Compound("source", "OR", (gt, _leaf("g", "IS NULL")))) is True
    assert tri({}, Compound("source", "AND", (gt, _leaf("g", "IS NOT NULL")))) is False
    assert not evaluate({}, Compound("source", "NOT", (gt,)))
    # membership is two-valued: false over a null list
    assert evaluate({"l": None}, Compound("source", "NOT", (_leaf("l", "CONTAINS", 1, "list"),)))
    # a semi-join over a null field is unknown; its values' nulls are ignored
    sub = Predicate("source", "f", "INSUB", "", [None, 3], True, "int", sub=("n2", "g", None, False))
    assert tri({}, sub) is UNKNOWN and tri({"f": 4}, sub) is False and tri({"f": 3}, sub) is True


def test_mean_is_the_exact_quotient():
    assert mean([1, 2]) == 1.5 and mean([2, 4]) == 3 and isinstance(mean([2, 4]), int)
    assert mean([2**53 + 1]) == 2**53 + 1


def _random_tree(rng, depth=0):
    fields = {"a": FieldType.INT, "b": FieldType.INT, "l": FieldType.LIST}
    if depth < 2 and rng.random() < 0.5:
        op = rng.choice(("AND", "OR", "NOT"))
        kids = (_random_tree(rng, depth + 1),) if op == "NOT" else (_random_tree(rng, depth + 1), _random_tree(rng, depth + 1))
        return Compound("source", op, kids)
    f = rng.choice(list(fields))
    if fields[f] is FieldType.LIST:
        op = rng.choice(("CONTAINS", "CONTAINSANY", "CONTAINSALL", "=", "IS NULL"))
        pivot = {"CONTAINS": rng.randint(0, 3), "=": [rng.randint(0, 3)], "IS NULL": None}.get(op, [rng.randint(0, 3) for _ in range(2)])
        return _leaf(f, op, pivot, "list")
    op = rng.choice(("=", "<>", "<", "<=", ">", ">=", "IN", "BETWEEN", "IS NULL", "IS NOT NULL"))
    pivot = {"IN": [rng.randint(0, 3), rng.randint(0, 3)], "BETWEEN": sorted([rng.randint(0, 3), rng.randint(0, 3)])}.get(op, rng.randint(0, 3) if op not in ("IS NULL", "IS NOT NULL") else None)
    return _leaf(f, op, pivot)


def _records():
    values = [None, 0, 1, 2, 3]
    lists = [None, [], [0], [1, 2], [3, 3]]
    for a, b, l in itertools.product(values, values, lists):
        yield {k: v for k, v in (("a", a), ("b", b), ("l", l)) if v is not None or k == "a"}


def test_every_rewrite_keeps_the_truth_value_of_every_record():
    rng = random.Random(7)
    records = list(_records())
    checked = 0
    for _ in range(400):
        p = _random_tree(rng)
        r = rewrites.rewrite(p, rng, "q")
        if r is None:
            continue
        checked += 1
        for values in records:
            assert tri(values, p) == tri(values, r), (p, r, values)
    assert checked > 200


def test_the_arithmetic_rewrite_keeps_every_answer():
    rng = random.Random(3)
    for _ in range(300):
        op = rng.choice(("=", "<>", "<", "<=", ">", ">="))
        p = _leaf("a", op, rng.randint(-5, 5))
        values = [rng.randint(-5, 5) for _ in range(6)] + [None]
        r = rewrites.arithmetic(p, rng, "ar", FieldType.INT, values)
        assert r is not None
        for v in values:
            assert tri({"a": v}, p) == tri({"a": v}, r)


def test_the_value_pools_hold_what_every_data_model_keeps_alike():
    assert not any(isinstance(v, str) and any(ord(c) > 0xFFFF for c in v) for v in BOUNDARY_VALUES[FieldType.STRING])
    assert not any(v == 0 and str(v).startswith("-") for v in BOUNDARY_VALUES[FieldType.FLOAT])
    assert set(BOUNDARY_VALUES) == {FieldType.INT, FieldType.FLOAT, FieldType.STRING, FieldType.BOOL, FieldType.LIST}


def test_the_axes_the_provider_does_not_model_are_refused():
    for axis in UNSUPPORTED_AXES:
        with pytest.raises(SystemExit):
            E.configure(None, E.config_class(**{axis: 0.5}))
    assert E.configure(None, CFG) is CFG


# --- every plan, case and write renders --------------------------------------------------------


@pytest.mark.parametrize("seed", range(1, 6))
def test_every_plan_case_and_write_renders(seed):
    schema, instance, rng = build(seed, CFG, E)
    for dm in E.data_models:
        plan = E.mappers[dm](instance)
        assert plan and json.dumps([s.to_dict() for s in plan], default=repr)
    cases = all_cases(rng, schema, instance, cfg=CFG)
    assert {c.generator_name for c in cases} >= {"ir_traversal", "ir_select", "ir_path"}
    for c in cases:
        assert all(text for text in c.queries.values())
    wr = rng.for_generator("ir_write")
    cur = instance
    for step in range(6):
        drawn = sample_write(wr, schema, cur, step)
        if drawn is None:
            continue
        q, rw, syntaxes = drawn
        after = apply(q, cur)
        for dm in E.data_models:
            st = E.write_syntax.render(q, syntaxes[dm][0], rw, dm, cur, after)
            assert st.display() is not None
        cur = after


# --- what the live campaign of 2026-10-09 (campaign-20261009-095838) turned up ---------------


def test_the_state_check_reads_a_double_from_its_exact_text():
    from ganda.hana.connection import HanaSession

    s = HanaSession(None, "db", "rdf")
    s.types = {"e6": {"f1": "float", "l": "list"}, "n1": {"f2": "int"}}
    # STR prints `%.17g`: an integral double has no point and no exponent, while its
    # term is the rounded `9.0072e+15` (H1)
    assert s._exact("e6", "f1", 9.0072e15, "9007199254740992") == 9007199254740992.0
    assert s._exact("e6", "f1", 3.14159, "3.1415926535897931") == 3.141592653589793
    # an integer element of a LIST keeps its own type
    assert s._exact("e6", "l", 5, "5") == 5 and isinstance(s._exact("e6", "l", 5, "5"), int)
    assert s._exact("n1", "f2", 7, "7") == 7


@pytest.mark.parametrize("seed", range(1, 6))
def test_no_sparql_select_list_names_a_never_bound_variable(seed):
    """`?ganda_unknown` outside an aggregate in a grouped SELECT is refused ("Non GROUP BY
    variable in SELECT", SPARQL 1.1 11.4); the sentinel belongs in a FILTER only."""
    import re

    schema, instance, rng = build(seed, CFG, E)
    clause = re.compile(r"SELECT\s+(.*?)\s+(?:WHERE|FROM)\b", re.S)
    for c in all_cases(rng, schema, instance, cfg=CFG):
        for variant, text in c.queries.items():
            if variant.startswith("rdf"):
                for m in clause.finditer(text):
                    assert "ganda_unknown" not in m.group(1), (variant, text)


def test_the_doc_views_ask_containsall_without_a_scalar_subquery():
    from ganda.hana.concrete_syntax.document import NAMES
    from ganda.hana.concrete_syntax.predicate import sql

    p = _leaf("l", "CONTAINSALL", [1, 2], "list")
    assert "COUNT(DISTINCT" in sql(p, "t", "n1")
    # over the doc views the count is a correlated scalar subquery over UNNEST (H2)
    text = sql(p, "t", "n1", names=NAMES)
    assert "COUNT" not in text and text.count("EXISTS") == 2
    assert positional(text, {"bl": [1, 2]})[1] == [1, 2]


def test_a_doc_traversal_withholds_a_list_membership():
    from ganda.core.ast.traversal import Hop, TraversalQuery
    from ganda.hana.concrete_syntax import traversal

    member = Predicate("target", "l", "CONTAINS", "bl", 1, True, "list")
    q = TraversalQuery("n1", (Hop("e1", "out", member),), "ids")
    # the Document Store refuses an EXISTS over an element view next to the UNNEST edge
    # views (B6) and crashes on the CONTAINSALL count (H2)
    assert not any(traversal.applicable(q, None, "doc", s) for s in traversal.SYNTAXES["doc"])
    plain = TraversalQuery("n1", (Hop("e1", "out", _leaf("f", ">", 1)),), "ids")
    assert all(traversal.applicable(plain, None, "doc", s) for s in traversal.SYNTAXES["doc"])
