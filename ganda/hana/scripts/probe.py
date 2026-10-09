"""Re-check live every SAP HANA fact the provider rests on: `ganda hana probe`.

Each fact is one small statement against the attached instance (HANA_HOST, ...), run in
a scratch schema and graph that are dropped afterwards. A fact is either REQUIRED (the
provider's rendering or its semantics assume it: a failure means the provider must
change, see HANDOFF.md) or OBSERVED (recorded so a later run can see a change; never
fails). Every line prints what the engine answered.

    python3 -m ganda hana probe            # every fact
    python3 -m ganda hana probe K5 D6      # some of them (ids or prefixes: R, D, K)
"""
from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from ganda.core.provider import EngineError

PREFIX = "PREFIX c: <urn:ganda:c:> PREFIX p: <urn:ganda:p:> PREFIX i: <urn:ganda:i:> PREFIX l: <urn:ganda:l:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"
G = "<urn:ganda:graph>"


@dataclass
class Fact:
    id: str
    text: str
    check: Callable  # (Probe) -> (ok, what the engine answered)
    required: bool = True


class Probe:
    def __init__(self, session):
        self.s = session

    def sql(self, text: str, binds: "dict | None" = None):
        return self.s.sql(text, binds)

    def run(self, text: str, binds: "dict | None" = None):
        return self.s.sql(text, binds, rows=False)

    def one(self, text: str, binds: "dict | None" = None):
        rows = self.sql(text, binds)
        return rows[0]["v"] if rows else None

    def sparql(self, text: str):
        return self.s.sparql(f"{PREFIX} {text}")

    def update(self, text: str):
        return self.s.sparql(f"{PREFIX} {text}", update=True)

    def ids(self, text: str) -> list:
        return sorted(r["id"] for r in self.sparql(text))

    def error(self, fn) -> str:
        try:
            fn()
        except EngineError as e:
            return str(e)
        return ""


def _eq(got, want):
    return got == want, f"got {got!r}, want {want!r}"


# --- SQL on column tables -------------------------------------------------------------------


def r_setup(p: Probe):
    p.run("CREATE COLUMN TABLE pt (id NVARCHAR(256) PRIMARY KEY, i BIGINT, d DOUBLE, s NVARCHAR(5000), b BOOLEAN, l NVARCHAR(5000))")
    rows = [("a", 1, 0.5, "a", True, "[1,2]"), ("b", 2, -1.5, "B", False, "[]"), ("c", None, None, None, None, None), ("d", 9223372036854775807, 1.7976931348623157e308, "a ", True, "[3]")]
    for r in rows:
        p.run("INSERT INTO pt (id, i, d, s, b, l) VALUES (:a, :b, :c, :d, :e, :f)", dict(zip("abcdef", r)))
    return True, "pt with 4 rows"


R = [
    Fact("R0", "a column table with BIGINT, DOUBLE, NVARCHAR, BOOLEAN columns takes the boundary values", r_setup),
    Fact("R1", "BIGINT keeps 2^63 - 1 and DOUBLE keeps DBL_MAX exactly", lambda p: _eq([(r["i"], r["d"]) for r in p.sql("SELECT i, d FROM pt WHERE id = 'd'")], [(9223372036854775807, 1.7976931348623157e308)])),
    Fact("R2", "a BOOLEAN column reads back as a Python bool", lambda p: _eq([r["b"] for r in p.sql("SELECT b FROM pt WHERE id IN ('a', 'b') ORDER BY id")], [True, False])),
    Fact("R3", "NVARCHAR compares binary (UTF-16 code units): 'B' < 'a', and 'a' <> 'a ' (no padding)", lambda p: _eq([r["id"] for r in p.sql("SELECT id FROM pt WHERE s IS NOT NULL ORDER BY s, id")], ["b", "a", "d"])),
    Fact("R4", "an empty string is not NULL", lambda p: _eq(p.one("SELECT COUNT(*) AS v FROM DUMMY WHERE '' IS NULL"), 0)),
    Fact("R5", "NULLS FIRST / NULLS LAST are accepted, and ORDER BY puts NULL first ascending by default", lambda p: _eq([r["id"] for r in p.sql("SELECT id FROM pt ORDER BY i ASC, id")][0], "c"), required=False),
    Fact("R6", "ORDER BY ... DESC NULLS LAST with LIMIT / OFFSET", lambda p: _eq([r["id"] for r in p.sql("SELECT id FROM pt ORDER BY i DESC NULLS LAST, id ASC LIMIT 2 OFFSET 1")], ["b", "a"])),
    Fact("R7", "AVG of BIGINT is the exact quotient (1.5 for 1 and 2)", lambda p: _eq(p.one("SELECT AVG(i) AS v FROM pt WHERE id IN ('a', 'b')"), 1.5)),
    Fact("R8", "SUM of BIGINT is exact up to 2^63 - 1", lambda p: _eq(p.one("SELECT SUM(x) AS v FROM (SELECT 4611686018427387904 AS x FROM DUMMY UNION ALL SELECT 4611686018427387903 AS x FROM DUMMY) z"), 2**63 - 1)),
    Fact("R9", "PERCENTILE_CONT(0.5) is the interpolating median the reference computes (of 1 and 2: 1.5); HANA's own MEDIAN does not interpolate an even count of integers, so the renderings use PERCENTILE_CONT", lambda p: _eq(p.one("SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY i) AS v FROM pt WHERE id IN ('a', 'b')"), 1.5)),
    Fact("R10", "LIKE is case-sensitive and `_` is one character", lambda p: _eq([r["id"] for r in p.sql("SELECT id FROM pt WHERE s LIKE 'a%' ORDER BY id")], ["a", "d"])),
    Fact("R11", "two typed parameters compare in DUMMY's FROM (`CAST(? AS NVARCHAR(256)) = CAST(? AS ...)`)", lambda p: _eq(p.one("SELECT 1 AS v FROM DUMMY WHERE CAST(:a AS NVARCHAR(256)) = CAST(:b AS NVARCHAR(256))", {"a": "x", "b": "x"}), 1)),
    Fact("R12", "UPSERT ... VALUES ... WHERE id = ? updates an existing row and inserts a new one", lambda p: (p.run("UPSERT pt (id, i) VALUES (:k, 5) WHERE id = :k", {"k": "a"}), p.run("UPSERT pt (id, i) VALUES (:k, 6) WHERE id = :k", {"k": "e"}), _eq([(r["id"], r["i"]) for r in p.sql("SELECT id, i FROM pt WHERE id IN ('a', 'e') ORDER BY id")], [("a", 5), ("e", 6)]))[-1]),
    Fact("R13", "MERGE INTO t USING (SELECT ...) s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.i = ...", lambda p: (p.run("MERGE INTO pt USING (SELECT id FROM pt WHERE id = 'e') s ON pt.id = s.id WHEN MATCHED THEN UPDATE SET pt.i = 7"), _eq(p.one("SELECT i AS v FROM pt WHERE id = 'e'"), 7))[-1]),
    Fact("R14", "MERGE DELTA OF, a composite index, a unique index, a hash-partitioned table", lambda p: (p.run("MERGE DELTA OF pt"), p.run("CREATE INDEX ix_pt_is ON pt (i, s)"), p.run("CREATE UNIQUE INDEX ix_pt_s ON pt (d)"), p.run("CREATE COLUMN TABLE pp (id NVARCHAR(256) NOT NULL, v BIGINT) PARTITION BY HASH (id) PARTITIONS 3"), (True, "accepted"))[-1]),
    Fact("R15", "an UPDATE answers its row count", lambda p: _eq(p.run("UPDATE pt SET i = i + 1 WHERE i IS NOT NULL AND i < 100"), 3)),
    Fact("R16", "a derived table, a correlated scalar subquery, UNION ALL in FROM, EXISTS", lambda p: _eq(p.one("SELECT COUNT(*) AS v FROM (SELECT x.id FROM (SELECT id FROM pt UNION ALL SELECT id FROM pt) x WHERE EXISTS (SELECT 1 FROM pt y WHERE y.id = x.id AND (SELECT COUNT(*) FROM pt z WHERE z.id = y.id) = 1)) w"), 10)),
    Fact("R17", "a unique violation is error 301 (a rejection, not an internal error)", lambda p: (lambda e: (" 301]" in e or "[301]" in e, e[:120]))(p.error(lambda: p.run("INSERT INTO pt (id) VALUES ('a')")))),
]


# --- the JSON Document Store ----------------------------------------------------------------


def d_setup(p: Probe):
    p.run("CREATE COLLECTION pc")
    docs = [
        {"id": "a", "i": 1, "f": 0.1, "s": "x", "b": True, "arr": [1, 2], "links": [{"eid": "e0", "to": "b", "w": 3}]},
        {"id": "b", "i": 9007199254740993, "f": 0.30000000000000004, "s": "y", "b": False, "arr": [], "n": None},
        {"id": "c"},
    ]
    for d in docs:
        p.run("INSERT INTO pc VALUES(:doc)", {"doc": json.dumps(d)})
    p.run("""INSERT INTO pc VALUES({"id": 'd', "i": -5, "s": 'it''s', "b": TRUE, "arr": [7]})""")
    return True, "pc with 4 documents (one through the object expression)"


def _docs(p: Probe) -> dict:
    return {json.loads(next(iter(r.values())))["id"]: json.loads(next(iter(r.values()))) for r in p.sql("SELECT * FROM pc")}


D = [
    Fact("D0", "CREATE COLLECTION; INSERT a JSON text parameter and an object expression", d_setup),
    Fact("D1", "SELECT * reads a document back as JSON text; an integer above 2^53 and a double keep their values", lambda p: (lambda d: (d["b"]["i"] == 9007199254740993 and float(d["b"]["f"]) == 0.30000000000000004 and d["a"]["f"] == 0.1, json.dumps(d["b"])))(_docs(p))),
    Fact("D2", "a projected field is text: a number (TO_BIGINT / TO_DOUBLE to type it), a string unquoted, a boolean 'true'", lambda p: (lambda r: (r["i"] in ("1", 1) and r["s"] == "x" and str(r["b"]).lower() == "true", repr(r)))(p.sql('SELECT "i" AS i, "s" AS s, "b" AS b FROM pc WHERE "id" = CAST(:k AS NVARCHAR(256))', {"k": "a"})[0])),
    Fact("D3", "TO_BIGINT / TO_DOUBLE type a projected number", lambda p: _eq([(r["i"], r["f"]) for r in p.sql('SELECT TO_BIGINT("i") AS i, TO_DOUBLE("f") AS f FROM pc WHERE "id" = CAST(:k AS NVARCHAR(256))', {"k": "a"})], [(1, 0.1)])),
    Fact("D4", "an integer field compares with CAST(? AS BIGINT), above 2^53 exactly", lambda p: _eq([r["id"] for r in p.sql('SELECT "id" AS id FROM pc WHERE "i" = CAST(:v AS BIGINT)', {"v": 9007199254740993})], ["b"])),
    Fact("D5", "a fractional number (stored DECIMAL) compares with CAST(? AS DECIMAL) of its shortest repr", lambda p: _eq([r["id"] for r in p.sql('SELECT "id" AS id FROM pc WHERE "f" = CAST(:v AS DECIMAL)', {"v": Decimal("0.30000000000000004")})], ["b"])),
    Fact("D6", "a boolean compares with TO_JSON_BOOLEAN(TRUE)", lambda p: _eq(sorted(r["id"] for r in p.sql('SELECT "id" AS id FROM pc WHERE "b" = TO_JSON_BOOLEAN(TRUE)')), ["a", "d"])),
    Fact("D7", "IS NULL holds for an absent key and a JSON null; IS NOT NULL for neither", lambda p: _eq(sorted(r["id"] for r in p.sql('SELECT "id" AS id FROM pc WHERE "n" IS NULL')), ["a", "b", "c", "d"])),
    Fact("D8", "FOR ANY ... SATISFIES ... END is false (not unknown) over an absent array: its NOT holds", lambda p: _eq(sorted(r["id"] for r in p.sql('SELECT "id" AS id FROM pc WHERE NOT (FOR ANY "e" IN "arr" SATISFIES "e" = CAST(:v AS BIGINT) END)', {"v": 1})), ["b", "c", "d"])),
    Fact("D9", "UNNEST of an array of objects, the element's fields as \"l\".\"f\"", lambda p: _eq([(r["src"], r["dst"], r["w"]) for r in p.sql('SELECT "id" AS src, "l"."to" AS dst, TO_BIGINT("l"."w") AS w FROM pc UNNEST "links" AS "l"')], [("a", "b", 3)])),
    Fact("D10", "UNNEST of an array of integers skips an absent array", lambda p: _eq(sorted((r["owner"], r["v"]) for r in p.sql('SELECT "id" AS owner, TO_BIGINT("x") AS v FROM pc UNNEST "arr" AS "x"')), [("a", 1), ("a", 2), ("d", 7)])),
    Fact("D11", "an array equals PARSE_JSON(?) of its canonical text", lambda p: _eq([r["id"] for r in p.sql('SELECT "id" AS id FROM pc WHERE "arr" = PARSE_JSON(:v)', {"v": "[1,2]"})], ["a"])),
    Fact("D12", "GROUP BY a path, COUNT(DISTINCT path); ORDER BY a path puts nulls first ascending and last descending", lambda p: _eq([r["id"] for r in p.sql('SELECT "id" AS id FROM pc ORDER BY "i" ASC, "id" ASC')][0], "c")),
    Fact("D13", "UPDATE ... UNSET $.\"g\" WHERE ... (standalone, as the provider emits it, not combined with SET); SET $.\"f\" = CAST(? AS BIGINT) with a CONSTANT value (the doc store allows only constant expressions in SET, so an arithmetic write binds the value computed per row, never `f = f + c`)", lambda p: (p.run('UPDATE pc SET $."i" = CAST(:v AS BIGINT) WHERE $."id" = CAST(:k AS NVARCHAR(256))', {"v": 15, "k": "a"}), p.run('UPDATE pc UNSET $."s" WHERE $."id" = CAST(:k AS NVARCHAR(256))', {"k": "a"}), (lambda d: (d["a"].get("i") == 15 and "s" not in d["a"], json.dumps(d["a"])))(_docs(p)))[-1]),
    Fact("D14", "UPDATE ... SET \"arr\" = PARSE_JSON(?) and SET pc.$ = PARSE_JSON(?) (the whole document)", lambda p: (p.run('UPDATE pc SET "links" = PARSE_JSON(:v) WHERE "id" = CAST(:k AS NVARCHAR(256))', {"v": "[]", "k": "a"}), p.run('UPDATE pc SET pc.$ = PARSE_JSON(:v) WHERE "id" = CAST(:k AS NVARCHAR(256))', {"v": '{"id": "c", "i": 4}', "k": "c"}), (lambda d: (d["a"].get("links") == [] and d["c"] == {"id": "c", "i": 4}, json.dumps([d["a"], d["c"]])))(_docs(p)))[-1]),
    Fact("D15", "CREATE HASH INDEX on a field; an UPDATE / DELETE answers its row count", lambda p: (p.run('CREATE HASH INDEX ix_pc_i ON pc("i")'), _eq(p.run('DELETE FROM pc WHERE "id" = CAST(:k AS NVARCHAR(256))', {"k": "c"}), 1))[-1]),
    Fact("D16", "a WITH view over a collection joins SQL; the boolean CASE lives one CTE removed from the collection (the doc store rejects a BOOLEAN constant in a query that reads a collection)", lambda p: _eq(p.one("WITH raw AS (SELECT \"id\" AS id, \"b\" AS b FROM pc), v AS (SELECT id, CASE b WHEN 'true' THEN TRUE WHEN 'false' THEN FALSE END AS b FROM raw) SELECT COUNT(*) AS v FROM v x JOIN v y ON x.id = y.id WHERE x.b = TRUE"), 2)),
    Fact("D17", "a collection's INSERT rolls back with the transaction", lambda p: (p.s._transaction(lambda: p.run("INSERT INTO pc VALUES(:doc)", {"doc": '{"id": "z"}'}), commit=False), _eq(p.one('SELECT COUNT(*) AS v FROM pc WHERE "id" = CAST(:k AS NVARCHAR(256))', {"k": "z"}), 0))[-1]),
    Fact("D18", "LIKE on a string field: its right-hand side is a constant string literal (the doc store refuses a parameter there), so the provider inlines the pattern", lambda p: _eq([r["id"] for r in p.sql('SELECT $."id" AS id FROM pc WHERE $."s" LIKE \'y%\'')], ["b"])),
]


# --- the triple store -----------------------------------------------------------------------


def k_setup(p: Probe):
    p.update(f"""INSERT DATA {{ GRAPH {G} {{
        <urn:ganda:r:a> a c:t ; p:id "a" ; p:i "1"^^xsd:long ; p:f "0.5"^^xsd:double ; p:s "x\\n\\"q\\"" ; p:b true ; p:l "[1,2]" ; i:l "1"^^xsd:long , "2"^^xsd:long ; l:e <urn:ganda:r:b> .
        <urn:ganda:r:b> a c:t ; p:id "b" ; p:i "9223372036854775807"^^xsd:long ; p:s "B" ; l:e <urn:ganda:r:c> .
        <urn:ganda:r:c> a c:t ; p:id "c" .
        <urn:ganda:r:e0> a c:e ; p:eid "e0" ; p:src <urn:ganda:r:a> ; p:dst <urn:ganda:r:b> ; p:w "3"^^xsd:long .
        <urn:ganda:r:e1> a c:e ; p:eid "e1" ; p:src <urn:ganda:r:a> ; p:dst <urn:ganda:r:b> .
    }} }}""")
    return True, "graph with 3 nodes and 2 parallel relationship resources"


K = [
    Fact("K0", "SPARQL_EXECUTE runs an INSERT DATA into a named graph (created on first insert)", k_setup),
    Fact("K1", "a SELECT answers JSON results; xsd:long, xsd:double, xsd:boolean and an escaped string read back", lambda p: (lambda r: (r["i"] == 1 and r["f"] == 0.5 and r["b"] is True and r["s"] == 'x\n"q"', repr(r)))(p.sparql(f"SELECT ?i ?f ?b ?s WHERE {{ GRAPH {G} {{ ?n p:id \"a\" ; p:i ?i ; p:f ?f ; p:b ?b ; p:s ?s }} }}")[0])),
    Fact("K2", "xsd:long keeps 2^63 - 1", lambda p: _eq(p.sparql(f"SELECT ?i WHERE {{ GRAPH {G} {{ ?n p:id \"b\" ; p:i ?i }} }}")[0]["i"], 9223372036854775807)),
    Fact("K3", "FROM <g> asks the graph as GRAPH <g> {} does", lambda p: _eq(p.ids(f"SELECT ?id FROM {G} WHERE {{ ?n a c:t ; p:id ?id }}"), p.ids(f"SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:t ; p:id ?id }} }}"))),
    Fact("K4", "two relationship resources between one pair are two solutions; their direct link one triple", lambda p: _eq((len(p.sparql(f"SELECT ?e WHERE {{ GRAPH {G} {{ ?e a c:e ; p:src <urn:ganda:r:a> }} }}")), len(p.sparql(f"SELECT ?t WHERE {{ GRAPH {G} {{ <urn:ganda:r:a> l:e ?t }} }}"))), (2, 1))),
    Fact("K5", "error propagation: `!((?u = 1) && false)` holds for an unbound ?u (the `native` variants rest on it)", lambda p: _eq(p.ids(f"SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:t ; p:id ?id . OPTIONAL {{ ?n p:i ?u }} FILTER (!((?u = 1) && false)) }} }}"), ["a", "b", "c"])),
    Fact("K6", "a never-bound variable DECLARED by a never-matching OPTIONAL stays unbound: `IF(BOUND(?u), EXISTS {...}, ?decl)` propagates the error for an unbound ?u so a NOT of it keeps the record out (HANA rejects a FILTER naming a variable bound nowhere, so the provider declares it)", lambda p: _eq(p.ids(f"SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:t ; p:id ?id . OPTIONAL {{ ?n p:i ?u }} OPTIONAL {{ ?n <urn:ganda:never> ?ganda_unknown }} FILTER (!(IF(BOUND(?u), EXISTS {{ ?n p:s ?z }}, ?ganda_unknown))) }} }}"), [])),
    Fact("K7", "MAX / COUNT skip an unbound value; COUNT over no solution is 0", lambda p: _eq([(r["m"], r["c"]) for r in p.sparql(f"SELECT (MAX(?i) AS ?m) (COUNT(?i) AS ?c) WHERE {{ GRAPH {G} {{ ?n a c:t . OPTIONAL {{ ?n p:i ?i }} }} }}")], [(9223372036854775807, 2)])),
    Fact("K8", "SUM over no value is 0 (the renderings guard it with IF(COUNT(?w) > 0, ...))", lambda p: _eq(p.sparql(f"SELECT (SUM(?i) AS ?v) WHERE {{ GRAPH {G} {{ ?n a c:none ; p:i ?i }} }}")[0].get("v"), 0), required=False),
    Fact("K9", "a GROUP BY over no solution answers no row (HAVING (COUNT(*) > 0) guards it anyway)", lambda p: _eq(p.sparql(f"SELECT ?k (COUNT(*) AS ?v) WHERE {{ GRAPH {G} {{ ?n a c:none ; p:i ?k }} }} GROUP BY ?k"), []), required=False),
    Fact("K10", "property paths: ?, /, ^, |", lambda p: _eq(p.ids(f"SELECT DISTINCT ?id WHERE {{ GRAPH {G} {{ <urn:ganda:r:a> l:e?/l:e? ?r . ?r p:id ?id }} }}"), ["a", "b", "c"])),
    Fact("K11", "MINUS, FILTER NOT EXISTS", lambda p: _eq(p.ids(f"SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:t ; p:id ?id MINUS {{ ?n l:e ?x }} }} }}"), ["c"])),
    Fact("K12", "SPARQL_TABLE runs a SELECT inside SQL, one column per variable", lambda p: _eq(sorted(r["id"] for r in p.sql(f"SELECT * FROM SPARQL_TABLE('{PREFIX} SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:t ; p:id ?id }} }}')")), ["a", "b", "c"])),
    Fact("K13", "SPARQL_TABLE types an xsd:long column as a number", lambda p: (lambda r: (isinstance(r[0]["v"], int), repr(r)))(p.sql(f"SELECT * FROM SPARQL_TABLE('{PREFIX} SELECT ?v WHERE {{ GRAPH {G} {{ ?n p:id \"a\" ; p:i ?v }} }}')"))),
    Fact("K14", "DELETE / INSERT ... WHERE with OPTIONAL, STRDT, DELETE WHERE", lambda p: (p.update(f"DELETE {{ GRAPH {G} {{ ?n p:i ?o }} }} INSERT {{ GRAPH {G} {{ ?n p:i ?new }} }} WHERE {{ GRAPH {G} {{ ?n p:id \"a\" ; p:i ?o . BIND (STRDT(STR(?o + 1), xsd:long) AS ?new) }} }}"), p.update(f"DELETE WHERE {{ GRAPH {G} {{ <urn:ganda:r:e1> ?p ?o }} }}"), _eq((p.sparql(f"SELECT ?i WHERE {{ GRAPH {G} {{ ?n p:id \"a\" ; p:i ?i }} }}")[0]["i"], len(p.sparql(f"SELECT ?e WHERE {{ GRAPH {G} {{ ?e a c:e }} }}"))), (2, 1)))[-1]),
    Fact("K15", "a SPARQL update inside a SQL transaction rolls back with it", lambda p: (p.s._transaction(lambda: p.update(f"INSERT DATA {{ GRAPH {G} {{ <urn:ganda:r:z> a c:t ; p:id \"z\" }} }}"), commit=False), _eq(p.ids(f"SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:t ; p:id ?id }} }}"), ["a", "b", "c"]))[-1]),
    Fact("K16", "MEDIAN is a SPARQL aggregate, but only with a GROUP BY (an internal error without one), so the renderings group on a bound constant; it interpolates an even count the way the reference does (of 1, 2, 3: 2)", lambda p: _eq(p.sparql(f"SELECT (MEDIAN(?w) AS ?v) WHERE {{ GRAPH {G} {{ ?e p:w ?w . BIND(1 AS ?mg) }} }} GROUP BY ?mg")[0]["v"], 3)),
    Fact("K17", "ORDER BY puts an unbound key first ascending; strings in code point order (BMP)", lambda p: _eq([r["id"] for r in p.sparql(f"SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:t ; p:id ?id . OPTIONAL {{ ?n p:s ?s }} }} }} ORDER BY ASC(?s) ASC(?id)")], ["c", "b", "a"])),
    Fact("K19", "an EXISTS inside GRAPH in an update's WHERE reads the named graph (the rdf write marks its records so)", lambda p: (p.update(f"INSERT {{ GRAPH {G} {{ ?n <urn:ganda:w> true }} }} WHERE {{ GRAPH {G} {{ ?n a c:t . FILTER (EXISTS {{ ?n i:l \"1\"^^xsd:long }}) }} }}"), _eq(p.ids(f"SELECT ?id WHERE {{ GRAPH {G} {{ ?n <urn:ganda:w> true ; p:id ?id }} }}"), ["a"]), p.update(f"DELETE WHERE {{ GRAPH {G} {{ ?n <urn:ganda:w> ?x }} }}"))[1]),
    Fact("K18", "UCASE, LCASE, STRLEN, SUBSTR, CONCAT, ABS, STRSTARTS, STRENDS, CONTAINS, REGEX", lambda p: _eq(p.sparql('SELECT (CONCAT(UCASE("ab"), LCASE("C"), SUBSTR("xyz", 1, 1), STR(STRLEN("abc")), STR(ABS(-2))) AS ?v) WHERE { FILTER (STRSTARTS("abc", "a") && STRENDS("abc", "c") && CONTAINS("abc", "b") && REGEX("abc", "^a")) }')[0]["v"], "ABcx32")),
]

FACTS = R + D + K


def main(argv=None) -> int:
    from ganda.hana.connection import HanaServer

    wanted = list(argv if argv is not None else sys.argv[1:])
    facts = [f for f in FACTS if not wanted or any(f.id == w or f.id.startswith(w) and w.isalpha() for w in wanted)]
    failed = 0
    with HanaServer() as server:
        sessions = {}
        for kind in ("rel", "doc", "rdf"):
            sessions[kind] = server.fresh_session(f"fz_probe_{kind}")
        try:
            for f in facts:
                p = Probe(sessions[{"R": "rel", "D": "doc", "K": "rdf"}[f.id[0]]])
                try:
                    ok, detail = f.check(p)
                except Exception as e:  # noqa: BLE001 -- the fact did not hold: say how
                    ok, detail = False, f"{type(e).__name__}: {str(e)[:300]}"
                    if not isinstance(e, EngineError):
                        detail += "\n" + traceback.format_exc(limit=2)
                mark = "ok  " if ok else ("FAIL" if f.required else "obs ")
                if not ok and f.required:
                    failed += 1
                print(f"{mark} {f.id:4s} {f.text}\n          {detail}")
        finally:
            for kind in sessions:
                try:
                    server.drop_database(f"fz_probe_{kind}")
                except Exception:  # noqa: BLE001
                    pass
    print(f"{len(facts) - failed}/{len(facts)} facts hold" + (f"; {failed} REQUIRED facts fail: see HANDOFF.md, 'when a probe fact fails'" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
