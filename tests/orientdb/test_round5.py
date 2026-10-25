"""Round 5: reversed operands, arithmetic and literal leaves, list operators, the
transaction door, the BFS distances and their guards."""
import dataclasses
import math
import random

from ganda.core.query.ir.select import TxWrite
from tests.orientdb.helpers import GeneratorConfig, build
from ganda.orientdb.known_bugs.guards import _leaf_served, list_ops_known, o31_known
from ganda.core.data.model import EdgeTypeSpec, FieldSpec, FieldType, IndexSpec, Instance, NodeRow, NodeTypeSpec, Schema
from ganda.orientdb.engine.connection import is_script, unwrap_script
from ganda.orientdb.concrete_syntax.query.support import bfs_distances
from ganda.orientdb.concrete_syntax.query.support import tx_rows as _tx_rows
from ganda.orientdb.concrete_syntax.query.predicate import render_predicate as render
from ganda.core.query.ir.predicates import Compound, Predicate
from ganda.orientdb.engine.semantics import apply_arith
from ganda.orientdb.concrete_syntax.query.predicate_sampling.predicate_steering import evaluate
from ganda.orientdb.concrete_syntax.query.predicate_sampling.forms import _arithmetic, leaf_form
from ganda.core.query.generators.registry import all_cases
from ganda.orientdb.concrete_syntax.query.predicate_sampling.predicate_steering import predicate_hit


from ganda.orientdb import Schema  # noqa: E402,F811 -- bound to the OrientDB provider

def _schema(indexes=(), ftype=FieldType.INT):
    return Schema(
        node_types=(NodeTypeSpec("n1", (FieldSpec("f1", ftype), FieldSpec("f2", FieldType.STRING))),),
        edge_types=(EdgeTypeSpec("e1", "n1", "n1", ()),),
        indexes=tuple(indexes),
    )


class _Roll:
    """A random stream whose `random()` answers `roll` (then 0.99), `choice` the first."""

    def __init__(self, *rolls):
        self.rolls = list(rolls)

    def random(self):
        return self.rolls.pop(0) if self.rolls else 0.99

    def choice(self, xs):
        return xs[0]


class _Pick:
    def __init__(self, pick):
        self.pick = pick

    def choice(self, xs):
        return self.pick


def test_leaf_forms_render_and_evaluate_as_drawn():
    p = Predicate("source", "f1", "<", "p", 5, ftype="int")
    rp = leaf_form(_Roll(0.05), p)
    assert rp.rev and render(rp) == ":p > f1" and render(p) == "f1 < :p"
    assert rp.bind_vars == p.bind_vars and rp.signature == p.signature
    # IN and the null tests keep their operand order
    assert not leaf_form(_Roll(0.05), Predicate("source", "f1", "IN", "p", [5])).rev
    assert render(leaf_form(_Roll(0.05), Predicate("source", "f1", "HAS", "p", 2, ftype="list"))) == ":p IN f1"
    rows = [{"f1": 4}, {"f1": 5}, {"f1": 6}, {}, {"f1": None}]
    assert [evaluate(r, p) for r in rows] == [evaluate(r, rp) for r in rows]
    assert Predicate.from_dict(rp.to_dict()) == rp
    # a literal: in the text, not bound
    lp = leaf_form(_Roll(0.25), Predicate("source", "f1", "BETWEEN", "p", [1, 5], ftype="int"))
    assert lp.literal and render(lp) == "f1 BETWEEN 1 AND 5" and lp.bind_vars == {}
    assert Predicate.from_dict(lp.to_dict()) == lp
    # never a string, nor a double a FLOAT would not hold (O12), nor a relationship attribute
    assert not leaf_form(_Roll(0.25), Predicate("source", "f2", "=", "p", "a", ftype="string")).literal
    assert not leaf_form(_Roll(0.25), Predicate("source", "f1", "=", "p", 776.417, ftype="float")).literal
    assert leaf_form(_Roll(0.25), Predicate("source", "f1", "=", "p", 1.5, ftype="float")).literal
    assert not leaf_form(_Roll(0.25), Predicate("edge", "w", "=", "p", 1, ftype="int")).literal
    # a map entry keyed by a parameter
    kp = leaf_form(_Roll(0.1), Predicate("source", "m", "=", "p", 2, "int", False, "k2"))
    assert render(kp) == "m[:pk] = :p" and kp.bind_vars == {"pk": "k2", "p": 2}


def test_arithmetic_form_puts_the_field_under_arithmetic():
    rng = random.Random(1)
    p = Predicate("source", "f1", ">", "p", 5, ftype="int")
    ap = _arithmetic(rng, p)
    assert ap.arith is not None and not ap.plain and ap.arith.bind == "pc"
    assert render(ap).startswith("(f1 ") and ":pc" in render(ap)
    rows = [{"f1": 1}, {"f1": 7}, {}]
    # the constant shifted alike: the same records
    assert [evaluate(r, p) for r in rows] == [evaluate(r, ap) for r in rows]
    # a ci column, IN, a string constant on a number, a relationship attribute: none
    assert _arithmetic(rng, Predicate("source", "f2", "=", "p", "a", ftype="string", case_insensitive=True)) is None
    assert _arithmetic(rng, Predicate("source", "f1", "IN", "p", [5], ftype="int")) is None
    assert _arithmetic(rng, Predicate("source", "f1", ">", "p", "a", ftype="int")) is None
    assert _arithmetic(rng, Predicate("edge", "w", ">", "p", 5, ftype="int")) is None
    # an integer constant the arithmetic takes past the long range is no parameter
    edge = Predicate("source", "f1", ">=", "p", -(2**63), ftype="int")
    assert all(_arithmetic(random.Random(s), edge) is None or -(2**63) <= _arithmetic(random.Random(s), edge).constant < 2**63 for s in range(20))
    # a constant the arithmetic takes past the double range cannot travel as JSON
    huge = Predicate("source", "f1", ">", "p", 1e308, ftype="float")
    assert all(_arithmetic(random.Random(s), huge) is None or math.isfinite(_arithmetic(random.Random(s), huge).constant) for s in range(20))
    # dates shift by milliseconds, decimals stay exact
    assert apply_arith("2024-01-01 00:00:00", "+", 86400000, "datetime") == "2024-01-02 00:00:00"
    assert apply_arith("2024-01-01 00:00:00", "-", 1000, "date") == "2023-12-31 23:59:59"
    assert apply_arith(0.1, "+", 1, "decimal") == 1.1 and apply_arith(0.3, "*", 3, "decimal") == 0.9


def test_list_operators_predicate_logic():
    assert predicate_hit(True, [1, 2], "HAS ANY", [2, 9]) and not predicate_hit(True, [1, 2], "HAS ANY", [9])
    assert predicate_hit(True, [1, 2], "HAS ALL", [2, 1]) and not predicate_hit(True, [1, 2], "HAS ALL", [2, 3])
    assert predicate_hit(True, [], "HAS ALL", []) and not predicate_hit(False, None, "HAS ALL", [])


def test_list_ops_guard_names_the_boxed_faces():
    sch = Schema(node_types=(NodeTypeSpec("n1", (FieldSpec("f1", FieldType.LIST),)),), edge_types=(), indexes=(IndexSpec("n1", "f1", data_model="rel", kind="NOTUNIQUE"),))
    boxed = Predicate("source", "f1", "HAS ANY", "p", [1, 2], ftype="list")
    assert list_ops_known(sch, "n1", boxed, "doc") == "O7"  # a scan
    assert list_ops_known(sch, "n1", boxed, "rel") is None  # the list index converts the keys
    assert list_ops_known(sch, "n1", boxed, "rel", servable=False) == "O7"  # a bracket filter
    mixed = Predicate("source", "f1", "HAS ANY", "p", [1, 2**40], ftype="list")
    assert list_ops_known(sch, "n1", mixed, "rel") == "O10" and list_ops_known(sch, "n1", mixed, "doc") == "O7"
    assert list_ops_known(sch, "n1", Predicate("source", "f1", "HAS ALL", "p", [1], ftype="list"), "rel") == "O7"
    assert list_ops_known(sch, "n1", Predicate("source", "f1", "HAS ALL", "p", [2**40], ftype="list"), "rel") is None


def test_reversed_and_arithmetic_leaves_are_never_index_served():
    sch = _schema([IndexSpec("n1", "f1", data_model="both", kind="NOTUNIQUE")])
    plain = Predicate("source", "f1", "<", "p", 5, ftype="int")
    assert _leaf_served(sch, "n1", plain, "doc")
    assert not _leaf_served(sch, "n1", dataclasses.replace(plain, rev=True), "doc")
    ap = _arithmetic(random.Random(1), plain)
    assert not _leaf_served(sch, "n1", ap, "doc")



def test_o31_is_a_one_sided_range_after_a_write_of_the_key():
    sch = _schema([IndexSpec("n1", "f1", data_model="graph", kind="NOTUNIQUE")])
    p = Predicate("source", "f1", "<", "p", 0, ftype="int")
    assert o31_known(sch, "n1", p, "graph", "update", {"f1"}) == "O31"
    assert o31_known(sch, "n1", p, "graph", "insert", {"f1"}) == "O31"
    assert o31_known(sch, "n1", p, "graph", "update", {"f2"}) is None  # another column written
    assert o31_known(sch, "n1", p, "graph", "delete", set()) is None
    assert o31_known(sch, "n1", p, "doc", "update", {"f1"}) is None  # no index there
    assert o31_known(sch, "n1", Predicate("source", "f1", "=", "p", 0, ftype="int"), "graph", "update", {"f1"}) is None
    assert o31_known(sch, "n1", Predicate("source", "f1", "BETWEEN", "p", [0, 5], ftype="int"), "graph", "update", {"f1"}) is None
    hsch = _schema([IndexSpec("n1", "f1", data_model="graph", kind="NOTUNIQUE_HASH_INDEX")])
    assert o31_known(hsch, "n1", p, "graph", "update", {"f1"}) is None  # a hash index scans a range


def test_tx_rows_apply_the_write():
    sch = _schema()
    rows = [NodeRow("n1", 0, {"f1": 1}), NodeRow("n1", 1, {"f1": 2, "f2": "x"})]
    inst = Instance(sch, rows, [])
    base = TxWrite("n1", "update", "f1", "n1/r1")
    assert [n.record_id for n in _tx_rows(inst, dataclasses.replace(base, write="delete"))] == ["n1/r0"]
    ins = _tx_rows(inst, dataclasses.replace(base, write="insert", tv=7))
    assert ins[-1].record_id == "n1/tx0" and ins[-1].values == {"f1": 7}
    upd = _tx_rows(inst, dataclasses.replace(base, write="update", tv=9))
    assert upd[1].values == {"f1": 9, "f2": "x"} and rows[1].values == {"f1": 2, "f2": "x"}
    rem = _tx_rows(inst, dataclasses.replace(base, write="update", tv=None, remove=True))
    assert rem[1].values == {"f2": "x"}
    assert is_script("BEGIN; UPDATE n1 SET f1 = 1; LET $r = (SELECT id FROM n1); ROLLBACK; RETURN $r") and not is_script("SELECT FROM n1")
    assert unwrap_script([{"value": [{"id": "a", "@rid": "#1:0"}]}]) == [{"id": "a"}]


def test_bfs_distances():
    sch = _schema()
    rows = [NodeRow("n1", i, {}) for i in range(4)]
    from ganda.core.data.model import EdgeRow

    edges = [EdgeRow("e1", 0, "n1/r0", "n1/r1", {}), EdgeRow("e1", 1, "n1/r1", "n1/r2", {}), EdgeRow("e1", 2, "n1/r0", "n1/r0", {})]
    inst = Instance(sch, rows, edges)
    assert bfs_distances(inst, "n1/r0", "OUT", "e1") == {"n1/r0": 0, "n1/r1": 1, "n1/r2": 2}
    assert bfs_distances(inst, "n1/r2", "OUT", "e1").get("n1/r0") is None
    assert bfs_distances(inst, "n1/r2", "IN", "e1").get("n1/r0") == 2 and bfs_distances(inst, "n1/r2", "BOTH", None).get("n1/r0") == 2
    assert bfs_distances(inst, "n1/r0", "BOTH", None).get("n1/r3") is None


def test_order_form_withholds_the_asc_walk_of_a_composite_index_with_a_null_component():
    from ganda.orientdb.known_bugs.guards import select_shape_known
    from ganda.core.data.model import EdgeTypeSpec
    from ganda.core.query.ir.select import SelectQuery
    from ganda.orientdb.concrete_syntax.query.select import SelectSyntax

    sch = Schema(
        node_types=(NodeTypeSpec("n1", (FieldSpec("f1", FieldType.INT), FieldSpec("f2", FieldType.INT))),),
        edge_types=(EdgeTypeSpec("e1", "n1", "n1", ()),),
        indexes=(IndexSpec("n1", "f1", data_model="rel", kind="NOTUNIQUE", extra_fields=("f2",), ignore_nulls=True),),
    )
    rows = [NodeRow("n1", 0, {"f1": -5}), NodeRow("n1", 1, {"f1": 1, "f2": 10}), NodeRow("n1", 2, {"f1": 7, "f2": 70})]
    inst = Instance(sch, rows, [])
    order = SelectSyntax(form="order")
    for fn in ("max", "min"):
        q = SelectQuery("n1", None, "agg", ("f1",), fn=fn)
        assert select_shape_known(q, order, "rel", sch, inst, [-5, 1, 7]) == "O18", fn
        assert select_shape_known(q, order, "doc", sch, inst, [-5, 1, 7]) is None and select_shape_known(q, order, "graph", sch, inst, [-5, 1, 7]) is None
    # every component present: the ASC walk is complete
    full = Instance(sch, [dataclasses.replace(rows[0], values={"f1": -5, "f2": 0})] + rows[1:], [])
    assert select_shape_known(SelectQuery("n1", None, "agg", ("f1",), fn="min"), order, "rel", sch, full, [-5, 1, 7]) is None


def test_list_ops_guard_scans_under_not_or_and_on_the_scan_rewrite():
    sch = Schema(node_types=(NodeTypeSpec("n1", (FieldSpec("f1", FieldType.LIST), FieldSpec("f2", FieldType.INT))),), edge_types=(), indexes=(IndexSpec("n1", "f1", data_model="both", kind="NOTUNIQUE"),))
    boxed = Predicate("source", "f1", "HAS ANY", "p_0", [1, 2], ftype="list")
    other = Predicate("source", "f2", "<=", "p_1", 0, ftype="int")
    assert list_ops_known(sch, "n1", boxed, "graph") is None
    assert list_ops_known(sch, "n1", Compound("source", "AND", (boxed, other)), "graph") is None
    assert list_ops_known(sch, "n1", Compound("source", "NOT", (Compound("source", "AND", (boxed, other)),)), "graph") == "O7"
    assert list_ops_known(sch, "n1", Compound("source", "OR", (boxed, other)), "graph") == "O7"
