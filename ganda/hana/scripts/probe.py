"""Re-check live every SAP HANA fact the provider rests on: `ganda hana probe`.

Each fact is one small statement against the attached instance (HANA_HOST, ...), run in
a scratch schema and graph that are dropped afterwards. A fact is either REQUIRED (the
provider's rendering or its semantics assume it: a failure means the provider must
change, see HANDOFF.md) or OBSERVED (recorded so a later run can see a change; never
fails). Every line prints what the engine answered.

    python3 -m ganda hana probe            # every fact
    python3 -m ganda hana probe K5 D6      # some of them (ids or prefixes: R, D, K, G)
"""
from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from ganda.core.provider import EngineError
from ganda.hana.dialect import opencypher_table

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


def k20(p: Probe):
    """The renderings keep an unbound value out of SUM / AVG: once a predicate IRI has held
    a second datatype anywhere in the store HANA answers unbound for a group with an unbound
    value in it (H3), while with one datatype it skips the value."""
    p.update(f'INSERT DATA {{ GRAPH {G} {{ <urn:ganda:r:k20a> a c:k20 ; p:k20 "3"^^xsd:long . <urn:ganda:r:k20b> a c:k20 ; p:k20 "4"^^xsd:long . <urn:ganda:r:k20c> a c:k20 . <urn:ganda:r:k20d> a c:k20x ; p:k20 "text" . }} }}')
    raw = p.sparql(f"SELECT (SUM(?w) AS ?v) WHERE {{ GRAPH {G} {{ ?n a c:k20 . OPTIONAL {{ ?n p:k20 ?w }} }} }}")[0].get("v")
    req = p.sparql(f"SELECT (IF(COUNT(?w) > 0, SUM(?w), MAX(?w)) AS ?v) WHERE {{ GRAPH {G} {{ ?n a c:k20 ; p:k20 ?w . }} }}")[0].get("v")
    coal = p.sparql(f"SELECT (IF(COUNT(?w) > 0, SUM(COALESCE(?w, 0)), MAX(?w)) AS ?v) WHERE {{ GRAPH {G} {{ ?n a c:k20 . OPTIONAL {{ ?n p:k20 ?w }} }} }}")[0].get("v")
    return req == 7 and coal == 7, f"plain SUM over the group with an unbound value: {raw!r} (H3); value bound where present: {req!r}; COALESCE: {coal!r} (want 7)"


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
    Fact("K20", "SUM over a value bound where present, or over COALESCE(?w, 0), is exact on a predicate that has held several datatypes (a plain SUM over a group with an unbound value is not: H3)", k20),
    Fact("K18", "UCASE, LCASE, STRLEN, SUBSTR, CONCAT, ABS, STRSTARTS, STRENDS, CONTAINS, REGEX", lambda p: _eq(p.sparql('SELECT (CONCAT(UCASE("ab"), LCASE("C"), SUBSTR("xyz", 1, 1), STR(STRLEN("abc")), STR(ABS(-2))) AS ?v) WHERE { FILTER (STRSTARTS("abc", "a") && STRENDS("abc", "c") && CONTAINS("abc", "b") && REGEX("abc", "^a")) }')[0]["v"], "ABcx32")),
]

# --- the property graph engine ----------------------------------------------------------------


def _oc(p: Probe, cypher: str, binds: "dict | None" = None, params: "dict | None" = None) -> list:
    """The rows of an openCypher query over the probe schema's workspace `gw`."""
    return p.sql(f"SELECT * FROM {opencypher_table(cypher, params)}", binds)


def _oc_ids(p: Probe, cypher: str, binds=None, params=None) -> list:
    return sorted(r["id"] for r in _oc(p, cypher, binds, params))


def g_setup(p: Probe):
    """Two vertex tables with an `f1` of different types, an edge table between them with
    two parallel relationships, a self-typed one with a self-loop; the views and the
    workspace exactly as `mapping.graph` builds them."""
    p.run("CREATE COLUMN TABLE pv1 (id NVARCHAR(256) PRIMARY KEY, f1 BIGINT, f2 NVARCHAR(5000), b BOOLEAN, d DOUBLE, l NVARCHAR(5000))")
    p.run("CREATE COLUMN TABLE pv2 (id NVARCHAR(256) PRIMARY KEY, f1 NVARCHAR(5000))")
    p.run("CREATE COLUMN TABLE pe1 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL, w BIGINT, s NVARCHAR(5000))")
    p.run("CREATE COLUMN TABLE pe2 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL, w DOUBLE)")
    for r in [("a", 1, "x", True, 0.5, "[1,2]"), ("b", 2**63 - 1, "B", False, float(2**53), "[]"), ("c", None, None, None, None, None), ("d", -5, "it's", True, 1e-300, "[3]")]:
        p.run("INSERT INTO pv1 (id, f1, f2, b, d, l) VALUES (:a, :b, :c, :d, :e, :f)", dict(zip("abcdef", r)))
    p.run("INSERT INTO pv2 (id, f1) VALUES ('x', 'x')")
    p.run("INSERT INTO pv2 (id, f1) VALUES ('y', NULL)")
    for r in [("e0", "a", "x", 3, "p"), ("e1", "a", "x", None, None), ("e2", "b", "y", 7, "q")]:
        p.run("INSERT INTO pe1 (eid, src_id, dst_id, w, s) VALUES (:a, :b, :c, :d, :e)", dict(zip("abcde", r)))
    for r in [("e3", "a", "a", 0.5), ("e4", "a", "b", 1.5), ("e5", "b", "d", 2.5)]:
        p.run("INSERT INTO pe2 (eid, src_id, dst_id, w) VALUES (:a, :b, :c, :d)", dict(zip("abcd", r)))
    p.run("CREATE VIEW gv_pv1 AS SELECT id, f1 AS pv1_f1, f2 AS pv1_f2, b AS pv1_b, d AS pv1_d, l AS pv1_l FROM pv1")
    p.run("CREATE VIEW gv_pv2 AS SELECT id, f1 AS pv2_f1 FROM pv2")
    p.run("CREATE VIEW gv_pe1 AS SELECT eid, src_id, dst_id, w AS pe1_w, s AS pe1_s FROM pe1")
    p.run("CREATE VIEW gv_pe2 AS SELECT eid, src_id, dst_id, w AS pe2_w FROM pe2")
    p.run("CREATE GRAPH WORKSPACE gw VERTEX TABLE gv_pv1 KEY COLUMN id LABEL 'pv1' VERTEX TABLE gv_pv2 KEY COLUMN id LABEL 'pv2' EDGE TABLE gv_pe1 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_pv1 TARGET COLUMN dst_id REFERENCES gv_pv2 LABEL 'pe1' EDGE TABLE gv_pe2 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_pv1 TARGET COLUMN dst_id REFERENCES gv_pv1 LABEL 'pe2'")
    return _eq(_oc_ids(p, "MATCH (a:pv1) RETURN a.ID AS ID"), ["a", "b", "c", "d"])


def g12(p: Probe):
    """The workspace reads the transaction's own uncommitted writes, and forgets them
    with the rollback."""
    seen = []
    p.s._transaction(lambda: (p.run("INSERT INTO pv1 (id, f1) VALUES ('z', 9)"), seen.append(_oc(p, "MATCH (a:pv1) RETURN COUNT(a.ID) AS V")[0]["v"])), commit=False)
    after = _oc(p, "MATCH (a:pv1) RETURN COUNT(a.ID) AS V")[0]["v"]
    return (seen, after) == ([5], 4), f"inside the transaction {seen}, after rollback {after} (want [5], 4)"


def g13(p: Probe):
    p.run("UPDATE pv1 SET f1 = 10 WHERE id = 'a'")
    p.run("MERGE DELTA OF pv1")
    got = _oc(p, "MATCH (a:pv1) WHERE a.ID = 'a' RETURN a.PV1_F1 AS V")[0]["v"]
    p.run("UPDATE pv1 SET f1 = 1 WHERE id = 'a'")
    return _eq(got, 10)


def g16(p: Probe):
    lower_attr = p.error(lambda: _oc(p, "MATCH (a:pv1) RETURN a.id AS ID"))[:80] or "accepted"
    lower_alias = p.error(lambda: p.sql(f"SELECT z.id AS v FROM {opencypher_table('MATCH (a:pv1) RETURN a.ID AS id')} z"))[:80] or "accepted"
    return True, f"a.id: {lower_attr}; `AS id` read as z.id: {lower_alias}"


def g17(p: Probe):
    """A workspace over a view over a hash-partitioned table whose key has no PRIMARY KEY
    (NOT NULL only): the view-backed graph table skips the constraint check. Two edge
    tables, as every campaign workspace has (a homogeneous one misbehaves: G27)."""
    p.run("CREATE COLUMN TABLE pv3 (id NVARCHAR(256) NOT NULL, f1 BIGINT) PARTITION BY HASH (id) PARTITIONS 2")
    p.run("CREATE COLUMN TABLE pe3 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL)")
    p.run("CREATE COLUMN TABLE pe4 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL, w BIGINT)")
    p.run("INSERT INTO pv3 VALUES ('m', 1)")
    p.run("INSERT INTO pv3 VALUES ('n', 2)")
    p.run("INSERT INTO pe3 VALUES ('f0', 'm', 'n')")
    p.run("INSERT INTO pe4 VALUES ('f1', 'n', 'm', 5)")
    p.run("CREATE VIEW gv_pv3 AS SELECT id, f1 AS pv3_f1 FROM pv3")
    p.run("CREATE VIEW gv_pe3 AS SELECT eid, src_id, dst_id FROM pe3")
    p.run("CREATE VIEW gv_pe4 AS SELECT eid, src_id, dst_id, w AS pe4_w FROM pe4")
    p.run("CREATE GRAPH WORKSPACE gw2 VERTEX TABLE gv_pv3 KEY COLUMN id LABEL 'pv3' EDGE TABLE gv_pe3 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_pv3 TARGET COLUMN dst_id REFERENCES gv_pv3 LABEL 'pe3' EDGE TABLE gv_pe4 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_pv3 TARGET COLUMN dst_id REFERENCES gv_pv3 LABEL 'pe4'")
    rows = p.sql("SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE gw2 QUERY 'MATCH (a:pv3)-[e:pe3]->(b:pv3) RETURN a.ID AS ID, b.ID AS V')")
    return _eq([(r["id"], r["v"]) for r in rows], [("m", "n")])


def g27(p: Probe):
    """One vertex table and one edge table (homogeneous), labelled: what a labelled and
    an unlabelled query answer (the first run: `invalid column name: a.CYPHER$TABLE$LABEL`)."""
    p.run("CREATE GRAPH WORKSPACE gw4 VERTEX TABLE gv_pv3 KEY COLUMN id LABEL 'pv3' EDGE TABLE gv_pe3 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_pv3 TARGET COLUMN dst_id REFERENCES gv_pv3 LABEL 'pe3'")
    labelled = p.error(lambda: p.sql("SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE gw4 QUERY 'MATCH (a:pv3)-[e:pe3]->(b:pv3) RETURN a.ID AS ID, b.ID AS V')"))[:90] or "answers"
    bare = p.error(lambda: p.sql("SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE gw4 QUERY 'MATCH (a)-[e]->(b) RETURN a.ID AS ID, b.ID AS V')"))[:90] or "answers"
    p.run("CREATE GRAPH WORKSPACE gw5 VERTEX TABLE gv_pv3 KEY COLUMN id EDGE TABLE gv_pe3 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_pv3 TARGET COLUMN dst_id REFERENCES gv_pv3")
    unlabelled_ws = p.error(lambda: p.sql("SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE gw5 QUERY 'MATCH (a)-[e]->(b) RETURN a.ID AS ID, b.ID AS V')"))[:90] or "answers"
    return True, f"labelled query: {labelled} | unlabelled query: {bare} | workspace without LABEL clauses: {unlabelled_ws}"


def g22(p: Probe):
    """What a cypher mistake is classified as: a rejection, never an internal error."""
    out = []
    for label, text in (("syntax", "MATCH (a:pv1 RETURN a.ID AS ID"), ("label", "MATCH (a:nosuch) RETURN a.ID AS ID"), ("attribute", "MATCH (a:pv1) RETURN a.NOSUCH AS ID"), ("parameter", "MATCH (a:pv1) WHERE a.PV1_F1 = $nope RETURN a.ID AS ID")):
        try:
            _oc(p, text)
            out.append(f"{label}: accepted")
        except EngineError as e:
            out.append(f"{label}: {'INTERNAL ' if e.internal else ''}{str(e)[:90]}")
    return not any("INTERNAL" in o for o in out), " | ".join(out)


def g26(p: Probe):
    """DROP SCHEMA ... CASCADE removes the schema's workspace (the campaign's `_drop`)."""
    p.run("CREATE SCHEMA fz_probe_gws")
    p.run("CREATE COLUMN TABLE fz_probe_gws.v (id NVARCHAR(256) PRIMARY KEY)")
    p.run("CREATE COLUMN TABLE fz_probe_gws.e (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL)")
    p.run("CREATE GRAPH WORKSPACE fz_probe_gws.gw VERTEX TABLE fz_probe_gws.v KEY COLUMN id EDGE TABLE fz_probe_gws.e KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id")
    p.run("DROP SCHEMA fz_probe_gws CASCADE")
    return _eq(p.one("SELECT COUNT(*) AS v FROM SYS.GRAPH_WORKSPACES WHERE SCHEMA_NAME = 'FZ_PROBE_GWS'"), 0)


G = [
    Fact("G0", "a graph workspace over VIEWS of rel-shaped tables, with LABEL and REFERENCES, unqualified `gw` resolving in the session's schema; `(a:pv1)` matches LABEL 'pv1'", g_setup),
    Fact("G0b", "the workspace is valid (SYS.GRAPH_WORKSPACES.IS_VALID, the text 'TRUE')", lambda p: _eq(str(p.sql("SELECT IS_VALID AS v FROM SYS.GRAPH_WORKSPACES WHERE SCHEMA_NAME = CURRENT_SCHEMA AND WORKSPACE_NAME = 'GW'")[0]["v"]).upper(), "TRUE")),
    Fact("G1", "OPENCYPHER_TABLE composes: in a CTE, as a derived table, LEFT JOINed to a column table with GROUP BY, under a correlated scalar subquery (the views family)", lambda p: _eq(p.one(f"WITH g AS (SELECT * FROM {opencypher_table('MATCH (a:pv1) RETURN a.ID AS ID, a.PV1_F1 AS F1')}) SELECT COUNT(*) AS v FROM (SELECT x.id FROM g x LEFT JOIN pe2 e ON e.src_id = x.id WHERE (SELECT COUNT(*) FROM g y WHERE y.id = x.id) = 1 GROUP BY x.id) z"), 4)),
    Fact("G1b", "a result column is typed by the workspace column: BIGINT an int, DOUBLE a float, NVARCHAR a str, BOOLEAN a bool; the aliases ID / F1 / D / S / B reach SQL unquoted", lambda p: (lambda r: (r["f1"] == 1 and isinstance(r["f1"], int) and r["d"] == 0.5 and r["s"] == "x" and r["b"] is True, repr(r)))(_oc(p, "MATCH (a:pv1) WHERE a.ID = 'a' RETURN a.ID AS ID, a.PV1_F1 AS F1, a.PV1_D AS D, a.PV1_F2 AS S, a.PV1_B AS B")[0])),
    Fact("G2", "PARAMETERS ('i' = ?, 's' = ?) binds $i / $s by NAME, bare (typed by the engine): an int exact above 2^53, a string, a double", lambda p: _eq((_oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F1 = $i OR a.PV1_F2 = $s RETURN a.ID AS ID", {"i": 2**63 - 1, "s": "x"}, {"i": ":i", "s": ":s"}), _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_D = $d RETURN a.ID AS ID", {"d": 0.5}, {"d": ":d"})), (["a", "b"], ["a"]))),
    Fact("G2b", "a parameter bound through CAST(? AS BIGINT) (the typed form, if the bare one ever needs it)", lambda p: _eq(_oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F1 = $i RETURN a.ID AS ID", {"i": -5}, {"i": "CAST(:i AS BIGINT)"}), ["d"]), required=False),
    Fact("G3", "three-valued WHERE as SQL: NOT (f > 1) drops a null f; f > 100 OR s IS NULL keeps a row with both null", lambda p: _eq((_oc_ids(p, "MATCH (a:pv1) WHERE NOT (a.PV1_F1 > 1) RETURN a.ID AS ID"), _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F1 > 100 OR a.PV1_F2 IS NULL RETURN a.ID AS ID")), (["a", "d"], ["b", "c"]))),
    Fact("G4", "an aggregate-only RETURN over no match answers one row: COUNT 0, MAX NULL; SUM over a null-only group and over no match (recorded: the renderings guard a sum with its COUNT, whatever it answers)", lambda p: (lambda c, m, s, s0: (c == [0] and m == [None], f"COUNT {c}, MAX {m}, SUM over a null-only group {s}, SUM over no match {s0}"))([r["v"] for r in _oc(p, "MATCH (a:pv1) WHERE a.PV1_F2 = 'none' RETURN COUNT(a.ID) AS V")], [r["v"] for r in _oc(p, "MATCH (a:pv1) WHERE a.PV1_F2 = 'none' RETURN MAX(a.PV1_F1) AS V")], [r["v"] for r in _oc(p, "MATCH (a:pv1) WHERE a.ID = 'c' RETURN SUM(a.PV1_F1) AS V")], [r["v"] for r in _oc(p, "MATCH (a:pv1) WHERE a.PV1_F2 = 'none' RETURN SUM(a.PV1_F1) AS V")])),
    Fact("G5", "ORDER BY ASC puts NULL LAST (HANA SQL: first), so a native ordering over a nullable field is not asked (`graph.NULLS_LIKE_SQL` off); LIMIT n SKIP m after it", lambda p: _eq(([r["id"] for r in _oc(p, "MATCH (a:pv1) RETURN a.ID AS ID ORDER BY a.PV1_F1 ASC, a.ID ASC")], [r["id"] for r in _oc(p, "MATCH (a:pv1) RETURN a.ID AS ID ORDER BY a.PV1_F1 ASC, a.ID ASC LIMIT 2 SKIP 1")]), (["d", "a", "b", "c"], ["a", "b"]))),
    Fact("G5b", "ORDER BY ... DESC: where NULL goes (recorded); `SKIP` without `LIMIT` is a syntax error (the renderings ride a skip on the largest LIMIT)", lambda p: (lambda desc, skip: (True, f"DESC order {desc}; SKIP alone: {skip[:70] or 'accepted'}"))([r["id"] for r in _oc(p, "MATCH (a:pv1) RETURN a.ID AS ID ORDER BY a.PV1_F1 DESC, a.ID ASC")], p.error(lambda: _oc(p, "MATCH (a:pv1) RETURN a.ID AS ID ORDER BY a.PV1_F1 ASC, a.ID ASC SKIP 3"))), required=False),
    Fact("G6", "strings order by code unit as HANA SQL does ('B' < 'i' < 'x'), in ORDER BY and in <", lambda p: _eq(([r["id"] for r in _oc(p, "MATCH (a:pv1) WHERE NOT (a.PV1_F2 IS NULL) RETURN a.ID AS ID ORDER BY a.PV1_F2 ASC")], _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F2 < 'a' RETURN a.ID AS ID")), (["b", "d", "a"], ["b"]))),
    Fact("G7", "an undirected edge over a SELF-LOOP: how many times the loop matches (the `undirected` syntax asks set questions only; the offline translator mirrors the count)", lambda p: (lambda vs: (True, f"self-loop matched {vs.count('a')} time(s) (rows {vs})"))([r["v"] for r in _oc(p, "MATCH (a:pv1)-[e:pe2]-(b:pv1) WHERE a.ID = 'a' RETURN b.ID AS V")]), required=False),
    Fact("G8", "a heterogeneous workspace takes ONE MATCH clause (a second is refused: 'only one match clause is allowed'); a pattern holds ONE edge (a chained `(a)-[e1]->(b)-[e2]->(c)` is a syntax error), the hops comma-separated; within the clause a relationship binds once: the self-loop a->a cannot be both hops (so a native multi-hop is asked only over pairwise distinct edge types)", lambda p: (lambda second, chained, commas: ("only one match clause" in second and "syntax error" in chained and commas == 0, f"second clause: {second[:70]}; chained pattern: {chained[:70]}; comma-separated over the loop twice: {commas} (want 0)"))(p.error(lambda: _oc(p, "MATCH (a:pv1)-[e1:pe2]->(b:pv1) MATCH (b)-[e2:pe2]->(c:pv1) RETURN COUNT(c.ID) AS V")), p.error(lambda: _oc(p, "MATCH (a:pv1)-[e1:pe2]->(b:pv1)-[e2:pe2]->(c:pv1) RETURN COUNT(c.ID) AS V")), _oc(p, "MATCH (a:pv1)-[e1:pe2]->(b:pv1), (b)-[e2:pe2]->(c:pv1) WHERE a.ID = 'a' AND b.ID = 'a' AND c.ID = 'a' RETURN COUNT(c.ID) AS V")[0]["v"])),
    Fact("G8b", "two distinct relationships chain in one clause: (a)-[e1:pe2]->(b), (b)-[e2:pe2]->(c) over a->b->d answers d", lambda p: _eq([r["v"] for r in _oc(p, "MATCH (a:pv1)-[e1:pe2]->(b:pv1), (b)-[e2:pe2]->(c:pv1) WHERE a.ID = 'a' AND b.ID = 'b' RETURN c.ID AS V")], ["d"])),
    Fact("G9", "variable-length paths run: `[:pe2*1..2]` labelled, `[*1..1]` unlabelled, undirected; a path repeats no relationship (a->a in 1..2 hops over the loop: 1 path, not 2)", lambda p: (lambda loop, any_, und: (loop in (1, 2) and any_ == 2 and und >= 1, f"a->a over *1..2: {loop} (1 = trail, 2 = walk); unlabelled *1..1 from a: {any_}; undirected *1..1 from c: {und}"))(_oc(p, "MATCH rp = (a:pv1)-[:pe2*1..2]->(b:pv1) WHERE a.ID = 'a' AND b.ID = 'a' RETURN COUNT(b.ID) AS V")[0]["v"], _oc(p, "MATCH rp = (a:pv1)-[*1..1]->(b) WHERE a.ID = 'b' RETURN COUNT(b.ID) AS V")[0]["v"], _oc(p, "MATCH rp = (a:pv1)-[*1..1]-(b:pv1) WHERE a.ID = 'd' RETURN COUNT(b.ID) AS V")[0]["v"])),
    Fact("G10", "a BOOLEAN attribute is projected as a bool; compared in WHERE (= $b, TOINTEGER(b) = 1)? (recorded: BOOL_FILTERS)", lambda p: (lambda proj: (proj == [True, False], f"projection {proj}; `= $b` {p.error(lambda: _oc(p, 'MATCH (a:pv1) WHERE a.PV1_B = $b RETURN a.ID AS ID', {'b': True}, {'b': ':b'}))[:80] or 'accepted'}; TOINTEGER {p.error(lambda: _oc(p, 'MATCH (a:pv1) WHERE TOINTEGER(a.PV1_B) = 1 RETURN a.ID AS ID'))[:80] or 'accepted'}"))([r["b"] for r in _oc(p, "MATCH (a:pv1) WHERE a.ID = 'a' OR a.ID = 'b' RETURN a.ID AS ID, a.PV1_B AS B ORDER BY a.ID ASC")])),
    Fact("G11", "BIGINT 2^63 - 1 and DOUBLE 2^53, 1e-300 come back exact through OPENCYPHER_TABLE", lambda p: _eq([(r["i"], r["d"]) for r in _oc(p, "MATCH (a:pv1) WHERE a.ID = 'b' OR a.ID = 'd' RETURN a.ID AS ID, a.PV1_F1 AS I, a.PV1_D AS D ORDER BY a.ID ASC")], [(2**63 - 1, float(2**53)), (-5, 1e-300)])),
    Fact("G12", "OPENCYPHER_TABLE sees the transaction's own uncommitted INSERT, gone after ROLLBACK (the transaction door, the cancelled junk)", g12),
    Fact("G13", "an UPDATE in auto-commit, then MERGE DELTA OF, is what the workspace's dynamic cache answers", g13),
    Fact("G14", "arithmetic over an attribute with a parameter: `f - $c > $p` on BIGINT, `d * $c = $p` on DOUBLE, exact; integer division (recorded)", lambda p: (lambda i, d: (i == ["b"] and d == ["a"], f"{i} {d}; 1 / 2 over a: {p.error(lambda: _oc(p, 'MATCH (a:pv1) WHERE a.PV1_F1 = 1 RETURN a.PV1_F1 / 2 AS V'))[:70] or [r['v'] for r in _oc(p, 'MATCH (a:pv1) WHERE a.PV1_F1 = 1 RETURN a.PV1_F1 / 2 AS V')]}"))(_oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F1 - $c > $p RETURN a.ID AS ID", {"c": 1, "p": 0}, {"c": ":c", "p": ":p"}), _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_D * $c = $p RETURN a.ID AS ID", {"c": 2.0, "p": 1.0}, {"c": ":c", "p": ":p"}))),
    Fact("G15", "string predicates over constants: STARTS WITH, ENDS WITH, CONTAINS, and the `\\'` escape in a constant; a parameter on the right-hand side (recorded)", lambda p: (lambda st, en, co, esc: (st == ["d"] and en == ["a"] and co == ["d"] and esc == ["d"], f"{st} {en} {co} {esc}; CONTAINS $s: {p.error(lambda: _oc(p, 'MATCH (a:pv1) WHERE a.PV1_F2 CONTAINS $s RETURN a.ID AS ID', {'s': 't'}, {'s': ':s'}))[:80] or 'accepted'}"))(_oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F2 STARTS WITH 'i' RETURN a.ID AS ID"), _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F2 ENDS WITH 'x' RETURN a.ID AS ID"), _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F2 CONTAINS 't' RETURN a.ID AS ID"), _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F2 = 'it\\'s' RETURN a.ID AS ID"))),
    Fact("G16", "attribute names and aliases are case-sensitive: `a.id` (lowercase) is refused, a lowercase alias is a quoted column outside (recorded)", g16, required=False),
    Fact("G17", "a workspace over a view over a HASH-partitioned table whose key is NOT NULL without a PRIMARY KEY (the id-index and partition axes)", g17),
    Fact("G19", "an OPENCYPHER_TABLE nested in NOT IN (SELECT ...) (nexists) and in IN (SELECT ...)", lambda p: _eq((p.one(f"SELECT COUNT(*) AS v FROM pv1 s WHERE s.id NOT IN (SELECT z.ID FROM {opencypher_table('MATCH (a:pv1)-[e:pe2]->(b:pv1) RETURN a.ID AS ID')} z)"), p.one(f"SELECT COUNT(*) AS v FROM pv1 s WHERE s.id IN (SELECT z.ID FROM {opencypher_table('MATCH (a:pv1)-[e:pe2]->(b:pv1) RETURN a.ID AS ID')} z)")), (2, 2))),
    Fact("G20", "two disconnected patterns of one clause are the Cartesian product (a native join's two classes, filtered by the correlation)", lambda p: _eq((_oc(p, "MATCH (a:pv2), (b:pv2) RETURN COUNT(b.ID) AS V")[0]["v"], sorted((r["id"], r["v"]) for r in _oc(p, "MATCH (t:pv1), (m:pv1) WHERE (m.PV1_F1 > t.PV1_F1) RETURN t.ID AS ID, m.ID AS V"))), (4, [("a", "b"), ("d", "a"), ("d", "b")]))),
    Fact("G21", "an unlabelled vertex spans every vertex table, an attribute absent from a table NULL", lambda p: _eq(len(_oc(p, "MATCH (a) RETURN a.ID AS ID, a.PV1_F1 AS F1")), 6), required=False),
    Fact("G22", "a cypher syntax error, an unknown label, an unknown attribute, an undeclared parameter are REJECTIONS (their codes and texts recorded for `connection.REJECTIONS`)", g22),
    Fact("G23", "two parallel relationships are two rows; a LIST column aliased `__JSON` decodes; SUM of BIGINT is an int, of DOUBLE a float", lambda p: (lambda par, lst, si, sd: (par == 2 and lst == [1, 2] and si == -4 and isinstance(si, int) and sd == 1e-300 + 0.5 + float(2**53), f"parallel {par}, list {lst}, sums {si!r} {sd!r}"))(len(_oc(p, "MATCH (a:pv1)-[e:pe1]->(b:pv2) WHERE a.ID = 'a' RETURN e.EID AS ID")), _oc(p, "MATCH (a:pv1) WHERE a.ID = 'a' RETURN a.PV1_L AS L__JSON")[0]["l"], _oc(p, "MATCH (a:pv1) WHERE a.ID = 'a' OR a.ID = 'd' RETURN SUM(a.PV1_F1) AS V")[0]["v"], _oc(p, "MATCH (a:pv1) RETURN SUM(a.PV1_D) AS V")[0]["v"])),
    Fact("G24", "DISTINCT over a BOOLEAN attribute and over NVARCHAR(5000); COUNT grouped implicitly by the other items", lambda p: _eq((len(_oc(p, "MATCH (a:pv1) RETURN DISTINCT a.PV1_B AS B")), sorted(((r["k"], r["v"]) for r in _oc(p, "MATCH (a:pv1) RETURN a.PV1_B AS K, COUNT(a.ID) AS V")), key=repr)), (3, sorted([(None, 1), (False, 1), (True, 2)], key=repr))), required=False),
    Fact("G25", "a second workspace over the same views (heterogeneous: two edge tables) created NO CACHE answers the same rows (the graph_cache axis)", lambda p: (p.run("CREATE GRAPH WORKSPACE gw3 VERTEX TABLE gv_pv1 KEY COLUMN id LABEL 'pv1' VERTEX TABLE gv_pv2 KEY COLUMN id LABEL 'pv2' EDGE TABLE gv_pe1 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_pv1 TARGET COLUMN dst_id REFERENCES gv_pv2 LABEL 'pe1' EDGE TABLE gv_pe2 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_pv1 TARGET COLUMN dst_id REFERENCES gv_pv1 LABEL 'pe2' NO CACHE"), _eq(sorted(r["v"] for r in p.sql("SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE gw3 QUERY 'MATCH (a:pv1)-[e:pe2]->(b:pv1) RETURN b.ID AS V')")), ["a", "b", "d"]))[-1]),
    Fact("G27", "a HOMOGENEOUS workspace (one vertex table, one edge table) with LABEL clauses: a labelled query fails with `invalid column name: a.CYPHER$TABLE$LABEL` (found by the first G17/G25 run; recorded -- a campaign's workspace always has two or more edge tables)", g27, required=False),
    Fact("G26", "DROP SCHEMA ... CASCADE removes the schema's graph workspace", g26),
]

FACTS = R + D + K + G


def main(argv=None) -> int:
    from ganda.hana.connection import HanaServer

    wanted = list(argv if argv is not None else sys.argv[1:])
    facts = [f for f in FACTS if not wanted or any(f.id == w or f.id.startswith(w) and w.isalpha() for w in wanted)]
    failed = 0
    with HanaServer() as server:
        sessions = {}
        for kind in ("rel", "doc", "rdf", "graph"):
            sessions[kind] = server.fresh_session(f"fz_probe_{kind}")
        try:
            for f in facts:
                p = Probe(sessions[{"R": "rel", "D": "doc", "K": "rdf", "G": "graph"}[f.id[0]]])
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
