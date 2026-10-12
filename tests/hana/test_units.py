"""The SAP HANA provider without a server: its connection layer's pure parts, its value
semantics, its rewrites, and that every plan and every case renders."""
from __future__ import annotations

import itertools
import json
import random
import re
from decimal import Decimal

import pytest

from ganda.core.ast.predicates import Compound, Predicate
from ganda.core.ast.write import apply
from ganda.core.data_generator import build
from ganda.core.generators.registry import all_cases
from ganda.core.generators.write import sample_write
from ganda.core.model import EdgeTypeSpec, FieldSpec, FieldType, NodeTypeSpec
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
    op = rng.choice(("=", "<>", "<", "<=", ">", ">=", "IN", "NOT IN", "BETWEEN", "IS NULL", "IS NOT NULL"))

    def num():
        # an integer, or a cross-type pivot (a double half a unit off: `pivots.cross_type`)
        return rng.randint(0, 3) + (0.5 if rng.random() < 0.3 else 0)

    pivot = {"IN": [num(), num()], "NOT IN": [num(), num()], "BETWEEN": sorted([num(), num()])}.get(op, num() if op not in ("IS NULL", "IS NOT NULL") else None)
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


def test_cross_type_pivots_stay_inside_the_bound_and_render_typed_on_every_data_model():
    from ganda.hana import pivots
    from ganda.hana.concrete_syntax import document, graph, predicate

    rng = random.Random(5)
    assert pivots.cross_type(rng, FieldType.INT, 7) in (6.5, 7.5) and pivots.cross_type(rng, FieldType.FLOAT, 2.6) == 3
    for v in (None, True, 10**12, -(10**12), 2**63 - 1, 1e300):
        assert pivots.cross_type(rng, FieldType.INT, v) is v or pivots.cross_type(rng, FieldType.INT, v) == v
    assert "NOT IN" in pivots.ORDERED_OPS
    schema = E.schema((NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT), FieldSpec("f5", FieldType.FLOAT))),), ())
    gt = Predicate("source", "f1", ">", "p", 1.5, True, "int")
    isin = Predicate("source", "f1", "IN", "p", [1.5, 2], True, "int")
    btw = Predicate("source", "f1", "BETWEEN", "p", [0.5, 2.5], True, "int")
    rev = Predicate("source", "f1", "<", "p", 1.5, True, "int", rev=True)
    fint = Predicate("source", "f5", "=", "p", 2, True, "float")
    # SQL types the parameter by the pivot (a bare `?` would take BIGINT and truncate)
    assert predicate.sql(gt, "t", "n1") == "t.f1 > CAST(:p AS DOUBLE)"
    assert predicate.sql(isin, "t", "n1") == "t.f1 IN (CAST(:p__0 AS DOUBLE), :p__1)"
    assert predicate.sql(btw, "t", "n1") == "t.f1 BETWEEN CAST(:pa AS DOUBLE) AND CAST(:pb AS DOUBLE)"
    assert predicate.sql(rev, "t", "n1") == "CAST(:p AS DOUBLE) > t.f1"
    assert predicate.sql(fint, "t", "n1") == "t.f5 = :p"
    # the collection filter casts the pivot; a double against an integer field as DOUBLE
    assert document.doc_pred(gt) == '$."f1" > CAST(:p AS DOUBLE)' and document.doc_pred(fint) == '$."f5" = CAST(:p__dec AS DECIMAL)'
    # SPARQL: literals of the pivot's own type, compared numerically
    assert predicate.sparql_filter(gt, "r") == 'OPTIONAL { ?r p:f1 ?r_f1 } FILTER ((BOUND(?r_f1) && (?r_f1 > "1.5"^^xsd:double)))'
    # openCypher: both sides doubles, the pivot as TOFLOAT of its text (G36), no parameter
    for leaf, text in ((gt, "(TOFLOAT(t.N1_F1) > TOFLOAT('1.5'))"), (isin, "(TOFLOAT(t.N1_F1) = TOFLOAT('1.5') OR TOFLOAT(t.N1_F1) = TOFLOAT('2'))"), (btw, "(TOFLOAT(t.N1_F1) >= TOFLOAT('0.5') AND TOFLOAT(t.N1_F1) <= TOFLOAT('2.5'))"), (rev, "(TOFLOAT('1.5') > TOFLOAT(t.N1_F1))"), (fint, "(t.N1_F5 = TOFLOAT('2'))")):
        params = graph.Params()
        assert graph.cypher_filter(leaf, "t", "n1", schema, params) == text and not params.items
    # the reference compares exactly across the types
    assert tri({"f1": 2}, gt) is True and tri({"f1": 1}, gt) is False and tri({}, gt) is UNKNOWN and tri({"f5": 2.0}, fint) is True


def test_not_in_renders_on_every_data_model_and_rewrites_three_valued():
    from ganda.hana.concrete_syntax import document, graph, predicate

    schema = E.schema((NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT),)),), ())
    p = Predicate("source", "f1", "NOT IN", "p", [1, 2], True, "int")
    assert predicate.sql(p, "t", "n1") == "t.f1 NOT IN :p"
    assert document.doc_pred(p) == '($."f1" <> CAST(:p__0 AS BIGINT) AND $."f1" <> CAST(:p__1 AS BIGINT))'
    assert predicate.sparql_filter(p, "r").endswith('FILTER ((BOUND(?r_f1) && (?r_f1 NOT IN ("1"^^xsd:long, "2"^^xsd:long))))')
    assert graph.cypher_filter(p, "t", "n1", schema, graph.Params()) == "NOT (t.N1_F1 = 1 OR t.N1_F1 = 2)"
    assert tri({"f1": 3}, p) is True and tri({"f1": 2}, p) is False and tri({}, p) is UNKNOWN
    rng = random.Random(1)
    r = rewrites.rewrite(p, rng, "q")
    assert isinstance(r, Compound) and r.op == "NOT" and r.children[0].op == "IN"
    back = rewrites.rewrite(r, rng, "q")
    assert back.op == "NOT IN" and back.pivot == [1, 2]


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


# --- what campaign-20261009-165805 turned up ------------------------------------------------------


@pytest.mark.parametrize("seed", range(1, 6))
def test_no_rdf_sum_or_avg_sees_an_unbound_value(seed):
    """Over a predicate holding several datatypes across the graph, HANA's SUM / AVG answer
    unbound for a group with an unbound value in it: a select `agg` binds its value
    where present only, a `group` sums `COALESCE(?a, 0)`."""
    import re

    schema, instance, rng = build(seed, CFG, E)
    for c in all_cases(rng, schema, instance, cfg=CFG):
        for variant, text in c.queries.items():
            if not variant.startswith("rdf") or c.generator_name != "ir_select":
                continue
            if re.search(r"(SUM|AVG)\(\?w\)", text):
                assert not re.search(r"OPTIONAL \{ \?n p:\w+ \?w \}", text), text
            if "SUM(" in text and "?a" in text:
                assert "SUM(COALESCE(?a, " in text, text


def test_the_doc_views_ask_a_membership_only_as_a_conjunct():
    from ganda.hana.concrete_syntax.document import views_ask

    member = _leaf("l", "CONTAINS", 1, "list")
    other = _leaf("f", ">", 1)
    assert views_ask(member) and views_ask(Compound("source", "AND", (member, other)))
    # under OR or NOT, or inside a semi-join, the Document Store refuses the EXISTS (B6)
    assert not views_ask(Compound("source", "OR", (member, other)))
    assert not views_ask(Compound("source", "NOT", (member,)))
    assert not views_ask(Compound("source", "AND", (other, Compound("source", "OR", (member, other)))))
    semi = Predicate("source", "f", "INSUB", "", [3], True, "int", sub=("n2", "g", member, False))
    assert not views_ask(semi)
    assert views_ask(Compound("source", "OR", (other, _leaf("g", "IS NULL"))))


def test_h4_withholds_a_doc_sum_over_a_column_that_may_overflow():
    from types import SimpleNamespace as NS

    from ganda.hana.known_bugs import _h4

    def instance(*values):
        return NS(edges=[NS(type_name="e1", values={"w": v}) for v in values])

    q = NS(projection="esum", field="w", hops=(NS(edge_type="e1", direction="out"),))
    # an excluded row may carry a value that overflows the sum: 2^63 - 1 next to 1
    assert _h4(q, None, instance(2**63 - 1, 1, None))
    assert not _h4(q, None, instance(5, -7, None))
    assert not _h4(NS(projection="emax", field="w", hops=q.hops), None, instance(2**63 - 1, 1))


def test_a_129_that_wraps_an_ordinary_refusal_is_a_rejection():
    from types import SimpleNamespace as NS

    from ganda.hana.connection import _error

    def err(code, text):
        return _error(NS(errorcode=code, errortext=text))

    # the triple store answers every refusal with 129 (probed): not an internal error
    assert not err(129, "transaction rolled back by an internal error: generic. Error - ?n: Non GROUP BY variable in SELECT").internal
    assert not err(129, "transaction rolled back by an internal error: generic. System error. Reference: Unsupported functionality: MEDIAN").internal
    assert not err(129, "transaction rolled back by an internal error: max supports only numeric values").internal
    assert err(129, "transaction rolled back by an internal error: something else").internal
    assert err(3, "fatal error: Execution flow must not reach here").internal
    assert err(613, "execution aborted by timeout").timeout


# --- found offline: the pulled-up traversal's outer alias --------------------------------------


@pytest.mark.parametrize("dm", ("rel", "doc"))
def test_a_pulled_up_traversal_correlates_a_membership_on_its_own_alias(dm):
    """`predicate.sql` aliases a LIST column's child table `x`; the pulled-up derived table
    must not, or the membership EXISTS correlates the child table with itself
    (`x.owner = x.id`, a column the child table lacks)."""
    from ganda.core.ast.traversal import Hop, TraversalQuery
    from ganda.hana.concrete_syntax import traversal

    schema = E.schema(
        (NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT), FieldSpec("f3", FieldType.LIST))),),
        (EdgeTypeSpec("e1", "n1", "n1", (FieldSpec("w", FieldType.INT),)),),
    )
    member = Predicate("source", "f3", "CONTAINS", "p0", 1, True, "list")
    for proj in ("ids", "exists"):
        q = TraversalQuery("n1", (Hop("e1", "out", None),), proj, None, member)
        for syntax in traversal.SYNTAXES[dm]:
            text = traversal.render(q, schema, dm, syntax, pull_up=True)
            assert ") x WHERE" not in text, text
            assert "x.owner = pu.id AND x.v = :p0" in text, text


# --- the graph data model: the Property Graph Engine through OPENCYPHER_TABLE ------------------


def _cases(seed: int):
    schema, instance, rng = build(seed, CFG, E)
    return schema, instance, all_cases(rng, schema, instance, cfg=CFG)


def _graph_texts(seed: int) -> list:
    """(case, variant, text) of every graph variant of a seed's cases."""
    _schema, _instance, cases = _cases(seed)
    return [(c, v, t) for c in cases for v, t in c.queries.items() if v.split(":", 1)[0] == "graph"]


_LITERAL = re.compile(r"QUERY '((?:[^']|'')*)'")


def _cypher_literals(text: str) -> list:
    return [m.group(1).replace("''", "'") for m in _LITERAL.finditer(text)]


def test_positional_leaves_the_cypher_literal_alone():
    """Labels `(a:n1)`, ranges `[*1..2]`, `$p` and `:` inside the quoted query are not
    binds; the PARAMETERS clause outside it is."""
    text = "SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE gw QUERY 'MATCH (a:n1)-[:e1*1..2]->(b:n2) WHERE a.N1_F1 > $p AND a.N1_F2 = ''it:s'' RETURN {k: a.ID} AS ID' PARAMETERS ('p' = :p))"
    stmt, params = positional(text, {"p": 1})
    assert params == [1]
    assert stmt.endswith("PARAMETERS ('p' = ?))")
    assert "(a:n1)-[:e1*1..2]->(b:n2)" in stmt and "$p" in stmt and "''it:s''" in stmt


def test_opencypher_table_doubles_quotes_and_declares_the_parameters_a_query_uses():
    from ganda.hana.concrete_syntax import graph
    from ganda.hana.dialect import opencypher_table

    assert opencypher_table("MATCH (a:n1) WHERE a.S = 'x' RETURN a.ID AS ID") == "OPENCYPHER_TABLE(GRAPH WORKSPACE gw QUERY 'MATCH (a:n1) WHERE a.S = ''x'' RETURN a.ID AS ID')"
    params = graph.Params()
    assert params.use("p") == "$p" and params.use("p__0") == "$p__0"
    text = graph.table_fn("MATCH (a:n1) WHERE a.N1_F1 = $p__0 RETURN a.ID AS ID", params)
    assert text.endswith("PARAMETERS ('p__0' = :p__0))"), text  # `$p` is unused there: not declared


@pytest.mark.parametrize("seed", range(1, 6))
def test_graph_attribute_names_have_one_type_across_the_workspace(seed):
    """The constraint the views exist for: an attribute name has one SQL type across the
    vertex tables and across the edge tables."""
    from ganda.hana.dialect import SQL_TYPES, graph_columns

    schema, _instance, _rng = build(seed, CFG, E)
    for tables, edge in ((schema.node_types, False), (schema.edge_types, True)):
        seen: dict = {}
        for t in tables:
            fields = {f.name: f.type for f in (t.fields if edge else schema.query_fields(t.name))}
            for col, attr in graph_columns(schema, t.name):
                if col in fields:
                    assert seen.setdefault(attr, SQL_TYPES[fields[col]]) == SQL_TYPES[fields[col]], attr


@pytest.mark.parametrize("seed", range(1, 4))
def test_the_graph_plan_is_the_rel_plan_plus_views_and_one_workspace(seed):
    from ganda.core.setup_plan import plan_from_dict, plan_to_dict
    from ganda.hana.mapping.common import GraphWorkspace, Sql

    schema, instance, _rng = build(seed, CFG, E)
    plan = E.mappers["graph"](instance)
    ws = [s for s in plan if isinstance(s, GraphWorkspace)]
    assert len(ws) == 1 and ws[0].cache in ("dynamic", "none")
    views = [s.text for s in plan if isinstance(s, Sql) and s.text.startswith("CREATE VIEW gv_")]
    assert len(views) == len(schema.node_types) + len(schema.edge_types)
    assert ws[0].sql().startswith("CREATE GRAPH WORKSPACE gw VERTEX TABLE gv_n1 KEY COLUMN id LABEL 'n1'")
    assert all("REFERENCES gv_" in part for part in ws[0].sql().split("EDGE TABLE")[1:])
    assert plan_to_dict(plan_from_dict(plan_to_dict(plan))) == plan_to_dict(plan)
    # the data steps are the rel plan's for the same write form
    rel = [s.to_dict() for s in E.mappers["rel"](instance) if not (isinstance(s, Sql) and s.text.startswith(("CREATE", "MERGE DELTA")))]
    gr = [s.to_dict() for s in plan if not (isinstance(s, (Sql, GraphWorkspace)) and (isinstance(s, GraphWorkspace) or s.text.startswith(("CREATE", "MERGE DELTA"))))]
    if instance.write_form("rel") == instance.write_form("graph"):
        assert rel == gr


@pytest.mark.parametrize("seed", range(1, 4))
def test_every_traversal_question_has_a_graph_syntax_and_every_select_a_graph_base(seed):
    from ganda.core.ast.traversal import from_params
    from ganda.hana.concrete_syntax import select, traversal

    schema, _instance, cases = _cases(seed)
    for c in cases:
        if c.generator_name == "ir_traversal":
            q = from_params(c.params["traversal"])
            assert traversal.syntaxes_for(q, schema, "graph"), c.shape
            assert "graph" in c.queries
        if c.generator_name == "ir_select":
            from ganda.core.ast.select import from_params as select_params

            q = select_params(c.params["select"])
            assert "graph" in select.base_data_models(q) or q.tx


def test_pattern_only_where_no_edge_can_repeat_and_undirected_only_for_sets():
    from ganda.core.ast.traversal import Hop, TraversalQuery
    from ganda.hana.concrete_syntax import traversal

    schema = E.schema(
        (NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT),)), NodeTypeSpec("n2", (FieldSpec("f1", FieldType.STRING),))),
        (EdgeTypeSpec("e1", "n1", "n2", ()), EdgeTypeSpec("e2", "n2", "n1", ()), EdgeTypeSpec("e3", "n1", "n1", (FieldSpec("w", FieldType.INT),))),
    )

    def ok(hops, proj, syntax):
        return traversal.applicable(TraversalQuery("n1", tuple(Hop(e, d, None) for e, d in hops), proj, None, None), schema, "graph", syntax)

    # one MATCH clause, a relationship bound once in it: a native multi-hop only where no
    # edge could repeat (G8); the views family always
    assert ok([("e1", "out"), ("e2", "out")], "ids", "match")
    assert not ok([("e3", "out"), ("e3", "out")], "ids", "match")  # the self-loop could bind twice
    assert not ok([("e1", "out"), ("e1", "in")], "count", "paths")  # the same relationship back
    assert ok([("e3", "out"), ("e3", "out")], "ids", "join") and ok([("e3", "out"), ("e3", "out")], "ids", "scalar")
    assert ok([("e3", "both")], "distinct_ids", "undirected") and ok([("e3", "both")], "nexists", "undirected")
    assert not ok([("e3", "both")], "ids", "undirected") and not ok([("e3", "out")], "distinct_ids", "undirected")
    assert ok([("e3", "both")], "count", "paths") and not ok([("e3", "both")], "count", "match")  # a both aggregate: SQL-side only
    assert ok([("e1", "out")], "reach2", "match") and not ok([("e1", "out")], "reach2", "paths")
    assert ok([("e1", "out")], "distinct_count", "paths") and not ok([("e1", "out")], "distinct_count", "match")
    for syntax in traversal.SYNTAXES["graph"]:
        assert ok([("e1", "out")], "ids", syntax) == (syntax in ("match", "join", "scalar")), syntax


@pytest.mark.parametrize("seed", range(1, 4))
def test_graph_texts_are_well_formed(seed):
    """No incoming arrow, ONE MATCH clause (G8), uppercase RETURN aliases, every native
    SUM guarded by its COUNT and a CASE outside, every `$name` declared, every bind
    resolvable, no two graph variants of a case alike."""
    by_case: dict = {}
    for c, v, t in _graph_texts(seed):
        # every NATIVE graph variant of a case is its own text -- except that a `rw`
        # variant may coincide with its base (a rewrite of `IN [x]` as `= x` inlines the
        # same constant); the views family inherits rel's own coincidences (join / scalar
        # on a reach question)
        native = "g_" not in t and "rw" not in v.split(":", 1)[-1].split("+")
        by_case.setdefault(id(c), []).append(t if native else None)
        positional(t, {**c.bind_vars})
        for lit in _cypher_literals(t):
            assert "<-[" not in lit, lit
            assert lit.count("MATCH ") == 1, lit
            for alias in re.findall(r" AS ([A-Za-z_][A-Za-z0-9_]*)", lit.split(" RETURN ", 1)[-1]):
                assert alias == alias.upper(), (alias, lit)
            if "SUM(" in lit:
                assert "COUNT(" in lit and "THEN NULL ELSE" in t, t
            declared = set(re.findall(r"'(\w+)' = :", t))
            outside_strings = re.sub(r"'(?:[^'\\]|\\.)*'", "''", lit)  # a constant may hold a `$`
            assert set(re.findall(r"\$(\w+)", outside_strings)) <= declared, (lit, declared)
    for texts in by_case.values():
        texts = [t for t in texts if t is not None]
        assert len(set(texts)) == len(texts)


def test_cypher_literals_escape_and_refuse():
    from ganda.hana.concrete_syntax import graph

    assert graph.cypher_string("it's\\") == "'it\\'s\\\\'"
    assert graph.literal(2**63 - 1) == "9223372036854775807" and graph.literal(-5) == "-5" and graph.literal("it's") == "'it\\'s'"
    assert graph.literal([1, 2]) == "'[1,2]'"
    # a double has no constant (the parser keeps ~15 digits, A8): bound instead
    for bad in ("a\nb", 0.1, -2.0, 1e-05, float("inf"), True, None):
        with pytest.raises(graph.Unsupported):
            graph.literal(bad)
    with pytest.raises(graph.Unsupported):
        graph.literal(2, "float")


def test_cypher_filter_mirrors_the_sql_binds_and_refuses_what_has_no_form():
    from ganda.hana.concrete_syntax import graph, predicate

    schema = E.schema((NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT), FieldSpec("f2", FieldType.STRING), FieldSpec("f3", FieldType.LIST), FieldSpec("f4", FieldType.BOOL), FieldSpec("f5", FieldType.FLOAT))),), ())
    leaves = {
        "cmp": Predicate("source", "f1", ">", "p", 1, True, "int"),
        "rev": Predicate("source", "f1", "<", "p", 1, True, "int", rev=True),
        "in": Predicate("source", "f1", "IN", "p", [1, 2], True, "int"),
        "between": Predicate("source", "f1", "BETWEEN", "p", [1, 2], True, "int"),
        "null": Predicate("source", "f1", "IS NOT NULL", "", None, True, "int"),
        "like": Predicate("source", "f2", "LIKE", "p", "ab%", True, "string"),
        "str": Predicate("source", "f2", "=", "p", "it's", True, "string"),
        "arith": Predicate("source", "f1", ">=", "p", 3, True, "int", arith=("+", "arc", 2)),
        "list": Predicate("source", "f3", "=", "p", [1], True, "list"),
        "fcmp": Predicate("source", "f5", "<=", "p", 1.5, True, "float"),
        "fin": Predicate("source", "f5", "IN", "p", [1.5, 2.5], True, "float"),
        "farith": Predicate("source", "f5", ">", "p", 3.0, True, "float", arith=("*", "arc", 2.0)),
    }
    texts = {}
    for name, leaf in leaves.items():
        params = graph.Params()
        texts[name] = graph.cypher_filter(leaf, "t", "n1", schema, params)
        assert set(re.findall(r"\$(\w+)", texts[name])) == set(params.items)
        sql_binds = set(re.findall(r":(\w+)", predicate.sql(leaf, "t", "n1")))
        # a DOUBLE pivot binds under the SQL bind's name (an IN list one marker per
        # element, the session's `name__k`); every other pivot is a constant (A9)
        expected = {"fcmp": sql_binds, "fin": {"p__0", "p__1"}, "farith": sql_binds}.get(name, set())
        assert set(params.items) == expected, (name, texts[name])
    assert texts["cmp"] == "(t.N1_F1 > 1)" and texts["rev"] == "(1 > t.N1_F1)" and texts["str"] == "(t.N1_F2 = 'it\\'s')"
    assert texts["in"] == "(t.N1_F1 = 1 OR t.N1_F1 = 2)"
    assert texts["between"] == "(t.N1_F1 >= 1 AND t.N1_F1 <= 2)" and texts["null"] == "NOT (t.N1_F1 IS NULL)"
    assert texts["like"] == "t.N1_F2 STARTS WITH 'ab'" and texts["arith"] == "((t.N1_F1 + 2) >= 3)" and texts["list"] == "(t.N1_F3 = '[1]')"
    assert texts["fcmp"] == "(t.N1_F5 <= $p)" and texts["fin"] == "(t.N1_F5 = $p__0 OR t.N1_F5 = $p__1)" and texts["farith"] == "((t.N1_F5 * $arc) > $p)"
    # a string the grammar cannot spell has no native form
    assert not graph.expressible(Predicate("source", "f2", "=", "p", "a\nb", True, "string"), schema, "n1")
    for leaf in (Predicate("source", "f3", "CONTAINS", "p", 1, True, "list"), Predicate("source", "f4", "=", "p", True, True, "bool"), Predicate("source", "f2", "=", "p", "x", True, "string", ci=True)):
        assert not graph.expressible(leaf, schema, "n1")
    assert not graph.expressible(Predicate("source", "f1", "IN", "p", [], True, "int", sub=("n1", "f1", None, False)), schema, "n1")


# --- the offline openCypher translator ----------------------------------------------------------


def _translator_db():
    """A SQLite database with the views of a small schema, and its workspace."""
    import sqlite3

    from ganda.hana.mapping import graph as mapping_graph
    from tests.hana.opencypher_sqlite import Workspace

    schema = E.schema(
        (NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT), FieldSpec("f2", FieldType.STRING))), NodeTypeSpec("n2", (FieldSpec("f1", FieldType.STRING),))),
        (EdgeTypeSpec("e1", "n1", "n2", (FieldSpec("w", FieldType.INT),)), EdgeTypeSpec("e2", "n1", "n1", ())),
    )
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE n1 (id TEXT PRIMARY KEY, f1 INTEGER, f2 TEXT)")
    db.execute("CREATE TABLE n2 (id TEXT PRIMARY KEY, f1 TEXT)")
    db.execute("CREATE TABLE e1 (eid TEXT PRIMARY KEY, src_id TEXT, dst_id TEXT, w INTEGER)")
    db.execute("CREATE TABLE e2 (eid TEXT PRIMARY KEY, src_id TEXT, dst_id TEXT)")
    db.executemany("INSERT INTO n1 VALUES (?, ?, ?)", [("a", 1, "x"), ("b", 2, "it's"), ("c", None, None), ("d", -5, "yz")])
    db.executemany("INSERT INTO n2 VALUES (?, ?)", [("x", "x"), ("y", None)])
    db.executemany("INSERT INTO e1 VALUES (?, ?, ?, ?)", [("e0", "a", "x", 3), ("e1", "a", "x", None), ("e2", "b", "y", 7)])
    db.executemany("INSERT INTO e2 VALUES (?, ?, ?)", [("e3", "a", "a"), ("e4", "a", "b"), ("e5", "b", "d")])
    for step in mapping_graph.view_ddl(schema):
        db.execute(step.text)
    return db, Workspace.from_step(mapping_graph.workspace(schema))


def _run(db, ws, cypher: str, params: "dict | None" = None, binds: "dict | None" = None) -> list:
    from tests.hana.opencypher_sqlite import to_sql

    sql_text = to_sql(cypher, params or {}, ws)
    stmt, values = positional(sql_text, binds)
    cur = db.execute(stmt, values)
    names = [d[0].lower() for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def test_translator_patterns_and_semantics():
    import tests.hana.opencypher_sqlite as oc

    db, ws = _translator_db()
    assert sorted(r["id"] for r in _run(db, ws, "MATCH (a:n1) RETURN a.ID AS ID")) == ["a", "b", "c", "d"]
    assert sorted((r["id"], r["v"]) for r in _run(db, ws, "MATCH (a:n1)-[e:e1]->(b:n2) RETURN a.ID AS ID, b.ID AS V")) == [("a", "x"), ("a", "x"), ("b", "y")]
    # the hop in is the pattern reversed; a bare variable is the one already bound
    assert sorted(r["v"] for r in _run(db, ws, "MATCH (t1:n1)-[j1:e1]->(s:n2) WHERE s.ID = 'y' RETURN t1.ID AS V")) == ["b"]
    # an undirected edge over the self-loop: once (G7)
    loop = [r["v"] for r in _run(db, ws, "MATCH (a:n1)-[e:e2]-(b:n1) WHERE a.ID = 'a' RETURN b.ID AS V")]
    assert loop.count("a") == (2 if oc.SELF_LOOP_TWICE else 1) and loop.count("b") == 1
    # a second MATCH clause is the engine's refusal; within the one clause a relationship binds once
    with pytest.raises(oc.UnsupportedCypher):
        _run(db, ws, "MATCH (a:n1)-[e1:e2]->(b:n1) MATCH (b)-[e2:e2]->(c:n1) WHERE a.ID = 'a' AND c.ID = 'a' RETURN COUNT(c.ID) AS V")
    within = _run(db, ws, "MATCH (a:n1)-[e1:e2]->(b:n1), (b)-[e2:e2]->(c:n1) WHERE a.ID = 'a' AND c.ID = 'a' RETURN COUNT(c.ID) AS V")[0]["v"]
    assert within == (0 if oc.ISOMORPHIC_WITHIN_CLAUSE else 1)
    # a variable-length path repeats no relationship; unlabelled edges span every table
    assert _run(db, ws, "MATCH rp = (a:n1)-[:e2*1..2]->(b:n1) WHERE a.ID = 'a' AND b.ID = 'a' RETURN COUNT(b.ID) AS V")[0]["v"] == (1 if oc.TRAIL_PATHS else 2)
    assert _run(db, ws, "MATCH rp = (a:n1)-[*1..1]->(b) WHERE a.ID = 'b' RETURN COUNT(b.ID) AS V")[0]["v"] == 2
    assert _run(db, ws, "MATCH (a:n1)-[x1]->(y1), (y1)-[x2]->(b:n1) WHERE a.ID = 'a' AND b.ID = 'd' RETURN COUNT(b.ID) AS V")[0]["v"] == 1
    # aggregates: one row over no match, SUM 0 over no present value, implicit grouping
    assert [r["v"] for r in _run(db, ws, "MATCH (a:n1) WHERE a.N1_F2 = 'none' RETURN COUNT(a.ID) AS V")] == [0]
    assert [r["v"] for r in _run(db, ws, "MATCH (a:n1) WHERE a.N1_F2 = 'none' RETURN MAX(a.N1_F1) AS V")] == [None]
    assert [r["v"] for r in _run(db, ws, "MATCH (a:n1) WHERE a.ID = 'c' RETURN SUM(a.N1_F1) AS V")] == [0 if oc.SUM_EMPTY_ZERO else None]
    assert sorted((r["k"], r["v"]) for r in _run(db, ws, "MATCH (a:n1)-[e:e1]->(b:n2) RETURN a.ID AS K, COUNT(b.ID) AS V")) == [("a", 2), ("b", 1)]
    assert sorted(r["v"] for r in _run(db, ws, "MATCH (a:n1)-[e:e1]->(b:n2) RETURN DISTINCT b.ID AS V")) == ["x", "y"]


def test_translator_filters_ordering_and_parameters():
    import tests.hana.opencypher_sqlite as oc

    db, ws = _translator_db()
    ids = lambda cy, **kw: sorted(r["id"] for r in _run(db, ws, cy, **kw))  # noqa: E731
    assert ids("MATCH (a:n1) WHERE NOT (a.N1_F1 > 1) RETURN a.ID AS ID") == ["a", "d"]
    assert ids("MATCH (a:n1) WHERE a.N1_F1 > 100 OR a.N1_F2 IS NULL RETURN a.ID AS ID") == ["c"]
    assert ids("MATCH (a:n1) WHERE a.N1_F2 STARTS WITH 'i' RETURN a.ID AS ID") == ["b"]
    assert ids("MATCH (a:n1) WHERE a.N1_F2 ENDS WITH 'z' RETURN a.ID AS ID") == ["d"]
    assert ids("MATCH (a:n1) WHERE a.N1_F2 CONTAINS 't\\'s' RETURN a.ID AS ID") == ["b"]
    assert ids("MATCH (a:n1) WHERE (a.N1_F1 - $c) > $p RETURN a.ID AS ID", params={"c": ":c", "p": ":p"}, binds={"c": 1, "p": 0}) == ["b"]
    order = [r["id"] for r in _run(db, ws, "MATCH (a:n1) RETURN a.ID AS ID ORDER BY a.N1_F1 ASC, a.ID ASC")]
    assert order == (["c", "d", "a", "b"] if oc.NULLS_FIRST_ASC else ["d", "a", "b", "c"])
    assert [r["id"] for r in _run(db, ws, "MATCH (a:n1) RETURN a.ID AS ID ORDER BY a.N1_F1 ASC, a.ID ASC LIMIT 2 SKIP 1")] == order[1:3]
    assert [r["id"] for r in _run(db, ws, "MATCH (a:n1) RETURN a.ID AS ID ORDER BY a.N1_F1 DESC, a.ID ASC")] == (["b", "a", "d", "c"] if oc.NULLS_FIRST_ASC else ["c", "b", "a", "d"])
    assert not oc.translatable("SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE gw QUERY 'MATCH (a:n1) OPTIONAL MATCH (a)-[e:e1]->(b) RETURN a.ID AS ID')")
    assert oc.translatable("SELECT x.id FROM OPENCYPHER_TABLE(GRAPH WORKSPACE gw QUERY 'MATCH (a:n1) WHERE a.N1_F2 = ''it\\''s'' RETURN a.ID AS ID') x")
    rewritten = oc.rewrite_sql("SELECT z.id AS id FROM OPENCYPHER_TABLE(GRAPH WORKSPACE gw QUERY 'MATCH (a:n1) WHERE a.N1_F1 = $p RETURN a.ID AS ID' PARAMETERS ('p' = :p)) z", ws)
    assert "OPENCYPHER_TABLE" not in rewritten and ":p" in rewritten
    stmt, values = positional(rewritten, {"p": 2})
    assert [r[0] for r in db.execute(stmt, values).fetchall()] == ["b"]


@pytest.mark.parametrize("seed", range(1, 4))
def test_the_graph_model_round_trips_offline_without_rdflib(seed):
    """The graph setup, its state read back through the translated openCypher, and
    every rel / graph variant of every case against each other and the reference."""
    import dataclasses

    from ganda.core.oracle import run_cases
    from ganda.core.state_check import verify_state
    from tests.hana.offline import OfflineServer

    schema, instance, cases = _cases(seed)
    server = OfflineServer()
    sessions = {}
    for dm in ("rel", "graph"):
        s = server.fresh_session(E.database_name(f"g{seed}", dm))
        s.apply_setup(E.mappers[dm](instance))
        diff = verify_state(s, dm, instance)
        assert diff.ok, (dm, diff.problems[:3])
        sessions[dm] = s
    kept = []
    for c in cases:
        queries = {v: t for v, t in c.queries.items() if v.split(":", 1)[0] in sessions}
        if "rel" in queries and "graph" in queries:
            kept.append(dataclasses.replace(c, queries=queries, params={**c.params, "partial": True}))
    results = run_cases(kept, sessions, instance)
    bad = [r for r in results if not r.ok]
    assert len(kept) > 100 and not bad, [(r.finding_class, r.case.shape, r.errors, [str(m)[:200] for m in r.mismatches[:1]]) for r in bad[:3]]


# --- plan steering, the kept NOT, custom keys, the inheritance axis (2026-10-12) ---------------


def test_hint_profiles_close_a_statement_and_strip_back():
    from ganda.hana.concrete_syntax import hints

    text = "SELECT t.id AS id FROM n1 t ORDER BY t.f1 ASC NULLS FIRST, t.id ASC LIMIT 3 OFFSET 2"
    for name in hints.NAMES:
        h = hints.hinted(text, name)
        assert h.startswith(text + " WITH HINT (") and h.endswith(")") and hints.is_hinted(h) and hints.strip(h) == text
        assert all(x in h for x in hints.PROFILES[name])
    assert not hints.is_hinted(text) and hints.strip(text) == text
    assert hints.choose("abc") in hints.NAMES and hints.choose("abc") == hints.choose("abc")
    assert {"USE_HEX_PLAN", "USE_ESX_PLAN", "NO_USE_HEX_PLAN"} <= {h for hs in hints.PROFILES.values() for h in hs}


@pytest.mark.parametrize("seed", range(1, 4))
def test_every_case_carries_one_hint_profile_per_sql_data_model(seed):
    """A hinted twin: one profile per question, named in the tag, on a SQL statement
    only (never a SPARQL text, never the collection query), the clause closing it."""
    from ganda.hana.concrete_syntax import hints, tx as _tx

    _schema, _instance, cases = _cases(seed)
    seen = 0
    for c in cases:
        profiles: dict = {}
        for v, t in c.queries.items():
            last = _tx.parts(t)[-1] if _tx.is_script(t) else t
            dm, _, tag = v.partition(":")
            # a traversal's base variant may be the hinted syntax (drawn like any other):
            # the case's `concrete_syntax` names it then
            if c.generator_name == "ir_traversal":
                tag = "+".join([tag, c.params.get("concrete_syntax", {}).get(v, "")])
            named = sorted({p.removeprefix("hint_") for p in tag.split("+") if p in hints.NAMES or p.startswith("hint_")})
            assert hints.is_hinted(last) == (len(named) == 1), v
            if not named:
                continue
            seen += 1
            profiles.setdefault(dm, set()).add(named[0])
            plain = hints.strip(last)
            assert not plain.lstrip().startswith("PREFIX ")
            if dm == "doc":
                assert plain.startswith("WITH raw_v_") or plain.startswith("WITH v_"), v
            if dm == "rdf":
                assert plain.startswith("SELECT * FROM SPARQL_TABLE("), v
        assert all(len(ps) == 1 for ps in profiles.values()) and len(set().union(*profiles.values()) if profiles else set()) <= 1, (c.shape, profiles)
        if c.generator_name == "ir_select" and profiles:
            assert c.params["hint"] == next(iter(next(iter(profiles.values()))))
            assert not hints.is_hinted(_tx.parts(c.queries["rel"])[-1] if _tx.is_script(c.queries["rel"]) else c.queries["rel"])
    assert seen > 100


def test_the_doc_neg_position_keeps_the_nots_the_store_can_say():
    from ganda.hana.concrete_syntax.document import doc_filter_ok, doc_pred, has_not

    leaf = Predicate("source", "f1", "=", "p", 1, True, "int")
    tree = Compound("source", "NOT", (Compound("source", "OR", (leaf, Predicate("source", "f2", "IS NULL", "", None, True, "string"))),))
    assert has_not(tree) and not has_not(leaf) and not has_not(None)
    assert doc_pred(tree) == '($."f1" <> CAST(:p AS BIGINT) AND $."f2" IS NOT NULL)'
    assert doc_pred(tree, keep_not=True) == 'NOT (($."f1" = CAST(:p AS BIGINT) OR $."f2" IS NULL))'
    member = Compound("source", "NOT", (Predicate("source", "f3", "CONTAINS", "p", 5, True, "list"),))
    assert not doc_filter_ok(member) and doc_filter_ok(member, keep_not=True)
    assert doc_pred(member, keep_not=True) == 'NOT (FOR ANY "e" IN $."f3" SATISFIES "e" = CAST(:p AS BIGINT) END)'
    like = Compound("source", "NOT", (Predicate("source", "f2", "LIKE", "p", "a%", True, "string"),))
    assert doc_pred(like) == "$.\"f2\" NOT LIKE 'a%'" and doc_pred(like, keep_not=True) == "NOT ($.\"f2\" LIKE 'a%')"
    # the position in the cases: a collection query with a NOT kept, only where the tree has one
    negs = [(c, v, t) for seed in (1, 2, 3) for c in _cases(seed)[2] for v, t in c.queries.items() if v.startswith("doc:") and "neg" in v.split(":", 1)[1].split("+")]
    assert negs
    for c, v, t in negs:
        assert "NOT (" in t and not t.startswith("WITH ") and ('$."' in t or '"l"."' in t), v


def test_the_custom_key_pool_orders_alike_in_sql_and_sparql():
    """The axis is on because SQL's UTF-16 code-unit order and SPARQL's code-point order
    part only for an astral key against one in U+E000..U+FFFF, which no key style
    draws (`data_generator._custom_key`); the probe's CUSTOM_KEYS cover every style."""
    from ganda.core.data_generator import _KEY_PUNCT_BASES, _KEY_UNICODE_BASES
    from ganda.hana.dialect import is_valid_key
    from ganda.hana.scripts.probe import CUSTOM_KEYS

    keys = [b + "7" for b in _KEY_PUNCT_BASES + _KEY_UNICODE_BASES] + CUSTOM_KEYS
    assert not any(0xE000 <= ord(ch) <= 0xFFFF for k in keys for ch in k)
    assert any(ord(ch) > 0xFFFF for k in keys for ch in k)
    assert sorted(keys, key=lambda k: k.encode("utf-16-be", "surrogatepass")) == sorted(keys, key=lambda k: [ord(c) for c in k])
    assert all(is_valid_key(k) for k in keys)
    assert "custom_key_probability" not in UNSUPPORTED_AXES and "inheritance_probability" not in UNSUPPORTED_AXES


def _poly(seed: int):
    cfg = CFG.with_(inheritance_probability=1.0, custom_key_probability=0.5)
    schema, instance, rng = build(seed, cfg, E)
    return cfg, schema, instance, rng


@pytest.mark.parametrize("seed", range(1, 4))
def test_the_base_class_is_a_union_view_a_second_class_triple_and_an_unlabelled_match(seed):
    from ganda.hana.mapping.common import Sparql, Sql

    cfg, schema, instance, rng = _poly(seed)
    assert schema.base is not None and schema.base.name == "N0"
    rel = E.mappers["rel"](instance)
    texts = [s.text for s in rel if isinstance(s, Sql)]
    views = [t for t in texts if t.startswith("CREATE VIEW N0")]
    assert views[0].startswith("CREATE VIEW N0 AS SELECT id, b1") and views[0].count(" UNION ALL ") == len(schema.node_types) - 1
    assert len(views) == 1 + sum(1 for f in schema.base.fields if f.type is FieldType.LIST)
    for ix in schema.indexes:
        if ix.table == "N0" and ix.applies_to("rel"):
            assert sum(1 for t in texts if t.startswith(("CREATE INDEX ix_N0_", "CREATE UNIQUE INDEX ix_N0_")) and f"_{ix.field}_" in t) == len(schema.node_types)
    assert not any(" ON N0 " in t for t in texts if t.startswith("CREATE"))
    graph = [s.text for s in E.mappers["graph"](instance) if isinstance(s, Sql)]
    assert any(t.startswith("CREATE VIEW N0 ") for t in graph) and not any("gv_N0" in t for t in graph)
    doc = [s.text for s in E.mappers["doc"](instance) if isinstance(s, Sql)]
    assert not any("N0" in t and t.startswith("CREATE COLLECTION") for t in doc) and not any(" ON N0(" in t for t in doc)
    from ganda.hana.mapping.common import Transaction

    def texts_of(steps):
        for s in steps:
            if isinstance(s, Transaction):
                yield from texts_of(s.steps)
            elif isinstance(s, Sparql):
                yield s.text

    rdf = " ".join(texts_of(E.mappers["rdf"](instance)))
    assert " a c:N0" in rdf and rdf.count(" a c:N0") >= len(schema.node_types)
    cases = all_cases(rng, schema, instance, cfg=cfg)
    on_base = [c for c in cases if c.generator_name == "ir_select" and c.params.get("table") == "N0"]
    assert on_base
    for c in on_base:
        assert "N0" in c.queries["rel"] and "?n a c:N0" in c.queries["rdf"]
        g = c.queries["graph"]
        assert ("g_N0 AS (SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE gw QUERY 'MATCH (a) RETURN a.ID AS ID, a.B1 AS B1" in g) or "MATCH (t)" in g, g[:200]
        assert "(t:N0)" not in g and "(a:N0)" not in g and "(m:N0)" not in g
        # the doc base is the views' union (none where the views cannot ask the filter:
        # a whole-list comparison, a membership under OR or NOT)
        d = c.queries.get("doc")
        if d is not None:
            assert d.startswith("WITH ") and "v_N0 AS (SELECT id, b1" in d
            assert d.index("v_N0 AS (") > max(d.index(f", v_{nt.name} AS (") for nt in schema.node_types)
        assert "doc:neg" not in c.queries and not any(v.startswith("doc:") and v.endswith("+neg") for v in c.queries)


def test_a_write_on_the_base_class_addresses_every_subclass():
    cfg, schema, instance, rng = _poly(1)
    wr = rng.for_generator("ir_write")
    cur = instance
    seen = set()
    for step in range(60):
        drawn = sample_write(wr, schema, cur, step)
        if drawn is None:
            continue
        q, rw, syntaxes = drawn
        after = apply(q, cur)
        if not q.edge and q.table == "N0":
            for dm in E.data_models:
                st = E.write_syntax.render(q, syntaxes[dm][0], rw, dm, cur, after)
                writes = [s for s in st.steps if s.text.startswith(("UPDATE ", "DELETE ", "MERGE "))]
                if dm == "rdf":
                    # the write names the base class (a read-back by captured ids need not)
                    assert any("?n a c:N0" in s.text for s in st.steps), st.display()
                    continue
                assert not any(re.match(r"(UPDATE|DELETE FROM|MERGE INTO) N0\b", s.text) for s in writes), st.display()
                tables = {re.match(r"(?:UPDATE|DELETE FROM|MERGE INTO) (\w+)", s.text).group(1) for s in writes}
                assert tables >= {nt.name for nt in schema.node_types}, (dm, st.display())
                if q.returning == "count" or q.op == "delete":
                    counted = [s for s in st.steps if s.role in ("count_part", "each_count")]
                    assert len(counted) == len(schema.node_types) and st.main is counted[0], st.display()
                if q.returning == "after":
                    assert [s for s in st.steps if s.role == "rows_captured"] and any(s.role in ("capture", "capture_more") for s in st.steps)
            seen.add(q.op)
        cur = after
    assert seen


def test_translator_unlabelled_vertex_projects_the_shared_attributes():
    import sqlite3

    from tests.hana.opencypher_sqlite import Workspace, to_sql

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE gv_n1 (id TEXT, b1 INTEGER, n1_f1 INTEGER)")
    db.execute("CREATE TABLE gv_n2 (id TEXT, b1 INTEGER, n2_f1 TEXT)")
    db.executemany("INSERT INTO gv_n1 VALUES (?, ?, ?)", [("a", 1, 5), ("b", None, 6)])
    db.executemany("INSERT INTO gv_n2 VALUES (?, ?, ?)", [("x", 2, "s")])
    vertices = {"n1": {"table": "gv_n1", "label": "n1", "key": "id", "columns": [["id", "ID"], ["b1", "B1"], ["f1", "N1_F1"]]}, "n2": {"table": "gv_n2", "label": "n2", "key": "id", "columns": [["id", "ID"], ["b1", "B1"], ["f1", "N2_F1"]]}}
    ws = Workspace("gw", vertices, {})
    rows = db.execute(to_sql("MATCH (a) WHERE NOT (a.B1 IS NULL) RETURN a.ID AS ID, a.B1 AS B1", {}, ws)).fetchall()
    assert sorted(rows) == [("a", 1), ("x", 2)]
    joined = db.execute(to_sql("MATCH (t), (m) WHERE (m.B1 > t.B1) RETURN t.ID AS ID, m.ID AS V", {}, ws)).fetchall()
    assert sorted(joined) == [("a", "x")]
    assert db.execute(to_sql("MATCH (a) RETURN COUNT(a.ID) AS V", {}, ws)).fetchall() == [(3,)]



def test_the_mean_is_hana_sql_avg():
    """AVG over BIGINT is DECIMAL(25,6): the quotient truncated to six places (R20); the
    B13 guard withholds an rdf average the triple store's double arithmetic cannot
    answer as SQL does."""
    from ganda.hana import known_bugs

    assert mean([7] + [0] * 127) == 0.054687 and mean([-7] + [0] * 127) == -0.054687 and mean([2, 2, 0]) == 1.333333
    assert mean([1, 2]) == 1.5 and mean([2, 4]) == 3 and isinstance(mean([2, 4]), int) and mean([0.5, 1.0]) == 0.75
    assert known_bugs.select_known is not None
