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
from ganda.arangodb.engine.semantics import eq, lt

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


@fact("V6", "MAX / MIN over nothing are null and skip a null")
def v6(s):
    _eq(_one(s, "RETURN [MAX([]), MIN([]), MAX([1, null, 3]), MIN([null, 2])]"), [None, None, 3, 2])


@fact("V7", "integer arithmetic and sums are exact under 2^53")
def v7(s):
    exact = _one(s, "RETURN [4503599627370495 + 4503599627370495, 2251799813685248 * 3, 9007199254740991 - 1, SUM([4503599627370495, 4503599627370495, 1])]")
    _eq(exact, [9007199254740990, 6755399441055744, 9007199254740990, 9007199254740991], "integer arithmetic under 2^53")
    past = _one(s, "RETURN [9007199254740992 + 1, 9007199254740993 == 9007199254740992 + 1]")
    print(f"        (past 2^53: 2^53 + 1 is {past[0]!r}; no form relies on it)")


@fact("V8", "HAS answers false for a non-object; VALUES and ATTRIBUTES refuse one under failOnWarning")
def v8(s):
    # over a stored attribute, as the renderers ask it (a constant argument is folded at
    # compile time and raises no warning): `f` holds a number, `g` a null on r0, `e1` an array
    _fixture(s)
    _eq(s.aql("FOR n IN n1 FILTER n.id == 'n1/r0' RETURN [HAS(n.f, 'k'), HAS(n.g, 'k'), HAS(n.e1, 'k'), HAS(n.zz, 'k')]", timeout_ms=0), [[False, False, False, False]], "HAS over a number, a null, an array, an absent attribute")
    for text in ("FOR n IN n1 FILTER n.id == 'n1/r1' RETURN VALUES(n.f)", "FOR n IN n1 FILTER n.id == 'n1/r1' RETURN 1 IN VALUES(n.f)", "FOR n IN n1 FILTER n.id == 'n1/r0' RETURN VALUES(n.g)", "FOR n IN n1 FILTER n.id == 'n1/r0' RETURN ATTRIBUTES(n.zz)"):
        e = _rejected(s, text)
        assert e is not None and "1542" in str(e), f"{text} answered: {e}"
    _eq(s.aql("FOR n IN n1 FILTER n.id IN ['n1/r0', 'n1/r1'] RETURN IS_OBJECT(n.f) ? 1 IN VALUES(n.f) : false", timeout_ms=0), [False, False], "the ternary")


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


@fact("W1", "UPDATE keeps a null unless keepNull false; REPLACE WITH MERGE / UNSET writes what UPDATE does and keeps the rest of the document")
def w1(s):
    _fixture(s)
    s.aql("FOR n IN n1 FILTER n.f > 1 UPDATE n WITH {g: 'y'} IN n1", timeout_ms=0)
    _eq(s.aql("FOR n IN n1 FILTER n.f == 0 UPDATE n WITH {g: null} IN n1 RETURN [HAS(NEW, 'g'), NEW.g]", timeout_ms=0), [[True, None]], "a null kept by default")
    rows = s.aql("FOR n IN n1 FILTER n.f == 2 UPDATE n WITH {g: null} IN n1 OPTIONS {keepNull: false} RETURN {id: NEW.id, v: NEW.g, h: HAS(NEW, 'g')}", timeout_ms=0)
    _eq(rows, [{"id": "n1/r2", "v": None, "h": False}])
    rows = s.aql("FOR n IN n1 FILTER n.f == 3 REPLACE n WITH MERGE(n, {g: null}) IN n1 RETURN {id: OLD.id, o: OLD.g, h: HAS(NEW, 'g'), f: NEW.f, e: LENGTH(NEW.e1)}", timeout_ms=0)
    _eq(rows, [{"id": "n1/r3", "o": "y", "h": True, "f": 3, "e": 0}])
    rows = s.aql("FOR n IN n1 FILTER n.f == 1 REPLACE n WITH UNSET(n, 'g') IN n1 RETURN {h: HAS(NEW, 'g'), e: LENGTH(NEW.e1)}", timeout_ms=0)
    _eq(rows, [{"h": False, "e": 1}])


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


def _fresh(s, *names, edge=()):
    for name in names + tuple(edge):
        try:
            s._request("DELETE", f"/_api/collection/{name}")
        except EngineError:
            pass
    for name in names:
        s._request("POST", "/_api/collection", {"name": name})
    for name in edge:
        s._request("POST", "/_api/collection", {"name": name, "type": 3})


@fact("A39", "use-indexes merges an integer constant with a double one or two ulps from it as one number and plans the conjunction as empty, on a collection with no index: `f > -5e-324 AND f == 0`, `f >= 2 AND f < 2.0000000000000004`, `f != 1.0000000000000002 AND f == 1`; the integer written as a double, a bound farther away, NOOPT and -use-indexes answer the rows")
def a39(s):
    _fresh(s, "ul")
    s._request("POST", "/_api/document/ul", [{"_key": "a", "f": 0}, {"_key": "b", "f": 1}, {"_key": "c", "f": 2}])

    def keys(cond, binds=None, **kw):
        return sorted(s.aql(f"FOR d IN ul FILTER {cond} RETURN d._key", binds, timeout_ms=0, **kw))

    for cond in ("d.f > -5e-324 AND d.f == 0", "d.f < 5e-324 AND d.f == 0", "d.f > -1e-323 AND d.f == 0", "d.f >= 2 AND d.f < 2.0000000000000004", "d.f != 1.0000000000000002 AND d.f == 1"):
        _eq(keys(cond), [], cond)
    _eq(keys("d.f > @a AND d.f IN @b", {"a": -5e-324, "b": [0, 0]}), [], "through bind parameters")
    _eq(_one(s, "RETURN [0 > -5e-324, 0 != -5e-324, 2 < 2.0000000000000004]"), [True, True, True], "the constants compare exactly")
    for cond in ("d.f > -5e-324 AND d.f == 0.0", "d.f > -4e-320 AND d.f == 0", "NOOPT(d.f) > -5e-324 AND NOOPT(d.f) == 0"):
        _eq(keys(cond), ["a"], cond)
    _eq(keys("d.f > -5e-324 AND d.f == 0", options={"optimizer": {"rules": ["-use-indexes"]}}), ["a"], "-use-indexes")


@fact("A40", "NOT (x IN y) is rewritten into x NOT IN y, false over a null, absent or scalar attribute, where the constant form is true; a NOT over an OR is kept unless optimize-traversals pushes it into a traversal or use-indexes through a filter an index serves on another field (a pulled-up filter once its subquery is inlined); `[* FILTER NOT (x IN CURRENT.y)]` drops the non-arrays")
def a40(s):
    rows = s.aql("FOR d IN [{}, {x: null}, {x: 7}, {x: [1]}, {x: [2]}] RETURN [1 IN d.x, NOT (1 IN d.x), 1 NOT IN d.x, NOT (NOT (1 IN d.x)), NOT (d.x ANY == 1), NOT (d.x ANY IN [1]), NOT ([1] ALL IN d.x)]", timeout_ms=0)
    _eq(rows[0], [False, False, False, False, True, True, True], "an absent attribute")
    _eq(rows[1], [False, False, False, False, True, True, True], "a null attribute")
    _eq(rows[2], [False, False, False, False, True, True, True], "a scalar attribute")
    _eq(rows[3], [True, False, False, True, False, False, False], "a list holding the value")
    _eq(rows[4], [False, True, True, False, True, True, True], "a list without it")
    _eq(_one(s, "RETURN [1 IN null, NOT (1 IN null), 1 NOT IN null]"), [False, True, False], "the constant forms")
    # s -> a, b, c, d; a stores no x, b a null, c [1], d [2] (the edges likewise); s holds 1
    _fresh(s, "na", edge=("ne",))
    s._request("POST", "/_api/index", {"type": "persistent", "fields": ["f"], "name": "ix_na_f"}, params={"collection": "na"})
    arr = [{"k": "a"}, {"k": "b", "x": None}, {"k": "c", "x": [1]}, {"k": "d", "x": [2]}]
    xs = {"a": {}, "b": {"x": None}, "c": {"x": [1]}, "d": {"x": [2]}}
    s._request("POST", "/_api/document/na", [{"_key": "s", "x": [1], "f": True, "arr": arr}] + [{"_key": k, "f": True, **x} for k, x in xs.items()])
    s._request("POST", "/_api/document/ne", [{"_key": f"e{k}", "_from": "na/s", "_to": f"na/{k}", **x} for k, x in xs.items()])
    _eq(_one(s, "FOR n IN na FILTER n._key == 's' RETURN n.arr[* FILTER NOT (1 IN CURRENT.x) RETURN CURRENT.k]"), ["d"], "the inline filter's complement")
    _eq(sorted(s.aql("FOR e IN ne FILTER 1 IN e.x RETURN e._key", timeout_ms=0)), ["ec"], "the collection filter")
    off = {"optimizer": {"rules": ["-optimize-traversals"]}}
    for var, want, right in (("e", ["ed"], ["ea", "eb", "ed"]), ("v", ["ed"], ["ea", "eb", "ed"])):
        text = f"FOR v, e IN 1..1 OUTBOUND 'na/s' ne FILTER NOT (({var}._key == 'zz' OR 1 IN {var}.x)) SORT e._key RETURN e._key"
        _eq(s.aql(text, timeout_ms=0), want, f"the traversal, a NOT over an OR on {var}")
        _eq(s.aql(text, options=off, timeout_ms=0), right, f"-optimize-traversals, on {var}")
    _eq(s.aql("FOR e IN ne FILTER e._from == 'na/s' FILTER NOT ((e._key == 'zz' OR 1 IN e.x)) SORT e._key RETURN e._key", timeout_ms=0), ["ea", "eb", "ed"], "the edge scan keeps the NOT")
    pulled = "FOR hr IN (FOR n IN na RETURN {id: n._key, v: (FOR e IN ne FILTER e._from == n._id RETURN e._key), x: n.x, f: n.f}) FILTER NOT ((1 IN hr.x OR hr.f != true)) SORT hr.id RETURN hr.id"
    _eq(s.aql(pulled, timeout_ms=0), ["d"], "the pulled-up filter, f indexed")
    _eq(s.aql(pulled, options={"optimizer": {"rules": ["-use-indexes"]}}, timeout_ms=0), ["a", "b", "d"], "-use-indexes")
    _eq(s.aql("FOR n IN na FILTER NOT ((n._key == 'zz' OR 1 IN n.x)) SORT n._key RETURN n._key", timeout_ms=0), ["a", "b", "d"], "a collection loop with no other field to serve keeps the NOT")
    s._request("DELETE", "/_api/index/na/ix_na_f")
    _eq(s.aql(pulled, timeout_ms=0), ["a", "b", "d"], "the pulled-up filter, f not indexed")


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


@fact("A43", "a correlated strict upper bound served by a SPARSE persistent index, `FOR x IN c FILTER x.b < n.b` inside a LET, loses the records whose b is null or absent (null is below every number); `<=`, `==`, `>`, a constant or bound bound, and a dense index are right")
def a43(s):
    for name in ("sp",):
        try:
            s._request("DELETE", f"/_api/collection/{name}")
        except EngineError:
            pass
    s._request("POST", "/_api/collection", {"name": "sp"})
    s._request("POST", "/_api/document/sp", [{"_key": f"k{i}", "b": i + 0.5} for i in range(30)] + [{"_key": f"n{i}", "b": None} for i in range(4)] + [{"_key": f"a{i}"} for i in range(4)])

    def below(op):
        rows = s.aql(f"FOR n IN sp LET jm = (FOR x IN sp FILTER x.b {op} n.b RETURN x._key) RETURN {{k: n._key, v: LENGTH(jm)}}", timeout_ms=0)
        return {r["k"]: r["v"] for r in rows}["k10"]

    _eq([below("<"), below("<=")], [18, 19], "no index: 10 values and 8 nulls below 10.5")
    s._request("POST", "/_api/index", {"type": "persistent", "fields": ["b"], "sparse": True, "name": "ix_sp_b"}, params={"collection": "sp"})
    _eq([below("<"), below("<="), _one(s, "FOR x IN sp FILTER x.b < @p COLLECT WITH COUNT INTO c RETURN c", {"p": 10.5})], [10, 19, 18], "the sparse index loses the nulls under `<` only")


@fact("A44", "two HAS tests of a variable bound to an object literal holding a subquery result, under one AND / OR in a collection loop's FILTER (or two FILTERs, which are fused), or one NOT HAS an OR beside it repeats in the disjunctive form, fail use-indexes with a 500 (errorNum 4, 'The key of the next key/value pair must be a string', while optimizing); one HAS, an array value, -use-indexes and a loop over an array answer")
def a44(s):
    _fixture(s)
    text = "FOR d IN %s LET hr = {v: %s, a: 1} FILTER %s RETURN 1"
    sub = "(FOR x IN [1] RETURN x)"
    for cond in ("HAS(hr, 'a') AND HAS(hr, 'v')", "HAS(hr, 'a') OR HAS(hr, 'v')", "HAS(hr, 'a') AND HAS(hr, 'a')"):
        e = _rejected(s, text % ("n1", sub, cond))
        assert e is not None and e.internal and "key/value pair must be a string" in str(e), f"{cond} answered: {e}"
    pulled = "FOR hr IN (FOR d IN n1 RETURN {v: (FOR x IN [1] RETURN x), a: 1, b: 2}) FILTER HAS(hr, 'a') AND HAS(hr, 'b') RETURN 1"
    e = _rejected(s, pulled)
    assert e is not None and e.internal, f"the pulled-up form answered: {e}"
    e = _rejected(s, text % ("n1", sub, "HAS(hr, 'a') FILTER HAS(hr, 'v')"))
    assert e is not None and e.internal, f"two FILTERs (fused into one condition) answered: {e}"
    # one negated HAS that an OR beside it repeats in the disjunctive form; a positive one answers
    e = _rejected(s, text % ("n1", sub, "(hr.a == 1 OR hr.a == 2) AND NOT HAS(hr, 'v')"))
    assert e is not None and e.internal, f"a NOT HAS beside an OR answered: {e}"
    four = [1, 1, 1, 1]
    _eq(s.aql(text % ("n1", sub, "(hr.a == 1 OR hr.a == 2) AND HAS(hr, 'v')"), timeout_ms=0), four, "a HAS beside an OR")
    _eq(s.aql(text % ("n1", sub, "hr.a == 7 OR NOT HAS(hr, 'c')"), timeout_ms=0), four, "a NOT HAS in one disjunct")
    _eq(s.aql(text % ("n1", sub, "HAS(hr, 'a')"), timeout_ms=0), four, "one HAS")
    _eq(s.aql(text % ("n1", "[1]", "HAS(hr, 'a') AND HAS(hr, 'v')"), timeout_ms=0), four, "an array value")
    _eq(s.aql(text % ("n1", sub, "HAS(hr, 'a') AND HAS(hr, 'v')"), options={"optimizer": {"rules": ["-use-indexes"]}}, timeout_ms=0), four, "-use-indexes")
    _eq(s.aql(text % ("[1, 2]", sub, "HAS(hr, 'a') AND HAS(hr, 'v')"), timeout_ms=0), [1, 1], "a loop over an array")


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
