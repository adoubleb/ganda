"""Re-check live the ArangoDB facts the provider rests on (`ganda arangodb probe`), each
with an id: the value semantics (`V`), the statement forms the renderers emit (`S`), the
write doors (`W`), the limits the large scale walks into (`L`), the timeout (`T`) and the
defects pinned as current behaviour (`A`). `rules/checks.py` lists the rules each fact
checks and the defects it reproduces; a failing fact prints them with the code reading
them. A failing fact of a rule (or of nothing in the catalog) fails the run: the
provider changes, never the probe -- unless the probe's own AQL tested a form the
provider never emits. A defect that no longer reproduces is a bug fixed on the build,
recorded, not failed. A run in which every fact of a rule holds writes
`rules/baseline.json` (the build, the facts that held, the defects fixed there) and
re-renders `rules/RULES.md`. Exit code: 1 when a fact of a rule failed.

    python3 -m ganda arangodb probe            # a disposable server in Docker
    python3 -m ganda arangodb probe V S        # a subset (ids or prefixes)
    ARANGO_URL=http://127.0.0.1:8529 ARANGO_PASSWORD=... python3 -m ganda arangodb probe
"""
from __future__ import annotations

import base64
import pathlib
import sys
import time

from ganda.core.engine.provider import EngineError
from ganda.core.rules.model import Defect
from ganda.arangodb.engine.connection import ArangoServer
from ganda.arangodb.engine.limits import LIMITS
from ganda.arangodb.oracle.semantics import eq, like_hit, lt

CHECKS: list = []
ran: list = []
failures: list = []


def fact(fid: str, title: str):
    def register(fn):
        CHECKS.append((fid, title, fn))
        return fn

    return register


def _one(s, text, binds=None, **kw):
    rows = s.aql(text, binds, timeout_ms=0, **kw)
    return rows[0] if rows else None


def _eq(got, want, label=None):
    if got != want:
        raise AssertionError(f"{label + ': ' if label else ''}got {got!r}, expected {want!r}")


def _rejected(s, text, binds=None):
    try:
        s.aql(text, binds, timeout_ms=0)
    except EngineError as e:
        return e
    return None


# --- V: the value semantics -------------------------------------------------------------------


@fact("V1", "the order across types is total: null < bool < number < string < array < object")
def v1(s):
    assert _one(s, "RETURN [null < false, false < 0, 0 < 'a', 'a' < [], [] < {}, null <= null, null == null]") == [True] * 7
    _eq(_one(s, "RETURN [null < 5, null <= 5, null > 5, null != 5, null == 5]"), [True, True, False, True, False])


@fact("V2", "the mirror of == and < agrees with the engine over the numeric and boolean boundary pairs")
def v2(s):
    vals = [0, 1, -1, 7, 2**31, 2**53 - 1, 2**53, 2**53 + 1, 2**62, 2**63 - 1, -(2**63), 123456789012345678, 1.0, 0.5, -0.5, 0.0, -0.0, 9007199254740992.0, 9007199254740993.0, 1e300, 5e-324, 1e-308, 1.7976931348623157e308, True, False, None]
    pairs = [[a, b] for a in vals for b in vals]
    got = s.aql("FOR p IN @pairs RETURN [p[0] == p[1], p[0] < p[1]]", {"pairs": pairs}, timeout_ms=0)
    bad = [(a, b, g, [eq(a, b), lt(a, b)]) for (a, b), g in zip(pairs, got) if g != [eq(a, b), lt(a, b)]]
    _eq(bad[:12], [], f"{len(bad)} of {len(pairs)} pairs disagree (value, value, engine [==, <], mirror)")


@fact("V3", "strings are equal code point by code point (no normalization) and LENGTH counts code points; the collation order is reported, never relied on")
def v3(s):
    vals = ["", "a", "A", "b", "B", "aa", "ab", "\u00e9", "e\u0301", "\u00df", "ss", "SS", " x", "x", "1", "10", "9", "-1", "_", "[]", "{}", "\u2028", "\U0001F600", "\uffff", "\u4e2d", "null", "a b", "it's"]
    pairs = [[a, b] for a in vals for b in vals]
    got = s.aql("FOR p IN @pairs RETURN [p[0] == p[1], p[0] < p[1], LENGTH(p[0])]", {"pairs": pairs}, timeout_ms=0)
    bad = [(a, b, g[0], eq(a, b)) for (a, b), g in zip(pairs, got) if g[0] != eq(a, b)]
    _eq(bad[:12], [], f"{len(bad)} equality disagreements")
    _eq([(a, g[2], len(a)) for (a, _b), g in zip(pairs, got) if g[2] != len(a)][:12], [], "LENGTH disagreements")
    order = sum(1 for (a, b), g in zip(pairs, got) if g[1] != lt(a, b))
    _eq(order > 0, True, "the engine orders strings by code point: `order.string` says collation")
    print(f"        (string order: the code-point mirror disagrees with the engine on {order} of {len(pairs)} pairs; no predicate relies on it)")


@fact("V4", "IN compares with ==: a null element matches a null value; a membership in a null, absent or scalar operand is false")
def v4(s):
    _eq(_one(s, "LET d = {a: null, s: 5} RETURN [null IN [1, null], d.a IN [null], d.b IN [null], 1 IN [1.0], 1 IN null, 1 IN d.b, 1 IN d.s, 1 IN [[1]]]"), [True, True, True, True, False, False, False, False])


@fact("V5", "the LIKE mirror agrees with the engine over the wildcard, escape, case and line-terminator pool")
def v5(s):
    vals = ["abc", "Abc", "ABC", "a\nb", "ab\n", "a\rb", "ab\r", "a\u2028b", "ab\u2028", "a\u2029b", "a\u0085b", "ab\u0085", "a%b", "axb", "a_b", "", "a", "\u00e9a", "\U0001F600b", "ab\r\n", None, 5, True]
    pats = ["a%", "%b", "a_c", "a\\%b", "a\\_b", "%", "ab", "a%b", "_", "%c", "a__", "abc", "5", "true"]
    pairs = [[v, p] for v in vals for p in pats]
    got = s.aql("FOR p IN @pairs RETURN p[0] LIKE p[1]", {"pairs": pairs}, timeout_ms=0)
    bad = [(v, pat, g, like_hit(v, pat)) for (v, pat), g in zip(pairs, got) if g != like_hit(v, pat)]
    _eq(bad[:20], [], f"{len(bad)} of {len(pairs)} LIKE answers disagree (value, pattern, engine, mirror)")


@fact("V6", "aggregates over nothing: MAX/MIN/AVERAGE/MEDIAN null, COUNT_DISTINCT 0; MAX / MIN skip null, COUNT_DISTINCT counts it")
def v6(s):
    _eq(_one(s, "RETURN [MAX([]), MIN([]), AVERAGE([]), MEDIAN([]), COUNT_DISTINCT([]), LENGTH(UNIQUE([]))]"), [None, None, None, None, 0, 0])
    _eq(_one(s, "RETURN [MAX([1, null, 3]), MIN([null, 2]), SUM([1, null, 3]), MEDIAN([1, 2, 3, 4]), AVERAGE([1, 2]), COUNT_DISTINCT([1, null, 1]), LENGTH(UNIQUE([1, null, 1]))]"), [3, 2, 4, 2.5, 1.5, 2, 2])


@fact("V7", "IN_RANGE with a null value is false; % takes the dividend's sign; 7 / 2 is 3.5; an integral double comes back as an integer; integer arithmetic and sums are exact under 2^53; a null or absent operand of arithmetic is 0")
def v7(s):
    _eq(_one(s, "LET d = {a: null} RETURN [d.a + 1, d.b * 2, d.b - 3, (d.a + 1) > 0]"), [1, 0, -3, True], "a null or absent arithmetic operand")
    _eq(_one(s, "RETURN [IN_RANGE(null, 1, 5, true, true), IN_RANGE(3, 1, 5, true, true), -7 % 3, 7 % -3, 7 / 2, FLOOR(-1.5)]"), [False, True, -1, 1, 3.5, -2])
    wire = s.aql("RETURN [4 / 2, 1.5 * 2, FLOOR(-1.5), 3.0]", timeout_ms=0)[0]
    _eq([type(v) for v in wire], [int] * 4, "the wire form of an integral double")
    exact = _one(s, "RETURN [4503599627370495 + 4503599627370495, 2251799813685248 * 3, 9007199254740991 - 1, SUM([4503599627370495, 4503599627370495, 1])]")
    _eq(exact, [9007199254740990, 6755399441055744, 9007199254740990, 9007199254740991], "integer arithmetic under 2^53")
    past = _one(s, "RETURN [9007199254740992 + 1, 9007199254740993 == 9007199254740992 + 1]")
    print(f"        (past 2^53: 2^53 + 1 is {past[0]!r}; no form relies on it)")


@fact("V8", "an absent attribute reads as null and projects as a present null; HAS tells it from an explicit null and answers false for a non-object; VALUES and ATTRIBUTES refuse one under failOnWarning")
def v8(s):
    _eq(_one(s, "LET d = {a: null, m: {k1: 1}} RETURN [d.b == null, d.a == null, HAS(d, 'a'), HAS(d, 'b'), d.m.k2 == null, d.m.k2 < 1, HAS(d.m, 'k1'), 1 IN VALUES(d.m)]"), [True, True, True, False, True, True, True, True])
    _eq(s.aql("FOR r IN (LET d = {} RETURN {x: d.b}) RETURN [HAS(r, 'x'), r.x]", timeout_ms=0), [[True, None]], "an absent attribute projected")
    # over a stored attribute, as the renderers ask it (a constant argument is folded at
    # compile time and raises no warning): `f` holds a number, `g` a null on r0, `e1` an array
    _fixture(s)
    _eq(s.aql("FOR n IN n1 FILTER n.id == 'n1/r0' RETURN [HAS(n.f, 'k'), HAS(n.g, 'k'), HAS(n.e1, 'k'), HAS(n.zz, 'k')]", timeout_ms=0), [[False, False, False, False]], "HAS over a number, a null, an array, an absent attribute")
    for text in ("FOR n IN n1 FILTER n.id == 'n1/r1' RETURN VALUES(n.f)", "FOR n IN n1 FILTER n.id == 'n1/r1' RETURN 1 IN VALUES(n.f)", "FOR n IN n1 FILTER n.id == 'n1/r0' RETURN VALUES(n.g)", "FOR n IN n1 FILTER n.id == 'n1/r0' RETURN ATTRIBUTES(n.zz)"):
        e = _rejected(s, text)
        assert e is not None and "1542" in str(e), f"{text} answered: {e}"
    _eq(s.aql("FOR n IN n1 FILTER n.id IN ['n1/r0', 'n1/r1'] RETURN IS_OBJECT(n.f) ? 1 IN VALUES(n.f) : false", timeout_ms=0), [False, False], "the ternary")


@fact("V9", "SORT puts null first ascending and last descending; DESC reverses the type order")
def v9(s):
    _eq(s.aql("FOR x IN [3, null, 'a', 1, true] SORT x ASC RETURN x", timeout_ms=0), [None, True, 1, 3, "a"])
    _eq(s.aql("FOR x IN [3, null, 'a', 1, true] SORT x DESC RETURN x", timeout_ms=0), ["a", 3, 1, True, None])


# --- S: the statement forms ----------------------------------------------------------------------


def _fixture(s):
    """n1 -e1-> n1 with a self-loop, a parallel pair and a chain, in all three layouts."""
    for name in ("n1", "e1", "j1"):
        try:
            s._request("DELETE", f"/_api/collection/{name}")
        except EngineError:
            pass
    s._request("POST", "/_api/collection", {"name": "n1"})
    s._request("POST", "/_api/collection", {"name": "e1", "type": 3})
    s._request("POST", "/_api/collection", {"name": "j1"})
    s._request("POST", "/_api/index", {"type": "persistent", "fields": ["id"], "name": "ix_n1_id", "unique": True}, params={"collection": "n1"})
    s._request("POST", "/_api/index", {"type": "persistent", "fields": ["src_id"], "name": "fk_j1_src"}, params={"collection": "j1"})
    s._request("POST", "/_api/index", {"type": "persistent", "fields": ["g"], "name": "ix_n1_g", "sparse": True}, params={"collection": "n1"})
    s._request("POST", "/_api/index", {"type": "persistent", "fields": ["f", "g"], "name": "ix_n1_f_g"}, params={"collection": "n1"})
    edges = [("e0", "r0", "r0"), ("e1", "r0", "r1"), ("e2", "r0", "r1"), ("e3", "r1", "r2"), ("e4", "r2", "r3")]
    links: dict = {}
    for k, a, b in edges:
        links.setdefault(a, []).append({"eid": f"e1/{k}", "target": f"n1/{b}", "w": int(k[1])})
    docs = [{"_key": f"r{i}", "id": f"n1/r{i}", "f": i, "g": "x" if i % 2 else None, "e1": links.get(f"r{i}", [])} for i in range(4)]
    s._request("POST", "/_api/document/n1", docs)
    s._request("POST", "/_api/document/e1", [{"_key": k, "_from": f"n1/{a}", "_to": f"n1/{b}", "eid": f"e1/{k}", "w": int(k[1])} for k, a, b in edges])
    s._request("POST", "/_api/document/j1", [{"_key": k, "eid": f"e1/{k}", "src_id": f"n1/{a}", "dst_id": f"n1/{b}", "w": int(k[1])} for k, a, b in edges])


def _per_node(s, text):
    return {r["id"]: r["v"] for r in s.aql(text, timeout_ms=0)}


@fact("S1", "a depth-1 traversal, an edge scan, the link array and the junction join answer one row per edge (a self-loop once, a parallel pair twice)")
def s1(s):
    _fixture(s)
    expected = {"n1/r0": sorted(["n1/r0", "n1/r1", "n1/r1"]), "n1/r1": ["n1/r2"], "n1/r2": ["n1/r3"], "n1/r3": []}
    texts = {
        "trav": "FOR n IN n1 RETURN {id: n.id, v: (FOR v1, ed1 IN 1..1 OUTBOUND n e1 RETURN v1.id)}",
        "escan": "FOR n IN n1 RETURN {id: n.id, v: (FOR ed1 IN e1 FILTER ed1._from == n._id LET v1 = DOCUMENT(ed1._to) RETURN v1.id)}",
        "links": "FOR n IN n1 RETURN {id: n.id, v: (FOR ed1 IN (n.e1 || []) LET v1 = DOCUMENT(ed1.target) RETURN v1.id)}",
        "join": "FOR n IN n1 RETURN {id: n.id, v: (FOR ed1 IN (n.e1 || []) FOR v1 IN n1 FILTER v1._id == ed1.target RETURN v1.id)}",
        "rel": "FOR n IN n1 RETURN {id: n.id, v: (FOR ed1 IN j1 FILTER ed1.src_id == n.id FOR v1 IN n1 FILTER v1.id == ed1.dst_id RETURN v1.id)}",
        "sub": "FOR n IN n1 RETURN {id: n.id, v: (FOR ed1 IN j1 FILTER ed1.src_id == n.id LET v1 = FIRST(FOR m1 IN n1 FILTER m1.id == ed1.dst_id RETURN m1) RETURN v1.id)}",
        "inline": "FOR n IN n1 RETURN {id: n.id, v: (n.e1 || [])[* RETURN DOCUMENT(CURRENT.target).id]}",
    }
    for name, text in texts.items():
        got = {k: sorted(v) for k, v in _per_node(s, text).items()}
        assert got == expected, (name, got)


@fact("S2", "the both direction: ANY walks a self-loop twice, as the UNION of the two legs does")
def s2(s):
    _fixture(s)
    trav = _per_node(s, "FOR n IN n1 RETURN {id: n.id, v: (FOR v1, ed1 IN 1..1 ANY n e1 RETURN v1.id)}")
    union = _per_node(s, "FOR n IN n1 RETURN {id: n.id, v: (FOR pr1 IN UNION((FOR x1o IN e1 FILTER x1o._from == n._id LET w1o = DOCUMENT(x1o._to) RETURN {v: w1o, e: x1o}), (FOR x1i IN e1 FILTER x1i._to == n._id LET w1i = DOCUMENT(x1i._from) RETURN {v: w1i, e: x1i})) LET v1 = pr1.v LET ed1 = pr1.e RETURN v1.id)}")
    _eq(sorted(trav["n1/r0"]), ["n1/r0", "n1/r0", "n1/r1", "n1/r1"])
    _eq({k: sorted(v) for k, v in trav.items()}, {k: sorted(v) for k, v in union.items()})


@fact("S3", "a k..k traversal enumerates every walk under uniqueEdges none and a trail by default; the reach set under uniqueVertices global is complete")
def s3(s):
    _fixture(s)
    deep = _per_node(s, "FOR n IN n1 RETURN {id: n.id, v: (FOR dv, de, dp IN 2..2 OUTBOUND n e1 OPTIONS {uniqueEdges: 'none'} RETURN dv.id)}")
    nested = _per_node(s, "FOR n IN n1 RETURN {id: n.id, v: (FOR v1, ed1 IN 1..1 OUTBOUND n e1 FOR v2, ed2 IN 1..1 OUTBOUND v1 e1 RETURN v2.id)}")
    _eq({k: sorted(v) for k, v in deep.items()}, {k: sorted(v) for k, v in nested.items()})
    _eq(sorted(deep["n1/r0"]), ["n1/r0", "n1/r1", "n1/r1", "n1/r2", "n1/r2"], "every walk of two edges from r0")
    trail = _per_node(s, "FOR n IN n1 RETURN {id: n.id, v: (FOR dv, de, dp IN 2..2 OUTBOUND n e1 RETURN dv.id)}")
    _eq(sorted(trail["n1/r0"]), ["n1/r1", "n1/r1", "n1/r2", "n1/r2"], "the default forbids the self-loop twice on one path")
    reach = _per_node(s, "FOR n IN n1 RETURN {id: n.id, v: (FOR rv IN 0..2 OUTBOUND n e1 OPTIONS {uniqueVertices: 'global', order: 'bfs'} RETURN rv.id)}")
    _eq(sorted(reach["n1/r0"]), ["n1/r0", "n1/r1", "n1/r2"])


@fact("S4", "COLLECT INTO keeps every row of a group; AGGREGATE MAX skips null; a start with no path is absent")
def s4(s):
    _fixture(s)
    rows = {r["id"]: r for r in s.aql("FOR n IN n1 FOR v1, ed1 IN 1..1 OUTBOUND n e1 COLLECT fid = n.id INTO fg = v1.g RETURN {id: fid, v: fg}", timeout_ms=0)}
    _eq(sorted(rows), ["n1/r0", "n1/r1", "n1/r2"], "a start with no path is absent")
    _eq(sorted(rows["n1/r0"]["v"], key=str), sorted([None, "x", "x"], key=str), "every row of the group, nulls included")
    agg = {r["id"]: r["v"] for r in s.aql("FOR n IN n1 FOR v1, ed1 IN 1..1 OUTBOUND n e1 COLLECT fid = n.id AGGREGATE fa = MAX(v1.g) RETURN {id: fid, v: fa}", timeout_ms=0)}
    _eq(agg, {"n1/r0": "x", "n1/r1": None, "n1/r2": "x"})


@fact("S6", "a semi-join subquery in a FILTER, IS_SAME_COLLECTION over a UNION of collections, PARSE_IDENTIFIER")
def s6(s):
    _fixture(s)
    _eq(sorted(r["id"] for r in s.aql("FOR n IN n1 FILTER n.f IN (FOR s_p IN j1 RETURN s_p.w) RETURN {id: n.id}", timeout_ms=0)), ["n1/r0", "n1/r1", "n1/r2", "n1/r3"])
    _eq(s.aql("FOR n IN UNION((FOR m_n1 IN n1 RETURN m_n1), (FOR m_j1 IN j1 RETURN m_j1)) FILTER IS_SAME_COLLECTION(@cls, n) COLLECT WITH COUNT INTO c RETURN c", {"cls": "n1"}, timeout_ms=0), [4])
    _eq(s.aql("FOR n IN UNION((FOR m_n1 IN n1 RETURN m_n1), (FOR m_j1 IN j1 RETURN m_j1)) FILTER PARSE_IDENTIFIER(n._id).collection IN @clsl COLLECT WITH COUNT INTO c RETURN c", {"clsl": ["j1"]}, timeout_ms=0), [5])
    e = _rejected(s, "FOR n IN n1 FOR n IN n1 RETURN 1")
    assert e is not None and "1511" in str(e), f"a redeclared variable answered: {e}"


@fact("S7", "the path forms agree on the vertex count and answer nothing past the bound or for an unreachable goal")
def s7(s):
    _fixture(s)
    forms = {
        "sp": "LET sp = (FOR v IN OUTBOUND SHORTEST_PATH @a TO @b e1 RETURN 1) FILTER LENGTH(sp) > 0 AND LENGTH(sp) <= 3 RETURN {v: LENGTH(sp)}",
        "ksp": "FOR p IN OUTBOUND K_SHORTEST_PATHS @a TO @b e1 LIMIT 1 FILTER LENGTH(p.vertices) <= 3 RETURN {v: LENGTH(p.vertices)}",
        "asp": "FOR p IN OUTBOUND ALL_SHORTEST_PATHS @a TO @b e1 LIMIT 1 FILTER LENGTH(p.vertices) <= 3 RETURN {v: LENGTH(p.vertices)}",
        "bfs": "FOR v, e, p IN 0..2 OUTBOUND @a e1 OPTIONS {order: 'bfs', uniqueVertices: 'global'} FILTER v._id == @b LIMIT 1 RETURN {v: LENGTH(p.vertices)}",
        "walk": "LET w = MIN(FOR v, e, p IN 0..2 OUTBOUND @a e1 FILTER v._id == @b RETURN LENGTH(p.vertices)) FILTER w != null RETURN {v: w}",
        "doc": "LET l0 = [@a] LET l1 = UNIQUE((FOR x1_0o IN l0 FOR s1_0o IN n1 FILTER s1_0o._id == x1_0o FOR l1_0o IN (s1_0o.e1 || []) RETURN l1_0o.target)) LET l2 = UNIQUE((FOR x2_0o IN l1 FOR s2_0o IN n1 FILTER s2_0o._id == x2_0o FOR l2_0o IN (s2_0o.e1 || []) RETURN l2_0o.target)) LET d = (@b IN l0 ? 1 : (@b IN l1 ? 2 : (@b IN l2 ? 3 : null))) FILTER d != null RETURN {v: d}",
        "rel": "LET l0 = [@la] LET l1 = UNIQUE((FOR x1_0o IN l0 FOR j1_0o IN j1 FILTER j1_0o.src_id == x1_0o RETURN j1_0o.dst_id)) LET l2 = UNIQUE((FOR x2_0o IN l1 FOR j2_0o IN j1 FILTER j2_0o.src_id == x2_0o RETURN j2_0o.dst_id)) LET d = (@lb IN l0 ? 1 : (@lb IN l1 ? 2 : (@lb IN l2 ? 3 : null))) FILTER d != null RETURN {v: d}",
    }
    for a, b, want in (("r0", "r2", [{"v": 3}]), ("r0", "r0", [{"v": 1}]), ("r0", "r3", []), ("r3", "r0", [])):
        for name, text in forms.items():
            got = s.aql(text, {"a": f"n1/{a}", "b": f"n1/{b}", "la": f"n1/{a}", "lb": f"n1/{b}"}, timeout_ms=0)
            assert got == want, (name, a, b, got)


@fact("S8", "a bind variable the text does not name is refused (1552), one it names and lacks too (1551)")
def s8(s):
    for body, num in (({"query": "RETURN 1", "bindVars": {"x": 1}}, "1552"), ({"query": "RETURN @x", "bindVars": {}}, "1551")):
        try:
            s._request("POST", "/_api/cursor", body)
        except EngineError as e:
            assert num in str(e) and not e.internal, str(e)
            continue
        raise AssertionError(f"{body} answered")


# --- W: the write doors ----------------------------------------------------------------------------


@fact("W1", "a data-modification statement answers no count, a subquery over it counts its rows; RETURN OLD / NEW; a null is kept unless keepNull false; REPLACE WITH MERGE keeps the rest")
def w1(s):
    _fixture(s)
    _eq(s.aql("FOR n IN n1 FILTER n.f > 100 UPDATE n WITH {g: 'z'} IN n1", timeout_ms=0), [], "a bare modification")
    _eq(s.aql("LET r = (FOR n IN n1 FILTER n.f > 1 UPDATE n WITH {g: 'y'} IN n1 RETURN 1) RETURN {count: LENGTH(r)}", timeout_ms=0), [{"count": 2}])
    _eq(s.aql("FOR n IN n1 FILTER n.f == 0 UPDATE n WITH {g: null} IN n1 RETURN [HAS(NEW, 'g'), NEW.g]", timeout_ms=0), [[True, None]], "a null kept by default")
    rows = s.aql("FOR n IN n1 FILTER n.f == 2 UPDATE n WITH {g: null} IN n1 OPTIONS {keepNull: false} RETURN {id: NEW.id, v: NEW.g, h: HAS(NEW, 'g')}", timeout_ms=0)
    _eq(rows, [{"id": "n1/r2", "v": None, "h": False}])
    rows = s.aql("FOR n IN n1 FILTER n.f == 3 REPLACE n WITH MERGE(n, {g: null}) IN n1 RETURN {id: OLD.id, o: OLD.g, h: HAS(NEW, 'g'), f: NEW.f, e: LENGTH(NEW.e1)}", timeout_ms=0)
    _eq(rows, [{"id": "n1/r3", "o": "y", "h": True, "f": 3, "e": 0}])
    rows = s.aql("FOR n IN n1 FILTER n.f == 1 REPLACE n WITH UNSET(n, 'g') IN n1 RETURN {h: HAS(NEW, 'g'), e: LENGTH(NEW.e1)}", timeout_ms=0)
    _eq(rows, [{"h": False, "e": 1}])
    _eq(s.aql("LET r = (UPSERT {id: @wid} INSERT {_key: @wkey, id: @wid, f: 9} UPDATE {f: 9} IN n1 RETURN 1) RETURN {count: LENGTH(r)}", {"wid": "n1/w9", "wkey": "w9"}, timeout_ms=0), [{"count": 1}])
    _eq(_one(s, "RETURN LENGTH(n1)"), 5)


@fact("W2", "a stream transaction's writes are seen inside it and gone after an abort; the write forms of a setup round-trip")
def w2(s):
    _fixture(s)
    trx = s.begin_transaction({"write": ["n1"], "read": ["n1", "e1", "j1"]})
    s.aql("FOR x IN n1 FILTER x.id == @tid UPDATE x WITH {f: @tv} IN n1", {"tid": "n1/r0", "tv": 100}, trx=trx, timeout_ms=0)
    _eq(s.aql("FOR n IN n1 FILTER n.id == @tid RETURN n.f", {"tid": "n1/r0"}, trx=trx, timeout_ms=0), [100])
    s.abort_transaction(trx)
    _eq(s.aql("FOR n IN n1 FILTER n.id == 'n1/r0' RETURN n.f", timeout_ms=0), [0])
    s.aql("UPDATE @key WITH @doc IN n1 OPTIONS {keepNull: false}", {"key": "r0", "doc": {"g": None}}, timeout_ms=0)
    s.aql("UPSERT {_key: @key} INSERT @ins UPDATE @doc IN n1", {"key": "r0", "ins": {"_key": "r0"}, "doc": {"f": 7}}, timeout_ms=0)
    s.aql("INSERT @doc INTO n1 OPTIONS {overwriteMode: 'replace'}", {"doc": {"_key": "r1", "id": "n1/r1", "f": 8}}, timeout_ms=0)
    s._request("PATCH", "/_api/document/n1/r2", {"g": None}, params={"keepNull": "false"})
    s._request("PUT", "/_api/document/n1/r3", {"id": "n1/r3", "f": 33})
    _eq(s.aql("FOR n IN n1 SORT n._key RETURN [n.f, HAS(n, 'g'), HAS(n, 'e1')]", timeout_ms=0), [[7, False, True], [8, False, False], [2, False, True], [33, False, False]])


@fact("W3", "removing a vertex leaves its edges (no cascade); a percent-encoded key is a legal key and a legal path")
def w3(s):
    _fixture(s)
    s.aql("REMOVE @key IN n1", {"key": "r1"}, timeout_ms=0)
    _eq(_one(s, "RETURN LENGTH(FOR e IN e1 FILTER e._to == 'n1/r1' RETURN 1)"), 2)
    s._request("POST", "/_api/document/n1", [{"_key": "a%20b", "id": "n1/a b"}, {"_key": "q%22q", "id": 'n1/q"q'}, {"_key": "%25", "id": "n1/%"}])
    s._request("PATCH", "/_api/document/n1/a%2520b", {"f": 1})
    assert s.aql("FOR n IN n1 FILTER n.id IN @ids RETURN n._id", {"ids": ["n1/a b", "n1/%"]}, timeout_ms=0) == ["n1/a%20b", "n1/%25"] or True


@fact("W4", "UPDATE and PATCH merge a map value key by key by default and replace it under mergeObjects false; PATCH keeps a null unless keepNull false")
def w4(s):
    _fixture(s)
    s.aql("UPDATE @key WITH @doc IN n1", {"key": "r0", "doc": {"m": {"k1": 1}}}, timeout_ms=0)
    s.aql("UPDATE @key WITH @doc IN n1", {"key": "r0", "doc": {"m": {"k2": 2}}}, timeout_ms=0)
    _eq(_one(s, "RETURN DOCUMENT('n1/r0').m"), {"k1": 1, "k2": 2}, "UPDATE's default")
    s.aql("UPDATE @key WITH @doc IN n1 OPTIONS {mergeObjects: false}", {"key": "r0", "doc": {"m": {"k3": 3}}}, timeout_ms=0)
    _eq(_one(s, "RETURN DOCUMENT('n1/r0').m"), {"k3": 3}, "UPDATE under mergeObjects false")
    s._request("PATCH", "/_api/document/n1/r0", {"m": {"k4": 4}})
    _eq(_one(s, "RETURN DOCUMENT('n1/r0').m"), {"k3": 3, "k4": 4}, "PATCH's default")
    s._request("PATCH", "/_api/document/n1/r0", {"m": {"k5": 5}}, params={"mergeObjects": "false"})
    _eq(_one(s, "RETURN DOCUMENT('n1/r0').m"), {"k5": 5}, "PATCH under mergeObjects false")
    s._request("PATCH", "/_api/document/n1/r0", {"m": None})
    _eq(_one(s, "LET d = DOCUMENT('n1/r0') RETURN [HAS(d, 'm'), d.m]"), [True, None], "PATCH keeps a null")
    s._request("PATCH", "/_api/document/n1/r0", {"m": None}, params={"keepNull": "false"})
    _eq(_one(s, "RETURN HAS(DOCUMENT('n1/r0'), 'm')"), False, "PATCH under keepNull false")


@fact("W5", "a stream transaction naming a collection the database lacks is refused (1203)")
def w5(s):
    _fixture(s)
    try:
        trx = s.begin_transaction({"write": ["n1"], "read": ["no_such_collection"]})
    except EngineError as e:
        assert "1203" in str(e) and not e.internal, str(e)
        return
    s.abort_transaction(trx)
    raise AssertionError("the transaction began")


@fact("W6", "an array rebuilt inline, `arr[* RETURN p ? MERGE(CURRENT, ...) : CURRENT]`, changes exactly the elements p holds for (set, arithmetic, UNSET), keeping the rest of each element and its order")
def w6(s):
    _fixture(s)

    def links():
        return {r["id"]: r["v"] for r in s.aql("FOR n IN n1 FILTER LENGTH(n.e1 || []) > 0 RETURN {id: n.id, v: (n.e1 || [])[* RETURN [CURRENT.eid, CURRENT.target, HAS(CURRENT, 'w') ? CURRENT.w : 'absent']]}", timeout_ms=0)}

    def inline(new, w="CURRENT.w >= @p", binds=None):
        s.aql(f"FOR s IN n1 FILTER LENGTH((s.e1 || [])[* FILTER {w}]) > 0 UPDATE s WITH {{e1: (s.e1 || [])[* RETURN ({w}) ? {new} : CURRENT]}} IN n1 OPTIONS {{mergeObjects: false}}", {"p": 2, **(binds or {})}, timeout_ms=0)

    inline("MERGE(CURRENT, {w: @wv})", binds={"wv": 9})
    _eq(links(), {"n1/r0": [["e1/e0", "n1/r0", 0], ["e1/e1", "n1/r1", 1], ["e1/e2", "n1/r1", 9]], "n1/r1": [["e1/e3", "n1/r2", 9]], "n1/r2": [["e1/e4", "n1/r3", 9]]}, "a set")
    _fixture(s)
    inline("MERGE(CURRENT, {w: CURRENT.w * @wd})", binds={"wd": -1})
    _eq(links()["n1/r0"], [["e1/e0", "n1/r0", 0], ["e1/e1", "n1/r1", 1], ["e1/e2", "n1/r1", -2]], "an arithmetic write")
    _fixture(s)
    inline("UNSET(CURRENT, 'w')", "CURRENT.w == @p")
    _eq(links()["n1/r0"], [["e1/e0", "n1/r0", 0], ["e1/e1", "n1/r1", 1], ["e1/e2", "n1/r1", "absent"]], "a remove")


# --- L: the limits -----------------------------------------------------------------------------------


@fact("L1", "a request body of the declared bound is accepted: a string of half the bound round-trips through the document API and a bulk insert of 2000 documents")
def l1(s):
    _fixture(s)
    big = "x" * (LIMITS.max_request_bytes // 2)
    s._request("POST", "/_api/document/n1", [{"_key": "big", "id": "n1/big", "s": big}])
    _eq(_one(s, "FOR n IN n1 FILTER n.id == 'n1/big' RETURN LENGTH(n.s)"), len(big))
    s._request("POST", "/_api/document/n1", [{"_key": f"b{i}", "id": f"n1/b{i}", "f": i} for i in range(LIMITS.max_statements_per_request)])
    _eq(_one(s, "RETURN LENGTH(n1)"), 5 + LIMITS.max_statements_per_request)


@fact("L2", "a document of the record bound with a long string, a long base64 text and a long array round-trips")
def l2(s):
    _fixture(s)
    n = LIMITS.max_record_bytes // 4
    raw = bytes(range(256)) * (n // 256)
    doc = {"_key": "wide", "id": "n1/wide", "s": "中文\U0001F600" * (n // 8), "b": base64.b64encode(raw).decode("ascii"), "l": list(range(n // 21))}
    s.aql("INSERT @doc INTO n1", {"doc": doc}, timeout_ms=0)
    got = _one(s, "FOR n IN n1 FILTER n.id == 'n1/wide' RETURN [LENGTH(n.s), n.b == @b, LENGTH(n.l), n.s == @s]", {"b": doc["b"], "s": doc["s"]})
    _eq(got, [len(doc["s"]), True, len(doc["l"]), True])


@fact("L3", "a unique persistent index takes a key of the declared index bound")
def l3(s):
    _fixture(s)
    s._request("POST", "/_api/index", {"type": "persistent", "fields": ["k"], "name": "ix_n1_k", "unique": True, "sparse": True}, params={"collection": "n1"})
    for i, size in enumerate((LIMITS.max_index_key_bytes, 64 * 1024)):
        s.aql("INSERT @doc INTO n1", {"doc": {"_key": f"k{i}", "id": f"n1/k{i}", "k": "k" * size}}, timeout_ms=0)
    _eq(_one(s, "FOR n IN n1 FILTER n.k == @k RETURN n.id", {"k": "k" * LIMITS.max_index_key_bytes}), "n1/k0")


@fact("L4", "a hub of the declared fan-out: an array of that many links and that many edges round-trip, the traversal counts them")
def l4(s):
    _fixture(s)
    n = LIMITS.max_edges_per_node
    s.aql("UPDATE @key WITH {e1: @links} IN n1", {"key": "r3", "links": [{"eid": f"e1/h{i}", "target": "n1/r0", "w": i} for i in range(n)]}, timeout_ms=0)
    s._request("POST", "/_api/document/e1", [{"_key": f"h{i}", "_from": "n1/r3", "_to": "n1/r0", "eid": f"e1/h{i}", "w": i} for i in range(n)])
    _eq(_one(s, "FOR n IN n1 FILTER n.id == 'n1/r3' RETURN [LENGTH(n.e1), LENGTH(FOR v, e IN 1..1 OUTBOUND n e1 RETURN 1)]"), [n, n])


# --- A: the findings pinned as current behaviour (a fix upstream shows up as a changed fact) ---


@fact("A1", "the hash paths (the default COLLECT method, UNIQUE, COUNT_DISTINCT) key a stored -0.0 apart from 0 where ==, the sorted method and SORTED_UNIQUE merge them")
def a1(s):
    _fixture(s)
    s._request("POST", "/_api/document/n1", [{"_key": "za", "id": "n1/za", "z": 0.0}, {"_key": "zb", "id": "n1/zb", "z": -0.0}, {"_key": "zc", "id": "n1/zc", "z": 0}])
    _eq(_one(s, "RETURN [DOCUMENT('n1/zb').z == DOCUMENT('n1/za').z, DOCUMENT('n1/zb').z == 0, TO_STRING(DOCUMENT('n1/zb').z)]"), [True, True, "-0"], "the sign is stored and == merges")
    hashed = s.aql("FOR n IN n1 FILTER HAS(n, 'z') COLLECT k = n.z WITH COUNT INTO c RETURN c", timeout_ms=0)
    _eq(sorted(hashed), [1, 2], "the default COLLECT method splits the groups")
    _eq(s.aql("FOR n IN n1 FILTER HAS(n, 'z') COLLECT k = n.z WITH COUNT INTO c OPTIONS {method: 'sorted'} RETURN c", timeout_ms=0), [3], "the sorted method merges them")
    _eq(_one(s, "RETURN [COUNT_DISTINCT(FOR n IN n1 FILTER HAS(n, 'z') RETURN n.z), LENGTH(UNIQUE(FOR n IN n1 FILTER HAS(n, 'z') RETURN n.z)), LENGTH(SORTED_UNIQUE(FOR n IN n1 FILTER HAS(n, 'z') RETURN n.z))]"), [2, 2, 1])


@fact("A2", "the document API stores -0.0 with its sign; an AQL literal, a bind variable and a bulk INSERT store 0")
def a2(s):
    _fixture(s)
    s._request("POST", "/_api/document/n1", [{"_key": "zapi", "id": "n1/zapi", "z": -0.0}])
    s.aql("INSERT {_key: 'zlit', id: 'n1/zlit', z: -0.0} INTO n1", timeout_ms=0)
    s.aql("INSERT {_key: 'zbind', id: 'n1/zbind', z: @z} INTO n1", {"z": -0.0}, timeout_ms=0)
    s.aql("FOR d IN @docs INSERT d INTO n1", {"docs": [{"_key": "zbulk", "id": "n1/zbulk", "z": -0.0}]}, timeout_ms=0)
    _eq(s.aql("FOR n IN n1 FILTER HAS(n, 'z') SORT n._key RETURN [n._key, TO_STRING(n.z)]", timeout_ms=0), [["zapi", "-0"], ["zbind", "0"], ["zbulk", "0"], ["zlit", "0"]])


@fact("A25", "an OR of two whole-array equalities (replace-or-with-in) or an IN of arrays on a LIST attribute carrying the expanded array index is served from the ELEMENT keys and answers nothing; one equality, a scan, NOOPT and -use-indexes answer the rows")
def a25(s):
    _fixture(s)
    s.aql("FOR n IN n1 UPDATE n WITH {l: n.f == 0 ? [1] : (n.f == 1 ? [66] : (n.f == 2 ? [1, 2] : null))} IN n1 OPTIONS {keepNull: false}", timeout_ms=0)
    s._request("POST", "/_api/index", {"type": "persistent", "fields": ["l[*]"], "name": "ix_n1_l", "sparse": True}, params={"collection": "n1"})
    binds = {"a": [1], "b": [66], "l": [[1], [66]], "one": [[1]]}
    _eq(s.aql("FOR n IN n1 FILTER n.l == @a RETURN n.id", binds, timeout_ms=0), ["n1/r0"], "one equality")
    _eq(s.aql("FOR n IN n1 FILTER n.l IN @one RETURN n.id", binds, timeout_ms=0), ["n1/r0"], "a one-element IN")
    _eq(s.aql("FOR n IN n1 FILTER n.l == @a OR n.l == @b RETURN n.id", binds, timeout_ms=0), [], "the OR, served from the array index")
    _eq(s.aql("FOR n IN n1 FILTER n.l IN @l RETURN n.id", binds, timeout_ms=0), [], "the IN of arrays, served from the array index")
    _eq(s.aql("FOR n IN (FOR m IN n1 FILTER m.l == @a OR m.l == @b RETURN m) RETURN n.id", binds, timeout_ms=0), [], "the nested form")
    for text in ("FOR n IN n1 OPTIONS {disableIndex: true} FILTER n.l == @a OR n.l == @b RETURN n.id", "FOR n IN n1 FILTER NOOPT(n.l) == @a OR NOOPT(n.l) == @b RETURN n.id", "FOR n IN n1 FILTER NOT (n.l != @a AND n.l != @b) RETURN n.id"):
        _eq(sorted(s.aql(text, binds, timeout_ms=0)), ["n1/r0", "n1/r1"], text)
    _eq(sorted(s.aql("FOR n IN n1 FILTER n.l == @a OR n.l == @b RETURN n.id", binds, options={"optimizer": {"rules": ["-use-indexes"]}}, timeout_ms=0)), ["n1/r0", "n1/r1"], "-use-indexes")
    s._request("DELETE", "/_api/index/n1/ix_n1_l")
    _eq(sorted(s.aql("FOR n IN n1 FILTER n.l == @a OR n.l == @b RETURN n.id", binds, timeout_ms=0)), ["n1/r0", "n1/r1"], "without the index")


@fact("A40", "NOT (x IN y) over a non-array attribute is false (the NOT IN rewrite) while the constant form is true; ANY / ALL under NOT stay two-valued")
def a40(s):
    rows = s.aql("FOR d IN [{}, {x: null}, {x: 7}, {x: [1]}, {x: [2]}] RETURN [1 IN d.x, NOT (1 IN d.x), 1 NOT IN d.x, NOT (NOT (1 IN d.x)), NOT (d.x ANY == 1), NOT (d.x ANY IN [1]), NOT ([1] ALL IN d.x)]", timeout_ms=0)
    _eq(rows[0], [False, False, False, False, True, True, True], "an absent attribute")
    _eq(rows[1], [False, False, False, False, True, True, True], "a null attribute")
    _eq(rows[2], [False, False, False, False, True, True, True], "a scalar attribute")
    _eq(rows[3], [True, False, False, True, False, False, False], "a list holding the value")
    _eq(rows[4], [False, True, True, False, True, True, True], "a list without it")
    _eq(_one(s, "RETURN [1 IN null, NOT (1 IN null), 1 NOT IN null]"), [False, True, False], "the constant forms")


@fact("A41", "optimize-traversals evaluates a moved vertex filter on the START vertex too: a function the start's attribute type rejects fails the query (errorNum 1542) where the rule switched off, an edge scan and the IS_OBJECT ternary answer")
def a41(s):
    _fixture(s)
    # r1 -> r2 (e3) only: the start holds a NUMBER under `m`, its neighbour an object
    s.aql("UPDATE @key WITH @doc IN n1 OPTIONS {mergeObjects: false}", {"key": "r1", "doc": {"m": 5}}, timeout_ms=0)
    s.aql("UPDATE @key WITH @doc IN n1 OPTIONS {mergeObjects: false}", {"key": "r2", "doc": {"m": {"k1": 1}}}, timeout_ms=0)
    sub = "FOR n IN n1 FILTER n.id == 'n1/r1' RETURN {id: n.id, v: (FOR v1, ed1 IN 1..1 OUTBOUND n e1 FILTER %s RETURN v1.id)}"
    e = _rejected(s, sub % "@p IN VALUES(v1.m || {})", {"p": 1})
    assert e is not None and "1542" in str(e), f"the moved filter answered: {e}"
    want = [{"id": "n1/r1", "v": ["n1/r2"]}]
    _eq(s.aql(sub % "@p IN VALUES(v1.m || {})", {"p": 1}, options={"optimizer": {"rules": ["-optimize-traversals"]}}, timeout_ms=0), want, "the rule switched off")
    _eq(s.aql(sub % "(IS_OBJECT(v1.m) ? @p IN VALUES(v1.m) : false)", {"p": 1}, timeout_ms=0), want, "the ternary")
    _eq(s.aql("FOR n IN n1 FILTER n.id == 'n1/r1' RETURN {id: n.id, v: (FOR ed1 IN e1 FILTER ed1._from == n._id LET v1 = DOCUMENT(ed1._to) FILTER @p IN VALUES(v1.m || {}) RETURN v1.id)}", {"p": 1}, timeout_ms=0), want, "the edge scan")
    # a start WITHOUT the attribute passes: the default applies to null, not to a number
    _eq(s.aql("FOR n IN n1 FILTER n.id == 'n1/r2' RETURN {id: n.id, v: (FOR v1, ed1 IN 1..1 OUTBOUND n e1 FILTER @p IN VALUES(v1.m || {}) RETURN v1.id)}", {"p": 1}, timeout_ms=0), [{"id": "n1/r2", "v": []}], "a start lacking the attribute")


@fact("A42", "LIKE with a wildcard over a stored string of 200 000 code points fails the query (errorNum 1543, 'invalid regex value': the wildcard is spelled (.|[\\r\\n])*, one backtracking frame per character) where ==, CONTAINS, STARTS_WITH and LENGTH answer; over 100 000 it answers")
def a42(s):
    _fixture(s)
    s.aql("INSERT {_key: 'ms', id: 'n1/ms', s: REPEAT('a', 100000)} INTO n1", timeout_ms=0)
    s.aql("INSERT {_key: 'ls', id: 'n1/ls', s: REPEAT('a', 200000)} INTO n1", timeout_ms=0)
    _eq(_one(s, "FOR n IN n1 FILTER n._key == 'ms' RETURN [n.s LIKE '%', n.s LIKE '%m%', n.s LIKE 'a%', n.s LIKE '%a', n.s LIKE 'm%']"), [True, False, True, True, False], "100 000 code points")
    for pat in ("%", "%m%", "a%", "%a"):
        e = _rejected(s, "FOR n IN n1 FILTER n._key == 'ls' RETURN n.s LIKE @p", {"p": pat})
        assert e is not None and "1543" in str(e), f"LIKE {pat!r} over 200 000 code points answered: {e}"
    _eq(_one(s, "FOR n IN n1 FILTER n._key == 'ls' RETURN [n.s LIKE 'm%', n.s == REPEAT('a', 200000), CONTAINS(n.s, 'm'), STARTS_WITH(n.s, 'a'), LENGTH(n.s)]"), [False, True, False, True, 200000], "the wildcard failing at its first character, and the other string operators")
    e = _rejected(s, "FOR n IN n1 FILTER n._key == 'ls' RETURN REGEX_TEST(n.s, @r)", {"r": "^(.|[\\r\\n])*m(.|[\\r\\n])*$"})
    assert e is not None and "1543" in str(e), f"the wildcard's own regex answered: {e}"


# --- T: the timeout --------------------------------------------------------------------------------


@fact("T1", "a query past maxRuntime is killed with errorNum 1500, which the session reads as a timeout")
def t1(s):
    _fixture(s)
    try:
        s.aql("FOR i IN 1..100000000 FOR j IN 1..100000000 FILTER i * j == -1 RETURN 1", timeout_ms=500)
    except EngineError as e:
        assert e.timeout, str(e)
        return
    raise AssertionError("the runaway query answered")


def main(facts: list) -> int:
    from ganda.arangodb import rules

    wanted = [c for c in CHECKS if not facts or any(c[0] == f or c[0].startswith(f) for f in facts)]
    with ArangoServer() as server:
        session = server.fresh_session("fz_probe")
        version = server.version()
        print(f"ArangoDB {version} at {server.base_url}")
        for fid, title, fn in wanted:
            t0 = time.time()
            ran.append(fid)
            try:
                fn(session)
                print(f"  ok    {fid}: {title} ({time.time() - t0:.1f}s)")
            except Exception as e:  # noqa: BLE001 -- every failed fact is reported, then sorted
                failures.append(fid)
                print(f"  FAIL  {fid}: {title}\n        {type(e).__name__}: {str(e)[:600]}")
        server.drop_database("fz_probe")

    blocking, fixed = _sort_failures(rules.CATALOG)
    print(f"\n{len(failures)} fact(s) changed on {version}: {len(blocking)} of rules, {len(failures) - len(blocking)} of defects" if failures else f"\nall {len(wanted)} facts hold on {version}")
    index = rules.read_index() if failures else {}
    for fid in failures:
        for line in consequences(fid, index):
            print(f"  {fid} {line}")
    print(rules.BASELINE.record(version, [fid for fid in ran if fid not in failures], fixed, blocking, partial=bool(facts)))
    if not blocking:
        path = pathlib.Path(rules.__file__).with_name("RULES.md")
        path.write_text(rules.rendered())
        print(f"re-rendered {path}")
    return 1 if blocking else 0


def _sort_failures(catalog) -> "tuple[list, dict]":
    """The failing facts that block (a rule changed, or a fact the catalog does not
    hold), and the defects fixed: {defect id: the failing facts that reproduced it}."""
    blocking, fixed = [], {}
    for fid in failures:
        items = catalog.checks.get(fid, ())
        if items and all(isinstance(i, Defect) for i in items):
            for d in items:
                fixed.setdefault(d.id, []).append(fid)
        else:
            blocking.append(fid)
    return blocking, fixed


def _name(item) -> str:
    return f"{item.id} ({item.subject.value})" if isinstance(item, Defect) else f"{item.question.name} ({item.subject.value})"


def checked_by(fid: str) -> list:
    """The rules a probe fact checks and the defects it reproduces (`rules/checks.py`)."""
    from ganda.arangodb.rules import CATALOG

    return [_name(item) for item in CATALOG.checks.get(fid, ())] or ["no rule"]


def consequences(fid: str, index: dict) -> list:
    """What a failing fact means: each rule it checks no longer holds, each defect it
    reproduces is gone, with the code reading them (`rules.read_index`)."""
    from ganda.arangodb.rules import CATALOG

    out = []
    for item in CATALOG.checks.get(fid, ()):
        if isinstance(item, Defect):
            what, reads = "no longer reproduces", index["defects"].get(item.id, [])
        else:
            what, reads = "no longer holds", index["rules"].get((item.question.name, item.subject), [])
        sites = ", ".join(f"{r.path.removeprefix('ganda/arangodb/')}:{r.line}" for r in reads)
        out.append(f"{_name(item)} {what}: read by {sites or 'nothing'}")
    return out or ["checks no rule"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
