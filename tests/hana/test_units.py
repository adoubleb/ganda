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
        # every NATIVE graph variant of a case is its own text -- except that two
        # `literal` variants may coincide (a rewrite of `IN [x]` as `= x` inlines the same
        # constant); the views family inherits rel's own coincidences (join / scalar on a
        # reach question)
        native = "g_" not in t and ("literal" not in v or v == "graph:literal")
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
    assert graph.literal(0.1) == "0.1" and graph.literal(2**63 - 1) == "9223372036854775807" and graph.literal(-2.0, "float") == "-2.0"
    assert graph.literal([1, 2]) == "'[1,2]'"
    for bad in ("a\nb", 1e-05, float("inf"), True, None):
        with pytest.raises(graph.Unsupported):
            graph.literal(bad)


def test_cypher_filter_mirrors_the_sql_binds_and_refuses_what_has_no_form():
    from ganda.hana.concrete_syntax import graph, predicate

    schema = E.schema((NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT), FieldSpec("f2", FieldType.STRING), FieldSpec("f3", FieldType.LIST), FieldSpec("f4", FieldType.BOOL))),), ())
    leaves = {
        "cmp": Predicate("source", "f1", ">", "p", 1, True, "int"),
        "rev": Predicate("source", "f1", "<", "p", 1, True, "int", rev=True),
        "in": Predicate("source", "f1", "IN", "p", [1, 2], True, "int"),
        "between": Predicate("source", "f1", "BETWEEN", "p", [1, 2], True, "int"),
        "null": Predicate("source", "f1", "IS NOT NULL", "", None, True, "int"),
        "like": Predicate("source", "f2", "LIKE", "p", "ab%", True, "string"),
        "arith": Predicate("source", "f1", ">=", "p", 3, True, "int", arith=("+", "arc", 2)),
        "list": Predicate("source", "f3", "=", "p", [1], True, "list"),
    }
    texts = {}
    for name, leaf in leaves.items():
        params = graph.Params()
        texts[name] = graph.cypher_filter(leaf, "t", "n1", schema, params)
        assert set(re.findall(r"\$(\w+)", texts[name])) == set(params.items)
        sql_binds = set(re.findall(r":(\w+)", predicate.sql(leaf, "t", "n1")))
        # the same names as the SQL binds, but an IN list one marker per element (the
        # session's `name__k`) and a LIKE constant inlined (no `$` at all)
        expected = {"in": {"p__0", "p__1"}, "like": set()}.get(name, sql_binds)
        assert set(params.items) == expected, (name, texts[name])
        assert graph.binds_parameters(leaf) == bool(expected)
    assert texts["cmp"] == "(t.N1_F1 > $p)" and texts["rev"] == "($p > t.N1_F1)"
    assert texts["in"] == "(t.N1_F1 = $p__0 OR t.N1_F1 = $p__1)"
    assert texts["between"] == "(t.N1_F1 >= $pa AND t.N1_F1 <= $pb)" and texts["null"] == "NOT (t.N1_F1 IS NULL)"
    assert texts["like"] == "t.N1_F2 STARTS WITH 'ab'" and texts["arith"] == "((t.N1_F1 + $arc) >= $p)" and texts["list"] == "(t.N1_F3 = $p__json)"
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
