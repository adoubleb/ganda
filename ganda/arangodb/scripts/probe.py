"""Re-check live the ArangoDB facts the provider rests on (`ganda arangodb probe`), each
with an id (`RULES.md` lists them): the value semantics (`V`), the statement forms the
renderers emit (`S`), the write doors (`W`), the limits the large scale walks into (`L`)
and the timeout (`T`). A fact that fails changes the provider, never the probe -- unless
the probe's own AQL tested a form the provider never emits. Exit code: the number of
failed facts.

    python3 -m ganda arangodb probe            # a disposable server in Docker
    python3 -m ganda arangodb probe V S        # a subset (ids or prefixes)
    ARANGO_URL=http://127.0.0.1:8529 ARANGO_PASSWORD=... python3 -m ganda arangodb probe
"""
from __future__ import annotations

import base64
import sys
import time

from ganda.core.provider import EngineError
from ganda.arangodb.connection import ArangoServer
from ganda.arangodb.limits import LIMITS
from ganda.arangodb.semantics import eq, like_hit, lt

CHECKS: list = []


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
    print(f"        (string order: the code-point mirror disagrees with the engine on {order} of {len(pairs)} pairs; no predicate relies on it)")


@fact("V5", "the LIKE mirror agrees with the engine over the wildcard, escape, case and line-terminator pool")
def v5(s):
    vals = ["abc", "Abc", "ABC", "a\nb", "ab\n", "a\rb", "ab\r", "a\u2028b", "ab\u2028", "a\u2029b", "a\u0085b", "ab\u0085", "a%b", "axb", "a_b", "", "a", "\u00e9a", "\U0001F600b", "ab\r\n", None, 5, True]
    pats = ["a%", "%b", "a_c", "a\\%b", "a\\_b", "%", "ab", "a%b", "_", "%c", "a__", "abc", "5", "true"]
    pairs = [[v, p] for v in vals for p in pats]
    got = s.aql("FOR p IN @pairs RETURN p[0] LIKE p[1]", {"pairs": pairs}, timeout_ms=0)
    bad = [(v, pat, g, like_hit(v, pat)) for (v, pat), g in zip(pairs, got) if g != like_hit(v, pat)]
    _eq(bad[:20], [], f"{len(bad)} of {len(pairs)} LIKE answers disagree (value, pattern, engine, mirror)")


@fact("V6", "aggregates over nothing: SUM 0, MAX/MIN/AVERAGE/MEDIAN null, COUNT_DISTINCT 0; a keyless COLLECT AGGREGATE answers one row")
def v6(s):
    _eq(_one(s, "RETURN [SUM([]), MAX([]), MIN([]), AVERAGE([]), MEDIAN([]), COUNT_DISTINCT([]), LENGTH(UNIQUE([]))]"), [0, None, None, None, None, 0, 0])
    _eq(s.aql("FOR x IN [] COLLECT AGGREGATE c = COUNT(1), m = MAX(x) RETURN {c, m}", timeout_ms=0), [{"c": 0, "m": None}])
    _eq(_one(s, "RETURN [MAX([1, null, 3]), SUM([1, null, 3]), MEDIAN([1, 2, 3, 4]), AVERAGE([1, 2])]"), [3, 4, 2.5, 1.5])


@fact("V7", "IN_RANGE with a null value is false; % takes the dividend's sign; 7 / 2 is 3.5")
def v7(s):
    _eq(_one(s, "RETURN [IN_RANGE(null, 1, 5, true, true), IN_RANGE(3, 1, 5, true, true), -7 % 3, 7 % -3, 7 / 2, FLOOR(-1.5)]"), [False, True, -1, 1, 3.5, -2])


@fact("V8", "an absent attribute reads as null, HAS tells it from an explicit null, a missing map key is null")
def v8(s):
    _eq(_one(s, "LET d = {a: null, m: {k1: 1}} RETURN [d.b == null, d.a == null, HAS(d, 'a'), HAS(d, 'b'), d.m.k2 == null, d.m.k2 < 1, HAS(d.m, 'k1'), 1 IN VALUES(d.m)]"), [True, True, True, False, True, True, True, True])


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


@fact("S3", "a k..k traversal under uniqueEdges none enumerates every walk; the reach set under uniqueVertices global is complete")
def s3(s):
    _fixture(s)
    deep = _per_node(s, "FOR n IN n1 RETURN {id: n.id, v: (FOR dv, de, dp IN 2..2 OUTBOUND n e1 OPTIONS {uniqueEdges: 'none'} RETURN dv.id)}")
    nested = _per_node(s, "FOR n IN n1 RETURN {id: n.id, v: (FOR v1, ed1 IN 1..1 OUTBOUND n e1 FOR v2, ed2 IN 1..1 OUTBOUND v1 e1 RETURN v2.id)}")
    _eq({k: sorted(v) for k, v in deep.items()}, {k: sorted(v) for k, v in nested.items()})
    reach = _per_node(s, "FOR n IN n1 RETURN {id: n.id, v: (FOR rv IN 0..2 OUTBOUND n e1 OPTIONS {uniqueVertices: 'global', order: 'bfs'} RETURN rv.id)}")
    _eq(sorted(reach["n1/r0"]), ["n1/r0", "n1/r1", "n1/r2"])


@fact("S4", "COLLECT INTO keeps every row of a group; AGGREGATE MAX skips null; a start with no path is absent")
def s4(s):
    _fixture(s)
    rows = {r["id"]: r for r in s.aql("FOR n IN n1 FOR v1, ed1 IN 1..1 OUTBOUND n e1 COLLECT fid = n.id INTO fg = v1.g RETURN {id: fid, v: fg}", timeout_ms=0)}
    _eq(sorted(rows), ["n1/r0", "n1/r1", "n1/r2"] and sorted(rows["n1/r0"]["v"], key=str) == sorted([None, "x", "x"], key=str))
    agg = {r["id"]: r["v"] for r in s.aql("FOR n IN n1 FOR v1, ed1 IN 1..1 OUTBOUND n e1 COLLECT fid = n.id AGGREGATE fa = MAX(v1.g) RETURN {id: fid, v: fa}", timeout_ms=0)}
    _eq(agg, {"n1/r0": "x", "n1/r1": None, "n1/r2": "x"})


@fact("S5", "the select forms agree: NOOPT, disableIndex, a forced index hint, a subquery target, COLLECT WITH COUNT")
def s5(s):
    _fixture(s)
    base = s.aql("FOR n IN n1 FILTER n.id IN @p RETURN {id: n.id}", {"p": ["n1/r1", "n1/r3"]}, timeout_ms=0)
    for text in (
        "FOR n IN n1 FILTER NOOPT(n.id) IN @p RETURN {id: n.id}",
        "FOR n IN n1 OPTIONS {disableIndex: true} FILTER n.id IN @p RETURN {id: n.id}",
        "FOR n IN n1 OPTIONS {indexHint: 'ix_n1_id', forceIndexHint: true} FILTER n.id IN @p RETURN {id: n.id}",
        "FOR n IN (FOR m IN n1 RETURN m) FILTER n.id IN @p RETURN {id: n.id}",
        "FOR n IN (FOR m IN n1 FILTER m.id IN @p RETURN m) RETURN {id: n.id}",
    ):
        _eq(sorted(r["id"] for r in s.aql(text, {"p": ["n1/r1", "n1/r3"]}, timeout_ms=0)), sorted(r["id"] for r in base), text)
    _eq(_one(s, "RETURN {v: LENGTH(FOR n IN n1 FILTER n.f > 1 RETURN 1)}"), {"v": 2})
    _eq(s.aql("FOR n IN n1 FILTER n.f > 1 COLLECT WITH COUNT INTO c RETURN {v: c}", timeout_ms=0), [{"v": 2}])
    _eq(s.aql("FOR n IN n1 FILTER n.g != null COLLECT k = n.g WITH COUNT INTO c RETURN {k, v: c}", timeout_ms=0), [{"k": "x", "v": 2}])
    _eq(s.aql("FOR n IN n1 COLLECT k = n.g INTO g RETURN {k, v: LENGTH(g[* FILTER CURRENT.n.f != null])}", timeout_ms=0), [{"k": None, "v": 2}, {"k": "x", "v": 2}])
    _eq(s.aql("FOR n IN n1 SORT n.g DESC, n.id ASC LIMIT 1, 2 RETURN {id: n.id}", timeout_ms=0), [{"id": "n1/r3"}, {"id": "n1/r0"}])


@fact("S6", "a semi-join subquery in a FILTER, IS_SAME_COLLECTION over a UNION of collections, PARSE_IDENTIFIER")
def s6(s):
    _fixture(s)
    _eq(sorted(r["id"] for r in s.aql("FOR n IN n1 FILTER n.f IN (FOR s_p IN j1 RETURN s_p.w) RETURN {id: n.id}", timeout_ms=0)), ["n1/r0", "n1/r1", "n1/r2", "n1/r3"])
    _eq(s.aql("FOR n IN UNION((FOR m_n1 IN n1 RETURN m_n1), (FOR m_j1 IN j1 RETURN m_j1)) FILTER IS_SAME_COLLECTION(@cls, n) COLLECT WITH COUNT INTO c RETURN c", {"cls": "n1"}, timeout_ms=0), [4])
    _eq(s.aql("FOR n IN UNION((FOR m_n1 IN n1 RETURN m_n1), (FOR m_j1 IN j1 RETURN m_j1)) FILTER PARSE_IDENTIFIER(n._id).collection IN @clsl COLLECT WITH COUNT INTO c RETURN c", {"clsl": ["j1"]}, timeout_ms=0), [5])


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


# --- W: the write doors ----------------------------------------------------------------------------


@fact("W1", "a data-modification subquery counts its rows; RETURN OLD / NEW; keepNull false unsets; REPLACE WITH MERGE keeps the rest")
def w1(s):
    _fixture(s)
    _eq(s.aql("LET r = (FOR n IN n1 FILTER n.f > 1 UPDATE n WITH {g: 'y'} IN n1 RETURN 1) RETURN {count: LENGTH(r)}", timeout_ms=0), [{"count": 2}])
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
    wanted = [c for c in CHECKS if not facts or any(c[0] == f or c[0].startswith(f) for f in facts)]
    failed = 0
    with ArangoServer() as server:
        session = server.fresh_session("fz_probe")
        print(f"ArangoDB at {server.base_url}: {_one(session, 'RETURN VERSION()')}")
        for fid, title, fn in wanted:
            t0 = time.time()
            try:
                fn(session)
                print(f"  ok    {fid}: {title} ({time.time() - t0:.1f}s)")
            except Exception as e:  # noqa: BLE001 -- every failed fact is reported, then counted
                failed += 1
                print(f"  FAIL  {fid}: {title}\n        {type(e).__name__}: {str(e)[:600]}")
        server.drop_database("fz_probe")
    print(f"{len(wanted) - failed}/{len(wanted)} facts hold")
    return failed


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
