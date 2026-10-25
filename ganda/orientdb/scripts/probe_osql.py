#!/usr/bin/env python3
"""Re-check every OrientDB SQL fact the fuzzer's design rests on, live, and exit
non-zero if one has changed. Each check is one statement (or a few) with the expected
answer that was observed on 3.2.56; a change means a template, a guard or the
semantics needs a second look before the next campaign: the output names the rule a
failing check checks, or the defect it reproduces, and the code reading it. A failing
check of a rule (or of nothing in the catalog) fails the run; a defect that no longer
reproduces is recorded as fixed on the build. A run in which every check of a rule holds
writes `rules/baseline.json` and re-renders `RULES.md`.

    python3 -m ganda orientdb probe                # Docker
    ORIENT_URL=http://127.0.0.1:2480 ORIENT_PASSWORD=fuzz python3 -m ganda orientdb probe
"""
from __future__ import annotations

import base64
import pathlib
import sys

from ganda.core.rules.model import Defect
from ganda.orientdb.engine.connection import OrientError, OrientServer  # noqa: E402
from ganda.orientdb.engine.limits import HTTP_MAX_LENGTH  # noqa: E402
from ganda.orientdb.storage.mapping.common import Sql  # noqa: E402

DB = "fz_probe_osql"
ran: list = []
failures: list = []


def check(fid: str, name: str, got, expected) -> None:
    """One fact: `fid` names it in `rules/checks.py`, which lists the rules it checks and
    the defects it reproduces."""
    ok = got == expected
    print(f"{'ok  ' if ok else 'FAIL'} {fid:5s} {name}: {got!r}" + ("" if ok else f"  (expected {expected!r})"))
    ran.append(fid)
    if not ok and fid not in failures:
        failures.append(fid)


def rows(session, sql, params=None):
    return session.sql(sql, params, timeout_ms=0)


def col(session, sql, params=None, key="v"):
    return [r.get(key) for r in rows(session, sql, params)]


def err(session, sql, params=None) -> str:
    try:
        rows(session, sql, params)
        return "ok"
    except OrientError as e:
        m = str(e)
        return "internal:" + ("NPE" if "NullPointer" in m else "ClassCast" if "ClassCast" in m else "unsupported" if "Unsupported" in m else "other") if e.internal else "rejected"


def _last_error(session, sql, params=None) -> str:
    try:
        rows(session, sql, params)
        return ""
    except OrientError as e:
        return str(e)


def err_script(session, text, params=None) -> str:
    try:
        session.script(text, params)
        return "ok"
    except OrientError as e:
        return "internal" if e.internal else "rejected"


def main() -> int:
    # the body bound the campaign's servers run with; an attached server (ORIENT_URL)
    # keeps its own, and the large-data checks below say so when they fail
    with OrientServer(http_max_length=HTTP_MAX_LENGTH) as server:
        s = server.fresh_session(DB)
        for q in [
            "CREATE CLASS n1", "CREATE PROPERTY n1.id STRING", "CREATE PROPERTY n1.f1 LONG", "CREATE PROPERTY n1.f2 DOUBLE", "CREATE PROPERTY n1.f3 STRING",
            "CREATE INDEX n1_id ON n1 (id) UNIQUE",
            "CREATE CLASS j1", "CREATE PROPERTY j1.src_id STRING", "CREATE PROPERTY j1.dst_id STRING", "CREATE PROPERTY j1.w LONG",
            "CREATE CLASS d1", "CREATE PROPERTY d1.id STRING", "CREATE PROPERTY d1.e1 EMBEDDEDLIST",
        ]:
            s.sql(q)
        data = [("n1/r0", 0, 0.0, "a"), ("n1/r1", 1, -0.0, "B"), ("n1/r2", 2**53, 1e300, "\U0001F600"), ("n1/r3", 2**53 + 1, 5e-324, "￿"), ("n1/r4", None, None, "b")]
        for rid, f1, f2, f3 in data:
            s.sql("INSERT INTO n1 SET id = :id, f1 = :f1, f2 = :f2, f3 = :f3", {"id": rid, "f1": f1, "f2": f2, "f3": f3})
        s.sql("INSERT INTO n1 SET id = 'n1/r5'")
        for a, b, w in [("n1/r0", "n1/r1", 1), ("n1/r0", "n1/r1", 2), ("n1/r0", "n1/r0", 3), ("n1/r1", "n1/r2", 4)]:
            s.sql("INSERT INTO j1 SET src_id = :a, dst_id = :b, w = :w", {"a": a, "b": b, "w": w})

        print("--- values")
        check("C1", "int64 max round-trips", col(s, "SELECT :p AS v", {"p": 2**63 - 1}), [2**63 - 1])
        check("C2", "-0.0 round-trips", str(col(s, "SELECT f2 AS v FROM n1 WHERE id = 'n1/r1'")[0]), "-0.0")
        check("C3", "5e-324 round-trips", col(s, "SELECT f2 AS v FROM n1 WHERE id = 'n1/r3'"), [5e-324])
        check("C4", "long overflow wraps", col(s, "SELECT (:p + 1) AS v", {"p": 2**63 - 1}), [-(2**63)])

        print("--- null semantics")
        check("C10", "IS NULL holds for absent and explicit null", col(s, "SELECT id AS v FROM n1 WHERE f1 IS NULL"), ["n1/r4", "n1/r5"])
        check("C12", "ORDER BY ASC puts nulls last", col(s, "SELECT id AS v FROM n1 ORDER BY f1 ASC, id ASC")[-2:], ["n1/r4", "n1/r5"])
        check("C13", "ORDER BY DESC puts nulls first", col(s, "SELECT id AS v FROM n1 ORDER BY f1 DESC, id ASC")[:2], ["n1/r4", "n1/r5"])

        print("--- doubles")
        check("C14", "-0.0 = 0 is false", col(s, "SELECT id AS v FROM n1 WHERE f2 = 0"), ["n1/r0"])
        check("C15", "-0.0 < 0 is TRUE", col(s, "SELECT id AS v FROM n1 WHERE f2 < 0"), ["n1/r1"])

        print("--- strings")
        check("C19", "ORDER BY collates: a, b, B (NOT code units) -- O6", col(s, "SELECT id AS v FROM n1 WHERE f3 IN ['a', 'b', 'B'] ORDER BY f3 ASC, id ASC"), ["n1/r0", "n1/r4", "n1/r1"])
        s.sql("INSERT INTO n1 SET id = 'n1/nfd', f3 = :v", {"v": "e" + chr(0x301)})
        check("C22", "no Unicode normalization (NFD stays 2 units)", col(s, "SELECT f3.length() AS v FROM n1 WHERE id = 'n1/nfd'"), [2])

        for q in ["CREATE CLASS w6", "CREATE PROPERTY w6.id STRING", "CREATE PROPERTY w6.n LONG", "CREATE INDEX w6_id ON w6 (id) UNIQUE"]:
            s.sql(q)
        for n_, k_ in enumerate(["b", "B", "a", "é", "z"]):
            s.sql("INSERT INTO w6 SET id = :k, n = :n", {"k": k_, "n": n_})
        check("C24", "O6 (walk face): a single-key ORDER BY through an SB-tree index walks in code-unit order", col(s, "SELECT id AS v FROM w6 ORDER BY id ASC"), ["B", "a", "b", "z", "é"])
        check("C25", "O6 (walk face): ... under a WHERE on another field and a LIMIT too", col(s, "SELECT id AS v FROM w6 WHERE n >= 0 ORDER BY id ASC LIMIT 5"), ["B", "a", "b", "z", "é"])
        check("C26", "O6 (walk face): ... where a subquery target sorts by the collator", col(s, "SELECT id AS v FROM (SELECT FROM w6) ORDER BY id ASC"), ["a", "b", "B", "é", "z"])
        check("C27", "O6 (walk face): ... and so does a second key", col(s, "SELECT id AS v FROM w6 ORDER BY id ASC, n ASC"), ["a", "b", "B", "é", "z"])

        print("--- aggregates and collections")
        check("C28", "sum over longs is exact", col(s, "SELECT sum(f1) AS v FROM n1"), [2**54 + 2])
        check("C29", "max ignores null, over nothing is null", col(s, "SELECT max(f1) AS v FROM n1 WHERE id = 'n1/r5'"), [None])
        for q in ["CREATE CLASS d0", "CREATE PROPERTY d0.id STRING", "CREATE PROPERTY d0.e1 EMBEDDEDLIST", "INSERT INTO d0 SET id = 'n', e1 = null", "INSERT INTO d0 SET id = 'a'", "INSERT INTO d0 SET id = 'e', e1 = []"]:
            s.sql(q)
        check("C31", ".size() of a null, an absent and an empty list is 0, so `.size() = 0` holds for all three", (col(s, "SELECT e1.size() AS v FROM d0 ORDER BY id"), col(s, "SELECT id AS v FROM d0 WHERE e1.size() = 0 ORDER BY id")), ([0, 0, 0], ["a", "e", "n"]))
        s.sql("INSERT INTO d1 SET id = 'd1/r0'")
        check("C34", ".sum() method is HTTP 501 (O5)", err(s, "SELECT e1.w.sum() AS v FROM d1"), "internal:unsupported")

        print("--- correlated subqueries")
        LET = "LET $h = (SELECT dst_id AS v FROM j1 WHERE src_id = $parent.$current.id)"
        check("C37", "$parent.$current under a scan", col(s, f"SELECT id, $h.v AS v FROM n1 {LET} WHERE f3 = 'a'"), [["n1/r1", "n1/r1", "n1/r0"]])
        check("C38", "O1: $parent.$current is null when the outer is index-served (WHERE id =)", col(s, f"SELECT id, $h.v AS v FROM n1 {LET} WHERE id = 'n1/r0'"), [[]])
        check("C39", "O1: ... and under ORDER BY on an indexed field", col(s, f"SELECT id, $h.v AS v FROM n1 {LET} ORDER BY id")[0], [])
        s.sql("CREATE CLASS j2"); s.sql("CREATE PROPERTY j2.src_id STRING"); s.sql("CREATE PROPERTY j2.dst_id STRING"); s.sql("CREATE INDEX j2_src ON j2 (src_id) NOTUNIQUE")
        for a, b in [("n1/r0", "n1/r1"), ("n1/r0", "n1/r1"), ("n1/r0", "n1/r0"), ("n1/r1", "n1/r2")]:
            s.sql("INSERT INTO j2 SET src_id = :a, dst_id = :b", {"a": a, "b": b})
        check("C40", "O1: a correlated LET under a GROUP BY on an indexed column answers (the index is on the grouped column, not the filter)", col(s, "SELECT src_id AS id, max($f[0].v) AS v FROM j2 LET $f = (SELECT f1 AS v FROM n1 WHERE id = $parent.$current.dst_id) GROUP BY src_id"), [1, 2**53])
        check("C41", "O1: a TRAVERSE rooted at $parent.$current under an index-served outer query traverses nothing", col(s, "SELECT id, $t.id AS v FROM n1 LET $t = (SELECT FROM (TRAVERSE out('e1') FROM $parent.$current MAXDEPTH 1) WHERE $depth = 1) WHERE id = 'n1/r0'"), [[]])
        s.sql("CREATE INDEX n1_f1 ON n1 (f1) NOTUNIQUE")
        check("C42", "O2: correlated equality + predicate on an indexed field is an NPE", err(s, "SELECT src_id AS id, $n.f1 AS v FROM j1 LET $n = (SELECT FROM n1 WHERE id = $parent.$current.dst_id AND f1 > :p)", {"p": 0}), "internal:NPE")
        check("C43", "O2: ... IS NULL on the indexed field too", err(s, "SELECT src_id AS id, $n.f1 AS v FROM j1 LET $n = (SELECT FROM n1 WHERE id = $parent.$current.dst_id AND f1 IS NULL)"), "internal:NPE")
        # O2's FROM face (cards-pilot1-c20 seed 8009, round 2's rel / graph suspects): the
        # correlation moved OUT of the index-served subquery into the query over it
        FACE = "SELECT id, $c.size() AS v FROM n1 LET $c = (SELECT k FROM (SELECT id AS k FROM n1 WHERE f1 {op} :p) WHERE k = {corr})"
        check("C47", "O2 (FROM face): a correlated subquery over a FROM subquery an index serves is an NPE", err(s, FACE.format(op="=", corr="$parent.$current.id"), {"p": 0}), "internal:NPE")
        check("C48", "O2 (FROM face): ... IS NULL / a range / IN served there too", err(s, "SELECT id, $c.size() AS v FROM n1 LET $c = (SELECT k FROM (SELECT id AS k FROM n1 WHERE f1 IS NULL) WHERE k = $parent.$current.id)"), "internal:NPE")
        check("C49", "O2 (FROM face): ... a correlation read only in the projection too", err(s, "SELECT id, $c.size() AS v FROM n1 LET $c = (SELECT k, $parent.$current.id AS o FROM (SELECT id AS k FROM n1 WHERE f1 = :p))", {"p": 0}), "internal:NPE")
        check("C50", "O2 (FROM face): ... two FROM levels down too", err(s, "SELECT id, $c.size() AS v FROM n1 LET $c = (SELECT FROM (SELECT FROM (SELECT FROM n1 WHERE f1 = :p)) WHERE id = $parent.$current.id)", {"p": 0}), "internal:NPE")
        for q in ["CREATE CLASS o2v EXTENDS V", "CREATE PROPERTY o2v.id STRING", "CREATE PROPERTY o2v.f LONG", "CREATE INDEX o2v_id ON o2v (id) UNIQUE", "CREATE CLASS o2e EXTENDS E",
                  "CREATE CLASS o2j", "CREATE PROPERTY o2j.src_id STRING", "CREATE PROPERTY o2j.dst_id STRING", "CREATE INDEX o2j_dst ON o2j (dst_id) NOTUNIQUE"]:
            s.sql(q)
        for i in range(4):
            s.sql("CREATE VERTEX o2v SET id = :id, f = :f", {"id": f"o2v/{i}", "f": i})
        for a_, b_ in [(0, 1), (1, 2), (0, 3)]:
            s.sql(f"CREATE EDGE o2e FROM (SELECT FROM o2v WHERE id = 'o2v/{a_}') TO (SELECT FROM o2v WHERE id = 'o2v/{b_}')")
            s.sql("INSERT INTO o2j SET src_id = :a, dst_id = :b", {"a": f"o2v/{a_}", "b": f"o2v/{b_}"})
        TRAV = "SELECT id, $b.size() AS v FROM o2v LET $b = (SELECT FROM (TRAVERSE out('o2e') FROM (SELECT FROM o2v WHERE id = :k) MAXDEPTH 2 STRATEGY BREADTH_FIRST) WHERE id = {corr})"
        check("C53", "O2 (FROM face): ... over a TRAVERSE rooted at an index-served SELECT too (round 2 slot 485)", err(s, TRAV.format(corr="$parent.$current.id"), {"k": "o2v/0"}), "internal:NPE")
        SIB = "SELECT id, $m.size() AS v FROM o2v LET $m = (SELECT $d AS x FROM o2j LET $b = (SELECT id FROM o2v WHERE {b}), $d = (SELECT dst_id FROM o2j WHERE src_id = $b[0].id AND dst_id IN (SELECT id FROM o2v WHERE f > 0)))"
        check("C55", "O2: a correlation through a correlated LET variable of the enclosing subquery, beside an index-served IN, is an NPE (round 2 slot 536)", err(s, SIB.format(b="id = $parent.$current.dst_id")), "internal:NPE")
        # O2: the rule (the round-2 smoke's NPEs, seeds 9000 / 9001): one subquery of the
        # outermost query holding a `$parent.$current` read ANYWHERE and an index-served
        # condition at ANY of its levels
        RULE = "SELECT id, $m.size() AS v FROM n1 LET $m = ({m})"
        rule = lambda m, p=None: err(s, RULE.format(m=m), p if p is not None else {"p": 0})
        check("C57", "O2 (the rule): a correlated level + an IN (subquery) an index serves is an NPE", rule("SELECT id FROM n1 WHERE f3 = $parent.$current.f3 AND f3 IN (SELECT f3 FROM n1 WHERE f1 = :p)"), "internal:NPE")
        check("C59", "O2 (the rule): ... an uncorrelated nested LET an index serves too", rule("SELECT id FROM n1 LET $n = (SELECT id FROM n1 WHERE f1 = :p) WHERE f3 = $parent.$current.f3 AND $n.size() > 0"), "internal:NPE")
        check("C60", "O2 (the rule): ... a correlated nested LET beside a served sibling subquery too", rule("SELECT id FROM n1 LET $x = (SELECT dst_id FROM j1 WHERE src_id = $parent.$current.id) WHERE f3 IN (SELECT f3 FROM n1 WHERE f1 = :p)"), "internal:NPE")
        check("C61", "O2 (the rule): ... a correlation held in an expression LET too", rule("SELECT id FROM n1 LET $r = $parent.$current.f3 WHERE f3 = $r AND f1 = :p"), "internal:NPE")
        check("C64", "O2 (the rule): a subquery an index serves inside a BRACKET filter is an NPE, no correlation needed", err(s, "SELECT id, out('o2e')[id IN (SELECT id FROM o2v WHERE id = :k)].size() AS v FROM o2v", {"k": "o2v/1"}), "internal:NPE")
        check("C69", "O1 (not O2): an index-served FROM target of the outermost query over a correlated LET answers nothing", col(s, "SELECT count(*) AS v FROM (SELECT id FROM n1 LET $x = (SELECT dst_id FROM j1 WHERE src_id = $parent.$current.id) WHERE f1 = 0 AND $x.size() > 0)"), [0])
        check("C70", "O1 (not O2): ... where the scan answers", col(s, "SELECT count(*) AS v FROM (SELECT id FROM n1 LET $x = (SELECT dst_id FROM j1 WHERE src_id = $parent.$current.id) WHERE f3 = 'a' AND $x.size() > 0)"), [1])
        check("C71", "aggregate over a list of lists is a ClassCastException (O5)", err(s, "SELECT id, max($h.v) AS v FROM n1 LET $h = (SELECT $f.v AS v FROM j1 LET $f = (SELECT f1 AS v FROM n1 WHERE id = $parent.$current.dst_id) WHERE src_id = $parent.$current.id) WHERE f3 = 'a' GROUP BY id"), "internal:ClassCast")
        check("C72", "[0] on a one-record lookup keeps the projection flat", col(s, "SELECT src_id AS id, $f[0].v AS v FROM j1 LET $f = (SELECT f1 AS v FROM n1 WHERE id = $parent.$current.dst_id) WHERE w = 4"), [2**53])
        check("C73", "nested LETs: inside a LET subquery, a nested LET's $parent.$current is that subquery's row (the junction row, not the outer record; the outer query scans, O1 aside)", sorted(sorted(x) for x in col(s, "SELECT id, $h.v AS v FROM n1 LET $h = (SELECT $t.v AS v FROM j1 LET $t = (SELECT dst_id AS v FROM j1 WHERE src_id = $parent.$current.dst_id) WHERE src_id = $parent.$current.id) WHERE f3 = 'a'")[0]), [["n1/r0", "n1/r1", "n1/r1"], ["n1/r2"], ["n1/r2"]])

        print("--- indexes")
        s.sql("CREATE CLASS k1"); s.sql("CREATE PROPERTY k1.f STRING"); s.sql("CREATE INDEX k1_f ON k1 (f) DICTIONARY")
        for i in range(3):
            s.sql("INSERT INTO k1 SET f = 'dup'")

        print("--- lists, links and composite indexes (O7-O11)")
        s.sql("CREATE CLASS l1"); s.sql("CREATE PROPERTY l1.f5 EMBEDDEDLIST LONG"); s.sql("CREATE PROPERTY l1.e1 EMBEDDEDLIST")
        s.sql("INSERT INTO l1 SET f5 = :v, e1 = :l", {"v": [1, 2], "l": [{"f5": [1, 2]}]})
        check("C77", "O7: [1, 2] never equals a declared EMBEDDEDLIST LONG", len(rows(s, "SELECT FROM l1 WHERE f5 = :p", {"p": [1, 2]})), 0)
        s.sql("CREATE CLASS v1 EXTENDS V"); s.sql("CREATE PROPERTY v1.id STRING"); s.sql("CREATE PROPERTY v1.f4 LONG"); s.sql("CREATE CLASS ve EXTENDS E")
        va = s.sql_raw("CREATE VERTEX v1 SET id = 'a', f4 = 1")[0]["@rid"]
        vb = s.sql_raw("CREATE VERTEX v1 SET id = 'b'")[0]["@rid"]
        s.sql("CREATE EDGE ve FROM :a TO :b", {"a": va, "b": vb})
        check("C80", "O8: link.field IS DEFINED holds for an absent field", col(s, "SELECT in.id AS v FROM ve WHERE in.f4 IS DEFINED"), ["b"])
        s.sql("CREATE CLASS w1"); s.sql("CREATE PROPERTY w1.id STRING"); s.sql("CREATE PROPERTY w1.e2 EMBEDDEDLIST"); s.sql("CREATE PROPERTY w1.e3 EMBEDDEDLIST")
        w0 = s.sql_raw("INSERT INTO w1 SET id = 'r0'")[0]["@rid"]
        w1 = s.sql_raw("INSERT INTO w1 SET id = 'r1'")[0]["@rid"]
        s.sql("UPDATE w1 SET e2 = :l WHERE id = 'r0'", {"l": [{"target": w1}]})
        s.sql("UPDATE w1 SET e3 = :l WHERE id = 'r1'", {"l": [{"target": w0, "f3": 1}]})
        check("C82", "O9: a bracket filter after a multi-valued link path matches nothing", col(s, "SELECT e2.target.e3[f3 = 1].target.id AS v FROM w1 WHERE id = 'r0'"), [[]])
        check("C83", "O9: ... field access through the same path is mapped", col(s, "SELECT e2.target.e3.target.id AS v FROM w1 WHERE id = 'r0'"), [[["r0"]]])
        s.sql("CREATE CLASS x1"); s.sql("CREATE PROPERTY x1.f1 LONG"); s.sql("CREATE PROPERTY x1.f2 STRING")
        s.sql("INSERT INTO x1 SET f1 = 1, f2 = 'a'"); s.sql("INSERT INTO x1 SET f1 = 9223372036854775807, f2 = 'b'"); s.sql("INSERT INTO x1 SET f2 = 'c'")
        s.sql("CREATE INDEX x1_f1 ON x1 (f1) NOTUNIQUE")
        check("C85", "O10: IN [int, long] through an index is a ClassCastException", err(s, "SELECT FROM x1 WHERE f1 IN [1, 9223372036854775807]"), "internal:ClassCast")
        s.sql("CREATE CLASS x2"); s.sql("CREATE PROPERTY x2.f2 DOUBLE"); s.sql("INSERT INTO x2 SET f2 = 1.5"); s.sql("INSERT INTO x2 SET f2 = 2.0")
        s.sql("CREATE INDEX x2_f2 ON x2 (f2) NOTUNIQUE")
        check("C87", "O10: IN [double, int] through an index is a ClassCastException", err(s, "SELECT FROM x2 WHERE f2 IN :p", {"p": [1.5, 2]}), "internal:ClassCast")
        s.sql("DROP INDEX x1_f1"); s.sql("CREATE INDEX x1_f1f2 ON x1 (f1, f2) NOTUNIQUE")
        s.sql("CREATE CLASS x36"); s.sql("CREATE PROPERTY x36.id STRING"); s.sql("CREATE PROPERTY x36.f LONG"); s.sql("CREATE PROPERTY x36.g LONG")
        for rid, f, g in (("a", 1, 10), ("b", 2, 30), ("c", 3, 10)):
            s.sql("INSERT INTO x36 SET id = :id, f = :f, g = :g", {"id": rid, "f": f, "g": g})
        O36 = "SELECT id AS v FROM x36 WHERE f BETWEEN 1 AND 3 AND g >= 20"
        s.sql("CREATE INDEX x36_fg ON x36 (f, g) NOTUNIQUE")
        check("C91", "O36: ... through a composite index on (f, g) it answers the lexicographic key range (the g condition dropped); `>= AND <=` and an equality lead are right", (sorted(col(s, O36)), sorted(col(s, "SELECT id AS v FROM x36 WHERE f >= 1 AND f <= 3 AND g >= 20")), sorted(col(s, "SELECT id AS v FROM x36 WHERE f = 3 AND g >= 20"))), (["b", "c"], ["b"], []))

        s.sql("CREATE CLASS y1"); s.sql("CREATE PROPERTY y1.f2 DOUBLE"); s.sql("INSERT INTO y1 SET f2 = 776.417")
        check("C92", "O12: a double literal is parsed as a float32 (scan misses)", len(rows(s, "SELECT FROM y1 WHERE f2 = 776.417")), 0)
        s.sql("CREATE INDEX y1_f2 ON y1 (f2) NOTUNIQUE")

        s.sql("CREATE CLASS z1"); s.sql("CREATE PROPERTY z1.e1 EMBEDDEDLIST"); s.sql("CREATE PROPERTY z1.f2 LONG")
        s.sql("INSERT INTO z1 SET f2 = 946, e1 = :l", {"l": [{"f2": 946}]})
        check("C95", "O13: BETWEEN with a Long bound misses an embedded Integer", col(s, "SELECT e1[f2 BETWEEN :a AND :b].f2 AS v FROM z1", {"a": 946, "b": 2**40}), [[]])

        print("--- null keys through an index (O14), mixed-width embedded aggregates (O15)")
        check("C98", "O14: IN (subquery holding a null) through an index returns the null rows", sorted(col(s, "SELECT id AS v FROM n1 WHERE f1 IN (SELECT f1 FROM n1 WHERE id IN ['n1/r1', 'n1/r4'])")), ["n1/nfd", "n1/r1", "n1/r4", "n1/r5"])
        check("C100", "O14: f1 = null through an index returns the null rows", sorted(col(s, "SELECT id AS v FROM n1 WHERE f1 = :p", {"p": None})), ["n1/nfd", "n1/r4", "n1/r5"])
        check("C102", "O14: IN [x, null] through an index is an NPE", err(s, "SELECT id AS v FROM n1 WHERE f1 IN :p", {"p": [1, None]}), "internal:NPE")
        s.sql("CREATE CLASS m1"); s.sql("CREATE PROPERTY m1.e1 EMBEDDEDLIST")
        s.sql("INSERT INTO m1 SET e1 = :l", {"l": [{"w": 3}, {"w": 2**40}]})
        check("C105", "O15: max over an embedded list mixing Integer and Long is a ClassCastException", err(s, "SELECT max(e1.w) AS v FROM m1"), "internal:ClassCast")

        print("--- cross-type pivots (O16, O17)")
        s.sql("CREATE CLASS c1"); s.sql("CREATE PROPERTY c1.id STRING"); s.sql("CREATE PROPERTY c1.f1 LONG"); s.sql("CREATE PROPERTY c1.e1 EMBEDDEDLIST")
        for i, v in (("a", 0), ("b", 1), ("c", 5), ("d", 6), ("f", 2**53 + 1)):
            s.sql("INSERT INTO c1 SET id = :id, f1 = :v, e1 = :l", {"id": i, "v": v, "l": [{"w": v}]})
        check("C110", "O17: BETWEEN truncates double bounds on a LONG column (scan)", col(s, "SELECT id AS v FROM c1 WHERE f1 BETWEEN :a AND :b", {"a": 0.5, "b": 5.5}), ["a", "b", "c"])
        check("C112", "O17: ... an integral float bound at 2^53 becomes the exact long", col(s, "SELECT id AS v FROM c1 WHERE f1 BETWEEN :a AND :b", {"a": 1, "b": 9007199254740992.0}), ["b", "c", "d"])
        check("C113", "O17: ... and so does an embedded Integer", col(s, "SELECT e1[w BETWEEN :a AND :b].w AS v FROM c1 WHERE id = 'a'", {"a": 0.5, "b": 5.5}), [[0]])
        s.sql("CREATE INDEX c1_f1 ON c1 (f1) NOTUNIQUE")
        check("C114", "O16: an index converts a double key by truncation (= 5.5 matches 5)", col(s, "SELECT id AS v FROM c1 WHERE f1 = :p", {"p": 5.5}), ["c"])
        check("C115", "O16: ... >= 5.5 includes 5", col(s, "SELECT id AS v FROM c1 WHERE f1 >= :p", {"p": 5.5}), ["c", "d", "f"])
        check("C116", "O16: ... < 5.5 excludes 5", col(s, "SELECT id AS v FROM c1 WHERE f1 < :p", {"p": 5.5}), ["a", "b"])
        check("C117", "O16: ... 2^53.0 no longer matches 2^53 + 1", col(s, "SELECT id AS v FROM c1 WHERE f1 = :p", {"p": 9007199254740992.0}), [])
        s.sql("DROP INDEX c1_f1"); s.sql("CREATE INDEX c1_f1h ON c1 (f1) NOTUNIQUE_HASH_INDEX")
        check("C118", "O16: a hash index truncates the equality key too", col(s, "SELECT id AS v FROM c1 WHERE f1 = :p", {"p": 5.5}), ["c"])

        print("--- unique indexes and null keys")
        s.sql("CREATE CLASS u2"); s.sql("CREATE PROPERTY u2.f LONG"); s.sql("CREATE PROPERTY u2.g STRING"); s.sql("CREATE INDEX u2_f ON u2 (f) UNIQUE METADATA {ignoreNullValues: true}")
        s.sql("INSERT INTO u2 SET g = 'a'")
        check("C121", "... with ignoreNullValues: true it accepts any number of them", err(s, "INSERT INTO u2 SET g = 'b'"), "ok")
        for g, f in (("c", 5), ("d", 1), ("e", 9)):
            s.sql("INSERT INTO u2 SET g = :g, f = :f", {"g": g, "f": f})
        check("C122", "O18: ORDER BY f DESC served from an index that ignores nulls omits the null records (3.2.56: no rows)", col(s, "SELECT g AS v FROM u2 ORDER BY f DESC"), ["e", "c", "d"])
        check("C123", "O18: ... ORDER BY f ASC omits the null records", col(s, "SELECT g AS v FROM u2 ORDER BY f ASC"), ["d", "c", "e"])

        print("--- graph")
        for q in ["CREATE CLASS g1 EXTENDS V", "CREATE PROPERTY g1.id STRING", "CREATE CLASS ge EXTENDS E", "CREATE PROPERTY ge.w LONG"]:
            s.sql(q)
        a = s.sql_raw("CREATE VERTEX g1 SET id = 'g1/a'")[0]["@rid"]
        b = s.sql_raw("CREATE VERTEX g1 SET id = 'g1/b'")[0]["@rid"]
        for w in (1, 2):
            s.sql("CREATE EDGE ge FROM :a TO :b SET w = :w", {"a": a, "b": b, "w": w})
        s.sql("CREATE EDGE ge FROM :a TO :b SET w = 3", {"a": a, "b": a})
        check("C126", "out() is a multiset (parallel edges twice)", col(s, "SELECT out('ge').id AS v FROM g1 WHERE id = 'g1/a'"), [["g1/b", "g1/b", "g1/a"]])
        check("C127", "both() yields a self-loop twice (3 out + 1 in)", col(s, "SELECT both('ge').id.size() AS v FROM g1 WHERE id = 'g1/a'"), [4])
        check("C128", "MATCH keeps path multiplicity", len(rows(s, "MATCH {class: g1, as: x, where: (id = 'g1/a')} .out('ge') {as: y} RETURN y.id AS v")), 3)
        check("C130", "DELETE VERTEX cascades its edges", (s.sql("DELETE VERTEX g1 WHERE id = 'g1/b'"), col(s, "SELECT count(*) AS v FROM ge"))[1], [1])

        print("--- MATCH walked backwards drops the edge class (O45)")
        for q in ["CREATE CLASS ma EXTENDS V", "CREATE PROPERTY ma.id STRING", "CREATE PROPERTY ma.f LONG", "CREATE CLASS mb EXTENDS V", "CREATE PROPERTY mb.id STRING", "CREATE PROPERTY mb.f LONG",
                  "CREATE CLASS mx EXTENDS E", "CREATE PROPERTY mx.eid STRING", "CREATE CLASS my EXTENDS E", "CREATE PROPERTY my.eid STRING"]:
            s.sql(q)
        for i in range(4):
            s.sql("CREATE VERTEX ma SET id = :i, f = :f", {"i": f"a{i}", "f": i})
        for i in range(2):
            s.sql("CREATE VERTEX mb SET id = :i, f = :f", {"i": f"b{i}", "f": i})
        for cls, eid, a_, b_ in (("mx", "x1", 0, 0), ("my", "y1", 0, 0), ("my", "y2", 1, 1)):
            s.sql(f"CREATE EDGE {cls} FROM (SELECT FROM ma WHERE id = 'a{a_}') TO (SELECT FROM mb WHERE id = 'b{b_}') SET eid = '{eid}'")
        O45 = "MATCH {{class: ma, as: a{aw}}}.{oute}('mx'){{as: e}}.{inv}(){{{bc}as: b{bw}}} RETURN e.eid AS v"
        m45 = lambda **kw: sorted(col(s, O45.format(**{"aw": "", "bw": "", "bc": "class: mb, ", "oute": "outE", "inv": "inV", **kw})))
        check("C132", "O45: outE('mx').inV(){class: mb} answers the `my` edges too when mb is the smaller class (4 vs 2)", m45(), ["x1", "y1", "y2"])
        check("C133", "O45: ... bothE / bothV too", m45(oute="bothE", inv="bothV"), ["x1", "y1", "y2"])
        check("C134", "O45: ... not without the class on the far alias (never the root)", m45(bc=""), ["x1"])
        for i in range(2, 4):
            s.sql("CREATE VERTEX mb SET id = :i, f = :f", {"i": f"b{i}", "f": i})
        check("C138", "O45: ... a `where:` halves an alias's estimate: on the far alias it is the root again", m45(bw=", where: (f >= 0)"), ["x1", "y1", "y2"])
        for i in range(4, 8):
            s.sql("CREATE VERTEX mb SET id = :i, f = :f", {"i": f"b{i}", "f": i})
        check("C141", "O45: ... and now the mirrored pattern drops the class", sorted(col(s, "MATCH {class: mb, as: b}.inE('mx'){as: e}.outV(){class: ma, as: a} RETURN e.eid AS v")), ["x1", "y1", "y2"])

        print("--- writes")
        check("C143", "a RID string inside a parameter becomes a LINK in an embedded document", col(s, "UPDATE d1 SET e1 = :l WHERE id = 'd1/r0'", {"l": [{"target": a, "w": 1}]}) and col(s, "SELECT e1.target.id AS v FROM d1"), [["g1/a"]])

        print("--- narrow numeric types: INTEGER (O16 wrap, O17, O19, O24), FLOAT (O16 rounding), DECIMAL (O20)")
        s.sql("CREATE CLASS i1"); s.sql("CREATE PROPERTY i1.id STRING"); s.sql("CREATE PROPERTY i1.f INTEGER")
        for i, v in (("a", 0), ("b", 5), ("c", 2**31 - 1), ("d", -(2**31))):
            s.sql("INSERT INTO i1 SET id = :id, f = :v", {"id": i, "v": v})
        check("C145", "O24: a write beyond int32 into an INTEGER property is stored WRAPPED", col(s, "INSERT INTO i1 SET id = 'w', f = :v", {"v": 2**31}, key="f"), [-(2**31)])
        s.sql("DELETE FROM i1 WHERE id = 'w'")
        check("C147", "O17: BETWEEN wraps a Long bound on an INTEGER column", col(s, "SELECT id AS v FROM i1 WHERE f BETWEEN :a AND :b", {"a": 1, "b": 2**31}), [])
        check("C148", "O19: sum() over INTEGERs accumulates in 32-bit arithmetic", col(s, "SELECT sum(f) AS v FROM i1 WHERE id IN ['b', 'c']"), [-2147483644])
        s.sql("CREATE INDEX i1_f ON i1 (f) NOTUNIQUE METADATA {ignoreNullValues: true}")
        check("C151", "O16: an index on an INTEGER column WRAPS a Long key (2^31 finds -2^31)", col(s, "SELECT id AS v FROM i1 WHERE f = :p", {"p": 2**31}), ["d"])
        check("C152", "O16: ... f > 2^31 through the index is everything above -2^31", sorted(col(s, "SELECT id AS v FROM i1 WHERE f > :p", {"p": 2**31})), ["a", "b", "c"])
        check("C153", "O16: ... 2^40 wraps to 0", col(s, "SELECT id AS v FROM i1 WHERE f = :p", {"p": 2**40}), ["a"])
        s.sql("CREATE CLASS fl"); s.sql("CREATE PROPERTY fl.id STRING"); s.sql("CREATE PROPERTY fl.f FLOAT")
        for i, v in (("a", 0.125), ("c", float(2**24)), ("d", 0.1)):
            s.sql("INSERT INTO fl SET id = :id, f = :v", {"id": i, "v": v})
        s.sql("CREATE INDEX fl_f ON fl (f) NOTUNIQUE METADATA {ignoreNullValues: true}")
        check("C158", "O16: an index on a FLOAT column ROUNDS a double key (0.1 finds 0.1f)", col(s, "SELECT id AS v FROM fl WHERE f = :p", {"p": 0.1}), ["d"])
        check("C159", "O16: ... and f > 0.1 through the index excludes 0.1f", sorted(col(s, "SELECT id AS v FROM fl WHERE f > :p", {"p": 0.1})), ["a", "c"])
        s.sql("CREATE CLASS dc"); s.sql("CREATE PROPERTY dc.id STRING"); s.sql("CREATE PROPERTY dc.f DECIMAL")
        for i, v in (("a", 0.1), ("b", 0.2), ("e", 0.5)):
            s.sql("INSERT INTO dc SET id = :id, f = :v", {"id": i, "v": v})
        check("C161", "O20: a scan compares a DECIMAL with a double through its binary expansion (0.1 = 0.1 is false)", col(s, "SELECT id AS v FROM dc WHERE f = :p", {"p": 0.1}), [])
        check("C162", "O20: ... f >= 0.1 excludes the stored 0.1", sorted(col(s, "SELECT id AS v FROM dc WHERE f >= :p", {"p": 0.1})), ["b", "e"])
        check("C165", "sum over DECIMALs is exact decimal arithmetic", col(s, "SELECT sum(f) AS v FROM dc WHERE id IN ['a', 'b']"), [0.3])
        s.sql("CREATE INDEX dc_f ON dc (f) NOTUNIQUE METADATA {ignoreNullValues: true}")

        print("--- COLLATE ci (O21, O25), LIKE")
        for q in ["CREATE CLASS cv EXTENDS V", "CREATE PROPERTY cv.id STRING", "CREATE PROPERTY cv.s STRING", "ALTER PROPERTY cv.s COLLATE ci", "CREATE PROPERTY cv.g STRING", "CREATE CLASS ce EXTENDS E"]:
            s.sql(q)
        crid = {}
        for i, v in (("r0", "a"), ("r1", "A"), ("r2", "b"), ("r3", "ab"), ("r4", "AB")):
            crid[i] = s.sql_raw("CREATE VERTEX cv SET id = :id, s = :v, g = :v", {"id": i, "v": v})[0]["@rid"]
        s.sql("CREATE EDGE ce FROM :a TO :b", {"a": crid["r0"], "b": crid["r4"]})
        check("C169", "O21: IN compares code units on a scan", col(s, "SELECT id AS v FROM cv WHERE s IN :p", {"p": ["A"]}), ["r1"])
        check("C170", "O21: BETWEEN compares code units on a scan", sorted(col(s, "SELECT id AS v FROM cv WHERE s BETWEEN :a AND :b", {"a": "A", "b": "B"})), ["r1", "r4"])
        check("C173", "ci: GROUP BY does not merge case variants", len(rows(s, "SELECT s AS k, count(*) AS c FROM cv GROUP BY s")), 5)
        for q in ["CREATE CLASS cim", "CREATE PROPERTY cim.s STRING", "ALTER PROPERTY cim.s COLLATE ci", "INSERT INTO cim SET s = 'a'", "INSERT INTO cim SET s = 'B'"]:
            s.sql(q)
        check("C174", "ci: max() compares code units ('a' above 'B'; collated, 'B' would be)", col(s, "SELECT max(s) AS v FROM cim"), ["a"])
        check("C176", "O25: a bracket filter ignores the collation", col(s, "SELECT out('ce')[s = :p].id AS v FROM cv WHERE id = 'r0'", {"p": "ab"}), [[]])
        check("C177", "O25: ... a dotted link field too", col(s, "SELECT out.id AS v FROM ce WHERE in.s = :p", {"p": "ab"}), [])
        check("C179", "O25: ... a projected ci value compared in an enclosing query is case-sensitive too", col(s, "SELECT id AS v FROM (SELECT id, s FROM cv) WHERE s = :p", {"p": "A"}), ["r1"])
        s.sql("CREATE INDEX cv_s ON cv (s) NOTUNIQUE METADATA {ignoreNullValues: true}")

        print("--- list indexes (O7 faces), MATCH NOT (O22), optional, both, unionall")
        s.sql("CREATE CLASS li"); s.sql("CREATE PROPERTY li.id STRING"); s.sql("CREATE PROPERTY li.f EMBEDDEDLIST LONG")
        for i, v in (("a", [1, 2]), ("e", [5]), ("g", [1, 1])):
            s.sql("INSERT INTO li SET id = :id, f = :v", {"id": i, "v": v})
        check("C185", "O7: CONTAINSANY with an Integer list matches nothing on a declared LONG list", col(s, "SELECT id AS v FROM li WHERE f CONTAINSANY :p", {"p": [1, 5]}), [])
        s.sql("CREATE INDEX li_f ON li (f) NOTUNIQUE METADATA {ignoreNullValues: true}")
        check("C187", "O7: a list equality through a list index answers element-wise, with duplicates", sorted(col(s, "SELECT id AS v FROM li WHERE f = :p", {"p": [1, 2]})), ["a", "a", "g"])
        check("C189", "O22: a MATCH negative pattern answers nothing for a record without the path", col(s, "MATCH {class: cv, as: a}, NOT {as: a}.out('ce') {as: b} RETURN a.id AS v"), [])
        check("C190", "O22: ... and repeats one record when the negated pattern can match nobody", col(s, "MATCH {class: cv, as: a}, NOT {as: a}.out('ce') {as: b, where: (s = 'nobody')} RETURN a.id AS v"), ["r0", "r0", "r0", "r0", "r0"])
        check("C191", "MATCH optional: a start record with no path answers a null row", col(s, "MATCH {class: cv, as: a, where: (id = 'r1')}.out('ce') {as: b, optional: true} RETURN a.id AS id, b.id AS v"), [None])
        check("C192", "MATCH may end at an edge alias", col(s, "MATCH {class: cv, as: a, where: (id = 'r0')}.bothE('ce') {as: e} RETURN e.@class AS v"), ["ce"])
        s.sql("CREATE EDGE ce FROM :a TO :b", {"a": crid["r0"], "b": crid["r0"]})
        check("C193", "both() visits a self-loop twice", sorted(col(s, "SELECT both('ce').id AS v FROM cv WHERE id = 'r0'")[0]), ["r0", "r0", "r4"])
        check("C194", "unionall of two LET lists, and over an absent bag", (col(s, "SELECT unionall($o.v, $i.v).size() AS v FROM cv LET $o = (SELECT in.id AS v FROM ce WHERE out = $parent.$current.@rid), $i = (SELECT out.id AS v FROM ce WHERE in = $parent.$current.@rid) WHERE id = 'r0'"), col(s, "SELECT unionall(out_ce.in.id, in_ce.out.id).size() AS v FROM cv WHERE id = 'r2'")), ([3], [0]))
        A26 = "SELECT out.id AS id, $f[0].v AS v FROM ce LET $f = (SELECT s AS v FROM cv WHERE @rid = $parent.$current.in)"
        B26 = "SELECT in.id AS id, $f[0].v AS v FROM ce LET $f = (SELECT s AS v FROM cv WHERE @rid = $parent.$current.out)"
        check("C196", "O26: ... but answers nothing when both parts carry a nested correlated LET", rows(s, f"SELECT expand($c) LET $a = ({A26}), $b = ({B26}), $c = unionall($a, $b)"), [])
        check("C197", "O26: ... where the inline union of the same parts answers", len(rows(s, f"SELECT expand(unionall(({A26}), ({B26})))")), 4)
        check("C198", "O26: ... and that inline union under an outer IN (subquery) filter loses a row and a value", sorted(col(s, f"SELECT id, v FROM (SELECT expand(unionall(({A26}), ({B26})))) WHERE id IN (SELECT id FROM cv WHERE id = 'r0')"), key=str), [None, "a"])
        check("C199", "O26: ... and an inline union whose parts hold an IN (subquery) loses rows under it too", len(rows(s, "SELECT id, v FROM (SELECT expand(unionall((SELECT out.id AS id, in.id AS v FROM ce WHERE in.id IN (SELECT id FROM cv WHERE s IS NOT NULL)), (SELECT in.id AS id, out.id AS v FROM ce WHERE out.id IN (SELECT id FROM cv WHERE s IS NOT NULL))))) WHERE id IN (SELECT id FROM cv WHERE id = 'r0')")), 1)
        s.sql("CREATE CLASS em"); s.sql("CREATE PROPERTY em.e EMBEDDEDLIST")
        s.sql("INSERT INTO em SET e = :l", {"l": [{"k": 2**31 - 1}, {"k": 5}]})
        check("C202", "O19: sum over embedded Integer attributes wraps in 32 bits too", col(s, "SELECT sum(e.k) AS v FROM em"), [-2147483644])

        print("--- ORDER BY through a composite index (O18 face, O23), writes")
        s.sql("CREATE CLASS ob"); s.sql("CREATE PROPERTY ob.id STRING"); s.sql("CREATE PROPERTY ob.f LONG"); s.sql("CREATE PROPERTY ob.g LONG")
        for i, f, g in (("r0", 1, 1), ("r1", 1, None), ("r2", 1, 0), ("r3", None, 5), ("r4", 2, None), ("r5", None, None), ("r6", 2, 2)):
            s.sql("INSERT INTO ob SET id = :id, f = :f, g = :g", {"id": i, "f": f, "g": g})
        s.sql("CREATE INDEX ob_fg ON ob (f, g) NOTUNIQUE METADATA {ignoreNullValues: true}")
        check("C204", "O18: ORDER BY walked from a composite index that ignores nulls lacks every record with a null key", col(s, "SELECT id AS v FROM ob ORDER BY f ASC, g ASC"), ["r2", "r0", "r6"])
        check("C205", "O18: ... descending too (3.2.56: nothing)", col(s, "SELECT id AS v FROM ob ORDER BY f DESC, g DESC"), ["r6", "r0", "r2"])
        s.sql("DROP INDEX ob_fg"); s.sql("CREATE INDEX ob_fg ON ob (f, g) NOTUNIQUE METADATA {ignoreNullValues: false}")
        check("C206", "O23: ORDER BY walked from a composite index that keeps nulls puts them FIRST ascending", col(s, "SELECT id AS v FROM ob ORDER BY f ASC, g ASC"), ["r5", "r3", "r1", "r2", "r0", "r4", "r6"])
        check("C207", "O23: ... and LAST descending", col(s, "SELECT id AS v FROM ob ORDER BY f DESC, g DESC"), ["r6", "r4", "r0", "r2", "r1", "r3", "r5"])
        s.sql("DROP INDEX ob_fg"); s.sql("CREATE INDEX ob_f ON ob (f) NOTUNIQUE METADATA {ignoreNullValues: false}")
        check("C209", "a single-field index that keeps nulls orders right", (col(s, "SELECT id AS v FROM ob ORDER BY f ASC")[-2:], col(s, "SELECT id AS v FROM ob ORDER BY f DESC")[:2]), (["r3", "r5"], ["r3", "r5"]))
        check("C211", "an arithmetic UPDATE (f = f + :d) maintains the index", (s.sql("UPDATE ob SET f = f + :d WHERE id = 'r0'", {"d": 10}), col(s, "SELECT id AS v FROM ob WHERE f = 11"))[1], ["r0"])
        check("C212", "UPDATE EDGE CONTENT keeps the endpoints", (s.sql("UPDATE EDGE ce CONTENT {\"w\": 8} WHERE out.id = 'r0' AND in.id = 'r4'"), col(s, "SELECT in.id AS v FROM ce WHERE w = 8"))[1], ["r4"])
        check("C213", "CREATE CLASS ... EXTENDS V CLUSTERS 3 is accepted", err(s, "CREATE CLASS cl3 EXTENDS V CLUSTERS 3"), "ok")
        check("C214", "REBUILD INDEX * is accepted", err(s, "REBUILD INDEX *"), "ok")
        s.sql("CREATE CLASS c2"); s.sql("CREATE PROPERTY c2.id STRING"); s.sql("CREATE PROPERTY c2.b BOOLEAN"); s.sql("CREATE PROPERTY c2.f LONG"); s.sql("CREATE PROPERTY c2.s STRING"); s.sql("ALTER PROPERTY c2.s COLLATE ci")
        for i, b, f, sv in (("a", True, 5, "x"), ("b", True, 7, "X"), ("c", False, 5, "y")):
            s.sql("INSERT INTO c2 SET id = :id, b = :b, f = :f, s = :s", {"id": i, "b": b, "f": f, "s": sv})
        s.sql("CREATE INDEX c2_bf ON c2 (b, f) NOTUNIQUE_HASH_INDEX METADATA {ignoreNullValues: true}"); s.sql("CREATE INDEX c2_bs ON c2 (b, s) NOTUNIQUE METADATA {ignoreNullValues: true}")
        check("C215", "O10: a mixed-box IN on the second field of a composite index, next to an equality on its lead, is the same ClassCastException", err(s, "SELECT id AS v FROM c2 WHERE b = :b AND f IN :p", {"b": True, "p": [5, 2**40]}), "internal:ClassCast")
        check("C216", "O16: ... and a double key on it is truncated", col(s, "SELECT id AS v FROM c2 WHERE b = :b AND f = :p", {"b": True, "p": 5.5}), ["a"])

        print("--- DATETIME / DATE (O27), EMBEDDEDMAP (O7 / O8 faces), the index: door, @class, avg (O28)")
        s.sql("CREATE CLASS dt"); s.sql("CREATE PROPERTY dt.id STRING"); s.sql("CREATE PROPERTY dt.t DATETIME"); s.sql("CREATE PROPERTY dt.d DATE")
        for i, t, d in (("a", "2024-01-01 00:00:00", "2024-01-01"), ("b", "2024-01-01 12:34:56", "2024-01-02")):
            s.sql("INSERT INTO dt SET id = :id, t = :t, d = :d", {"id": i, "t": t, "d": d})
        check("C221", "O27: an ISO 'T' pivot is compared as the date's midnight", col(s, "SELECT id AS v FROM dt WHERE t = :p", {"p": "2024-01-01T12:34:56"}), ["a"])
        check("C222", "O27: ... so is a pivot without seconds", col(s, "SELECT id AS v FROM dt WHERE t = :p", {"p": "2024-01-01 12:34"}), ["a"])
        s.sql("CREATE CLASS mp"); s.sql("CREATE PROPERTY mp.id STRING"); s.sql("CREATE PROPERTY mp.m EMBEDDEDMAP LONG")
        for i, m in (("a", {"k1": 1, "k2": 2}), ("b", {"k2": 5, "k3": 2**40}), ("c", {})):
            s.sql("INSERT INTO mp SET id = :id, m = :m", {"id": i, "m": m})
        s.sql("INSERT INTO mp SET id = 'h'")
        check("C227", "O8: IS DEFINED through a dotted map key holds for every record", sorted(col(s, "SELECT id AS v FROM mp WHERE m.k3 IS DEFINED")), ["a", "b", "c", "h"])
        check("C228", "O7: CONTAINSVALUE with an Integer-boxed pivot misses the Long values; m.values() CONTAINS coerces", (col(s, "SELECT id AS v FROM mp WHERE m CONTAINSVALUE :p", {"p": 2}), col(s, "SELECT id AS v FROM mp WHERE m.values() CONTAINS :p", {"p": 2}), col(s, "SELECT id AS v FROM mp WHERE m CONTAINSVALUE :p", {"p": 2**40})), ([], ["a"], ["b"]))
        s.sql("CREATE INDEX mp_mv ON mp (m BY VALUE) NOTUNIQUE METADATA {ignoreNullValues: true}")
        s.sql("CREATE INDEX mp_mk ON mp (m BY KEY) NOTUNIQUE METADATA {ignoreNullValues: true}")
        check("C233", "O5: ... a BETWEEN through a hash index's door is a 501", err(s, "SELECT rid.id AS v FROM index:c1_f1h WHERE key BETWEEN :a AND :b", {"a": 0, "b": 5}), "internal:unsupported")
        check("C234", "O16: ... the door converts a double key like an index lookup", col(s, "SELECT rid.id AS v FROM index:c1_f1h WHERE key = :p", {"p": 5.5}), ["c"])
        s.sql("CREATE INDEX c2_bsh ON c2 (b, f) NOTUNIQUE_HASH_INDEX METADATA {ignoreNullValues: true}")

        print("--- round 5: CONTAINSALL (O7 / O10 faces), the transaction door (O31), shortestPath maxDepth (O35)")
        s.sql("INSERT INTO n1 SET id = 'n1/dup', f1 = 1")
        s.sql("DELETE FROM n1 WHERE id = 'n1/dup'")
        s.sql("INSERT INTO li SET id = 'h', f = :v", {"v": [2**40, 1]})
        check("C263", "O7: CONTAINSALL with an Integer-boxed element matches nothing on a declared LONG list (never index-served); Long elements match", (col(s, "SELECT id AS v FROM li WHERE f CONTAINSALL :p", {"p": [1]}), col(s, "SELECT id AS v FROM li WHERE f CONTAINSALL :p", {"p": [2**40, 1]}), col(s, "SELECT id AS v FROM li WHERE f CONTAINSALL :p", {"p": [2**40]})), ([], [], ["h"]))
        check("C264", "O10: CONTAINSANY mixing Integer and Long elements through a list index is a ClassCastException; Long elements alone are served", (err(s, "SELECT id AS v FROM li WHERE f CONTAINSANY :p", {"p": [2**40, 1]}), col(s, "SELECT id AS v FROM li WHERE f CONTAINSANY :p", {"p": [2**40]})), ("internal:ClassCast", ["h"]))
        s.sql("DROP INDEX li_f"); s.sql("CREATE INDEX li_fh ON li (f) NOTUNIQUE_HASH_INDEX METADATA {ignoreNullValues: true}")
        check("C265", "O5: CONTAINSANY through a HASH (or DICTIONARY) list index is a 501 (the planner picks the index and cannot evaluate it); CONTAINS scans", (err(s, "SELECT id AS v FROM li WHERE f CONTAINSANY :p", {"p": [1]}), col(s, "SELECT id AS v FROM li WHERE f CONTAINS :p", {"p": 5})), ("internal:unsupported", ["e"]))
        s.sql("DROP INDEX li_fh"); s.sql("CREATE INDEX li_f ON li (f) NOTUNIQUE METADATA {ignoreNullValues: true}")
        s.sql("DELETE FROM li WHERE id = 'h'")
        check("C267", "an aggregate over no rows answers no row at all; count(*) answers 0", (col(s, "SELECT max(f1) AS v FROM n1 WHERE f1 = 12345"), col(s, "SELECT count(*) AS v FROM n1 WHERE f1 = 12345")), ([], [0]))

        for q in ["CREATE CLASS tx", "CREATE PROPERTY tx.id STRING", "CREATE PROPERTY tx.f LONG", "CREATE INDEX tx_id ON tx (id) UNIQUE METADATA {ignoreNullValues: true}", "CREATE INDEX tx_f ON tx (f) NOTUNIQUE METADATA {ignoreNullValues: true}"]:
            s.sql(q)
        for i, v in (("a", 1), ("b", -7), ("c", 5), ("d", 100)):
            s.sql("INSERT INTO tx SET id = :id, f = :v", {"id": i, "v": v})

        def txr(write, read, params=None):
            out = s.script(f"BEGIN; {write}; LET $r = ({read}); ROLLBACK; RETURN $r", params)
            return sorted(r["id"] for r in out[0]["value"])

        check("C270", "O31: a ONE-SIDED range through the index misses the changed key inside the transaction", (txr("UPDATE tx SET f = -100 WHERE id = 'c'", "SELECT id FROM tx WHERE f < 0"), txr("UPDATE tx SET f = 1000 WHERE id = 'c'", "SELECT id FROM tx WHERE f > 99")), (["b"], ["d"]))
        check("C271", "O31: ... finds it only when the bound IS the key, and returns a changed record whose new key does NOT satisfy the range", (txr("UPDATE tx SET f = -100 WHERE id = 'c'", "SELECT id FROM tx WHERE f <= -100"), txr("UPDATE tx SET f = 1000 WHERE id = 'c'", "SELECT id FROM tx WHERE f < 6")), (["c"], ["a", "b", "c"]))
        check("C272", "O31: ... misses a record the transaction inserted, on the unique id index too; a scan and reversed operands are right", (txr("INSERT INTO tx SET id = 'z', f = -100", "SELECT id FROM tx WHERE f < 0"), txr("INSERT INTO tx SET id = 'z', f = -100", "SELECT id FROM tx WHERE id > 'c'"), txr("INSERT INTO tx SET id = 'z', f = -100", "SELECT id FROM tx WHERE :p > f", {"p": 0})), (["b"], ["d"], ["b", "z"]))
        s.sql("CREATE CLASS cw"); s.sql("CREATE PROPERTY cw.id STRING"); s.sql("CREATE PROPERTY cw.f LONG"); s.sql("CREATE PROPERTY cw.g LONG")
        for i, fv, gv in (("a", 1, 10), ("b", -5, None), ("c", 7, 70), ("d", None, 5)):
            s.sql("INSERT INTO cw SET id = :id" + (", f = :f" if fv is not None else "") + (", g = :g" if gv is not None else ""), {"id": i, "f": fv, "g": gv})
        s.sql("CREATE INDEX cw_fg ON cw (f, g) NOTUNIQUE METADATA {ignoreNullValues: true}")
        check("C273", "O18: an ASC ORDER BY walked from a COMPOSITE index that ignores nulls skips a record whose OTHER component is null (min through ORDER BY ... LIMIT 1 is wrong; campaign c100e, seed 1049)", (col(s, "SELECT min(f) AS v FROM cw"), col(s, "SELECT f AS v FROM cw WHERE f IS NOT NULL ORDER BY f ASC LIMIT 1"), col(s, "SELECT f AS v FROM cw ORDER BY f ASC, id ASC")), ([-5], [1], [-5, 1, 7, None]))
        s.sql("DROP INDEX cw_fg"); s.sql("CREATE INDEX cw_fg ON cw (f, g) NOTUNIQUE METADATA {ignoreNullValues: false}")
        check("C274", "O23: ... with nulls kept, the walk answers every record (null keys first ascending)", col(s, "SELECT f AS v FROM cw ORDER BY f ASC"), [None, -5, 1, 7])
        for name, write in (("an update of the column", "UPDATE tx SET f = 9 WHERE id = 'c'"), ("an insert", "INSERT INTO tx SET id = 'z', f = 2"), ("a delete", "DELETE FROM tx WHERE id = 'a'")):
            try:
                s.script(f"BEGIN; {write}; LET $r = (SELECT id FROM tx WHERE f >= 6 AND f <= 4); ROLLBACK; RETURN $r")
                got = "ok"
            except OrientError as e:
                got = "internal:other" if e.internal and "fromKey" in str(e) else "?"
            check("C276", f"O31: ... after {name}: the reversed range is the IllegalArgumentException", got, "internal:other")
        for q in ["CREATE CLASS sv EXTENDS V", "CREATE PROPERTY sv.id STRING", "CREATE INDEX sv_id ON sv (id) UNIQUE METADATA {ignoreNullValues: true}", "CREATE CLASS se EXTENDS E"]:
            s.sql(q)
        srid = {v: s.sql_raw("CREATE VERTEX sv SET id = :id", {"id": v})[0]["@rid"] for v in "abcd"}
        for x, y in (("a", "b"), ("b", "c"), ("c", "d")):
            s.sql("CREATE EDGE se FROM :a TO :b", {"a": srid[x], "b": srid[y]})
        SP = "SELECT shortestPath((SELECT FROM sv WHERE id = :a), (SELECT FROM sv WHERE id = :b), 'OUT', 'se'{o}).size() AS v"
        check("C279", "shortestPath() answers the path's vertex count, 0 when unreachable, 1 for the same record; a subquery names each end", (col(s, SP.format(o=""), {"a": "a", "b": "c"}), col(s, SP.format(o=""), {"a": "c", "b": "a"}), col(s, SP.format(o=""), {"a": "a", "b": "a"})), ([3], [0], [1]))
        check("C280", "O35: shortestPath's maxDepth k finds paths of at most k - 1 hops", (col(s, SP.format(o=", :o"), {"a": "a", "b": "c", "o": {"maxDepth": 2}}), col(s, SP.format(o=", :o"), {"a": "a", "b": "c", "o": {"maxDepth": 3}})), ([0], [3]))
        check("C281", "O35: ... where TRAVERSE MAXDEPTH k and MATCH maxDepth k / while ($depth < k) find k hops; none of them answers a row when unreachable", (col(s, "SELECT min($depth) + 1 AS v FROM (TRAVERSE out('se') FROM (SELECT FROM sv WHERE id = :a) MAXDEPTH 2) WHERE id = :b", {"a": "a", "b": "c"}), col(s, "MATCH {class: sv, as: a, where: (id = :a)}.out('se'){as: b, maxDepth: 2, where: (id = :b), depthAlias: dd} RETURN min(dd) + 1 AS v", {"a": "a", "b": "c"}), col(s, "MATCH {class: sv, as: a, where: (id = :a)}.out('se'){as: b, while: ($depth < 2), where: (id = :b), depthAlias: dd} RETURN min(dd) + 1 AS v", {"a": "a", "b": "c"}), col(s, "SELECT min($depth) + 1 AS v FROM (TRAVERSE out('se') FROM (SELECT FROM sv WHERE id = :a) MAXDEPTH 1) WHERE id = :b", {"a": "a", "b": "c"})), ([3], [3], [3], []))
        # a decoy path a -> x -> y -> c created AFTER a -> b: depth-first expands the
        # last-discovered child (x) first and discovers c at depth 3 before b is
        # expanded; breadth-first discovers it at 2
        for v in ("x", "y"):
            srid[v] = s.sql_raw("CREATE VERTEX sv SET id = :id", {"id": v})[0]["@rid"]
        for x, y in (("a", "x"), ("x", "y"), ("y", "c")):
            s.sql("CREATE EDGE se FROM :a TO :b", {"a": srid[x], "b": srid[y]})
        check("C282", "TRAVERSE's default strategy is DEPTH_FIRST and $depth is the depth of first DISCOVERY in that order (the last-discovered child is expanded first), so min($depth) is the BFS distance only under BREADTH_FIRST (campaign c100e, seeds 1009 and 1087)", (col(s, "SELECT $depth AS v FROM (TRAVERSE out('se') FROM (SELECT FROM sv WHERE id = 'a') MAXDEPTH 5) WHERE id = 'c'"), col(s, "SELECT $depth AS v FROM (TRAVERSE out('se') FROM (SELECT FROM sv WHERE id = 'a') MAXDEPTH 5 STRATEGY DEPTH_FIRST) WHERE id = 'c'"), col(s, "SELECT $depth AS v FROM (TRAVERSE out('se') FROM (SELECT FROM sv WHERE id = 'a') MAXDEPTH 5 STRATEGY BREADTH_FIRST) WHERE id = 'c'")), ([3], [3], [2]))
        s.sql("DELETE VERTEX sv WHERE id IN ['x', 'y']")

        print("--- the SELECT IR: aggregates over no rows, subquery targets, the index: door over a composite")
        for q in ["CREATE CLASS sq", "CREATE PROPERTY sq.id STRING", "CREATE PROPERTY sq.f LONG", "CREATE PROPERTY sq.g STRING", "ALTER PROPERTY sq.g COLLATE ci", "CREATE PROPERTY sq.b BOOLEAN",
                  "CREATE INDEX sq_f ON sq (f) NOTUNIQUE METADATA {ignoreNullValues: true}", "CREATE INDEX sq_bf ON sq (b, f) NOTUNIQUE METADATA {ignoreNullValues: true}"]:
            s.sql(q)
        for rid, f, g, b in [("a", 3, "Ab", True), ("b", 1, "ab", True), ("c", None, "x", True), ("d", 7, None, False)]:
            s.sql("INSERT INTO sq SET id = :id, f = :f, g = :g, b = :b", {"id": rid, "f": f, "g": g, "b": b})
        none = "FROM sq WHERE f > 100"
        check("C285", "count(*) and count(f) over no rows answer 0; max / sum / avg / median / set answer NO row", [rows(s, f"SELECT {fn} AS v {none}") for fn in ("count(*)", "count(f)", "max(f)", "sum(f)", "avg(f)", "median(f)", "set(f)")], [[{"v": 0}], [{"v": 0}], [], [], [], [], []])
        check("C287", "O18 under a WHERE on another field: the DESC walk still omits the null record (3.2.56: no rows); over a subquery target it sorts", (col(s, "SELECT f AS v FROM sq WHERE b = true ORDER BY f DESC", key="v"), col(s, "SELECT f AS v FROM (SELECT FROM sq WHERE b = true) ORDER BY f DESC"), col(s, "SELECT f AS v FROM (SELECT FROM sq) WHERE b = true ORDER BY f DESC")), ([3, 1], [None, 3, 1], [None, 3, 1]))

        print("--- the write IR: predicate-targeted writes (O37, O38, O39)")
        TW = " TIMEOUT 4000 EXCEPTION"
        for q in ["CREATE CLASS wt", "CREATE PROPERTY wt.id STRING", "CREATE PROPERTY wt.f LONG", "CREATE PROPERTY wt.u LONG", "CREATE PROPERTY wt.m EMBEDDEDMAP LONG",
                  "CREATE INDEX wt_f ON wt (f) NOTUNIQUE METADATA {ignoreNullValues: true}", "CREATE INDEX wt_u ON wt (u) UNIQUE METADATA {ignoreNullValues: true}"]:
            s.sql(q)

        def wreset():
            s.sql("DELETE FROM wt")
            for i, f in enumerate([1, 5, 6, 7, 8, None]):
                s.sql("INSERT INTO wt SET id = :id, f = :f, u = :u, m = :m", {"id": f"w{i}", "f": f, "u": i, "m": {"k": 2**40 if i == 1 else i}})

        def wf():
            return [r.get("f") for r in sorted(rows(s, "SELECT id, f FROM wt"), key=lambda r: r["id"])]

        def werr(q, p=None):
            e = err(s, q, p)
            return "timeout" if e == "rejected" and "Timeout" in _last_error(s, q, p) else e

        wreset()
        check("C291", "UPDATE / DELETE answer the count; RETURN AFTER / BEFORE a row per matched record", (rows(s, "UPDATE wt SET u = u WHERE f < 6" + TW), rows(s, "UPDATE wt SET f = f RETURN AFTER $current.id AS id, $current.f AS v WHERE id = 'w1'" + TW)), ([{"count": 2}], [{"id": "w1", "v": 5}]))
        wreset()
        check("C293", "O37: `SET f = f + 1 WHERE f > 4` through an index on f never terminates (the TIMEOUT stops it, the records it wrote stay written: one of them far past its start)", (werr("UPDATE wt SET f = f + 1 WHERE f > 4" + TW), max(v for v in wf() if v is not None) > 1000), ("timeout", True))
        wreset()
        check("C294", "O37: ... `SET f = 100` counts every record twice", rows(s, "UPDATE wt SET f = 100 WHERE f > 4" + TW), [{"count": 8}])
        wreset()
        check("C295", "O37: ... a two-sided range walks the record up to its bound", (rows(s, "UPDATE wt SET f = f + 1 WHERE f > 4 AND f < 100" + TW), wf()[4]), ([{"count": 95}], 100))
        wreset()
        check("C297", "a UNIQUE index is checked per row and a failed UPDATE keeps what it wrote (no statement atomicity outside a transaction)", (err(s, "UPDATE wt SET u = 99 WHERE f > 4" + TW), sorted(r["u"] for r in rows(s, "SELECT u FROM wt WHERE u = 99"))), ("rejected", [99]))
        wreset()
        check("C300", "O39: CONTAINSVALUE inside a subquery consumed as a value (IN, NOT IN, LET) is a NullPointerException; as a FROM target, and CONTAINSKEY / values() CONTAINS there, are fine", (err(s, "SELECT id FROM wt WHERE id IN (SELECT id FROM wt WHERE m CONTAINSVALUE :p)", {"p": 2**40}), err(s, "SELECT id, $a.size() AS v FROM wt LET $a = (SELECT id FROM wt WHERE m CONTAINSVALUE :p)", {"p": 2**40}), col(s, "SELECT id AS v FROM (SELECT FROM wt WHERE m CONTAINSVALUE :p)", {"p": 2**40}), col(s, "SELECT id AS v FROM wt WHERE id IN (SELECT id FROM wt WHERE m.values() CONTAINS :p)", {"p": 2**40})), ("internal:NPE", "internal:NPE", ["w1"], ["w1"]))
        for q in ["CREATE CLASS wd", "CREATE PROPERTY wd.id STRING", "CREATE PROPERTY wd.w LONG", "CREATE PROPERTY wd.e1 EMBEDDEDLIST"]:
            s.sql(q)

        def dreset():
            s.sql("DELETE FROM wd")
            r = {i: s.sql_raw("INSERT INTO wd SET id = :id", {"id": f"d{i}"})[0]["@rid"] for i in range(3)}
            s.sql("UPDATE wd SET e1 = :l WHERE id = 'd0'", {"l": [{"target": r[1], "eid": "x0", "w": 1}, {"target": r[2], "eid": "x1", "w": 5}]})

        # the doc data model's edge-attribute write and cascade: a list position at a time
        for q in ["CREATE CLASS wpt", "CREATE PROPERTY wpt.id STRING", "CREATE CLASS wps", "CREATE PROPERTY wps.id STRING", "CREATE PROPERTY wps.e1 EMBEDDEDLIST"]:
            s.sql(q)

        def preset():
            s.sql("DELETE FROM wps"); s.sql("DELETE FROM wpt")
            r = {k: s.sql_raw("INSERT INTO wpt SET id = :id", {"id": k})[0]["@rid"] for k in ("t1", "t2", "t3")}
            for sid, lst in (("a", [("x1", "t1", 5), ("x2", "t2", 2), ("x3", "t3", 7)]), ("b", [("y1", "t1", 1)]), ("c", [("z1", "t2", 6)])):
                s.sql("INSERT INTO wps SET id = :id", {"id": sid})
                s.sql("UPDATE wps SET e1 = :l WHERE id = :id", {"id": sid, "l": [{"eid": e, "target": r[t], "w": w} for e, t, w in lst]})

        def pstate():
            return {r["id"]: [list(x) for x in zip(r.get("eid") or [], r.get("w") or [])] for r in rows(s, "SELECT id, e1.eid AS eid, e1.w AS w FROM wps ORDER BY id")}

        preset()
        check("C339", "`SET e[k].f = ... WHERE e.size() > k AND p(e[k])` sets the matching element of each list in place, one count per matching record; `SET e[p].f = ...` answers its count and changes nothing",
              ([rows(s, f"UPDATE wps SET e1[{k}].w = 9 WHERE e1.size() > {k} AND e1[{k}].w > :p", {"p": 4})[0]["count"] for k in range(3)], pstate(), (preset(), rows(s, "UPDATE wps SET e1[w > :p].w = 9", {"p": 4}), pstate())[1:]),
              ([2, 0, 1], {"a": [["x1", 9], ["x2", 2], ["x3", 9]], "b": [["y1", 1]], "c": [["z1", 9]]}, ([{"count": 3}], {"a": [["x1", 5], ["x2", 2], ["x3", 7]], "b": [["y1", 1]], "c": [["z1", 6]]})))
        preset()
        check("C340", "a list position past the end counts the record and changes nothing", (rows(s, "UPDATE wps SET e1[2].w = 9 WHERE e1[2].w IS NULL"), pstate()["b"]), ([{"count": 2}], [["y1", 1]]))
        s.sql("UPDATE wps SET e1[0].w = :v WHERE id = 'b'", {"v": 2**31 - 1})
        check("C341", "an untyped embedded number that fits is an INTEGER, and `asLong()` arithmetic on it is the exact LONG",
              (col(s, "SELECT e1[0].w.type() AS v FROM wps WHERE id = 'b'"), (s.sql("UPDATE wps SET e1[0].w = e1[0].w.asLong() + :d WHERE id = 'b'", {"d": 1}), col(s, "SELECT e1[0].w AS v FROM wps WHERE id = 'b'"))[1]),
              (["INTEGER"], [2**31]))
        s.sql("UPDATE wps SET e1 = :l WHERE id = 'b'", {"l": [{"eid": "y1", "w": 1}, {"eid": "y2"}, {"eid": "y3", "w": None}]})
        check("C343", "O8: `e[k].f IS DEFINED` holds for a list element without f (and IS NOT DEFINED for none); `e[k].keys() CONTAINS 'f'` tells absent from null",
              ([col(s, f"SELECT count(*) AS v FROM wps WHERE id = 'b' AND e1[{k}].w IS DEFINED")[0] for k in range(3)], [col(s, f"SELECT count(*) AS v FROM wps WHERE id = 'b' AND e1[{k}].keys() CONTAINS 'w'")[0] for k in range(3)]),
              ([1, 1, 1], [1, 0, 1]))
        preset()
        check("C342", "after a DELETE a link to the record reads `target.id` null, and the bracket drops it (in a transaction too)",
              (s.script("BEGIN; LET $r = DELETE FROM wpt WHERE id = 't1'; UPDATE wps SET e1 = e1[target.id IS NOT NULL] WHERE e1 CONTAINS (target.id IS NULL); COMMIT; RETURN $r"), pstate()),
              ([{"count": 1}], {"a": [["x2", 2], ["x3", 7]], "b": [], "c": [["z1", 6]]}))
        dreset()
        check("C301", "a link list is rewritten through a bracket filter, the links stay typed; DELETE FROM leaves a LINK dangling (target.id null)", (rows(s, "UPDATE wd SET e1 = e1[target.id NOT IN :g] WHERE e1 CONTAINS (target.id IN :g)" + TW, {"g": ["d1"]}), col(s, "SELECT e1.target.id AS v FROM wd WHERE id = 'd0'"), (dreset(), rows(s, "DELETE FROM wd WHERE id = 'd1'"))[1], col(s, "SELECT e1.target.id AS v FROM wd WHERE id = 'd0'")), ([{"count": 1}], [["d2"]], [{"count": 1}], [[None, "d2"]]))
        dreset()
        check("C302", "O38: in a transaction, a list rewritten through a filter that dereferences its LINKs and a delete of a dropped link's target fail at COMMIT (auto-commit and an attribute filter are fine)", (err_script(s, "BEGIN; UPDATE wd SET e1 = e1[target.id <> 'd1'] WHERE id = 'd0'; DELETE FROM wd WHERE id = 'd1'; COMMIT;"), (dreset(), s.script("BEGIN; UPDATE wd SET e1 = e1[w > 4] WHERE id = 'd0'; DELETE FROM wd WHERE id = 'd1'; COMMIT;"))[1]), ("rejected", [{"operation": "commit"}]))

        print("--- the select and path IRs (join, semi-join, edge targets, the transaction door, paths)")
        for q in ["CREATE CLASS QZ0 ABSTRACT", "CREATE PROPERTY QZ0.id STRING", "CREATE CLASS qzn EXTENDS QZ0", "CREATE PROPERTY qzn.f LONG", "CREATE PROPERTY qzn.g STRING",
                  "CREATE INDEX qzn_f ON qzn (f) NOTUNIQUE METADATA {ignoreNullValues: false}",
                  "CREATE CLASS qzj", "CREATE PROPERTY qzj.src_id STRING", "CREATE PROPERTY qzj.dst_id STRING",
                  "CREATE CLASS qzd", "CREATE PROPERTY qzd.id STRING", "CREATE PROPERTY qzd.e1 EMBEDDEDLIST"]:
            s.sql(q)
        for i, (f, g) in enumerate([(1, "a"), (2, "b"), (2, "a"), (None, "c"), (5, None)]):
            s.sql("INSERT INTO qzn SET id = :id, f = :f, g = :g", {"id": f"p{i}", "f": f, "g": g})
        for q in ["CREATE CLASS qzl EXTENDS QZ0", "CREATE PROPERTY qzl.f EMBEDDEDLIST LONG"]:
            s.sql(q)
        s.sql("INSERT INTO qzl SET id = 'l0', f = [1, 2]")
        check("C305", "O5's list face: a stored LIST compared through `<=` with a scalar is a ClassCastException (a LONG field against a list answers false on a scan, `=` answers); a join over the base class evaluates its LET for every record before `WHERE @class`, so it meets a sibling's same-named list: on the left (a reversed correlation) it throws, on the right of a LONG it does not",
              (err(s, "SELECT id FROM qzl WHERE f <= 2.5"), col(s, "SELECT id AS v FROM (SELECT FROM qzn) WHERE f <= :p", {"p": [1, 2]}), col(s, "SELECT id AS v FROM qzl WHERE f = 2"),
               err(s, "SELECT id, $m.size() AS v FROM QZ0 LET $m = (SELECT id FROM qzn WHERE $parent.$current.f >= f) WHERE @class = 'qzn'"),
               err(s, "SELECT id, $m.size() AS v FROM QZ0 LET $m = (SELECT id FROM qzn WHERE f <= $parent.$current.f) WHERE @class = 'qzn'")),
              ("internal:ClassCast", [], [], "internal:ClassCast", "ok"))
        s.sql("DELETE FROM qzl")
        for q in ["CREATE CLASS qzm", "CREATE PROPERTY qzm.m EMBEDDEDMAP LONG"]:
            s.sql(q)
        s.sql("INSERT INTO qzm SET m = :m", {"m": {"k3": -49}})
        check("C306", "O17's map-entry face: a map entry's BETWEEN truncates a double bound like a LONG column's (`m.k3 BETWEEN -54 AND -49.5` admits -49; `>= AND <=` does not)",
              (len(rows(s, "SELECT FROM qzm WHERE m.k3 BETWEEN :a AND :b", {"a": -54, "b": -49.5})), len(rows(s, "SELECT FROM qzm WHERE m.k3 >= :a AND m.k3 <= :b", {"a": -54, "b": -49.5}))),
              (1, 0))
        for q in ["CREATE CLASS qzx", "CREATE PROPERTY qzx.d DECIMAL", "CREATE PROPERTY qzx.l LONG", "CREATE PROPERTY qzx.i INTEGER", "CREATE PROPERTY qzx.t DATETIME", "CREATE PROPERTY qzx.b BOOLEAN"]:
            s.sql(q)
        s.sql("INSERT INTO qzx SET d = 2, l = 2, i = 2, t = '2024-01-01 00:00:00', b = true")
        check("C307", "O5's mixed-type face, field against field: DECIMAL = LONG throws (DECIMAL = INTEGER and LONG = DECIMAL answer); DECIMAL < DATETIME, DATETIME < BOOLEAN and BOOLEAN < DATETIME throw (`guards.mixed_compare_throws`)",
              [err(s, f"SELECT count(*) AS v FROM qzx WHERE {c}") for c in ("d = l", "d = i", "l = d", "d < t", "d = t", "t < b", "b < t", "l < t")],
              ["internal:ClassCast", "ok", "ok", "internal:ClassCast", "ok", "internal:ClassCast", "internal:ClassCast", "ok"])
        tr = {k: s.sql_raw("INSERT INTO qzn SET id = :id", {"id": k})[0]["@rid"] for k in ("t0", "t1")}
        s.sql("INSERT INTO qzd SET id = 'd0', e1 = :l", {"l": [{"target": tr["t0"], "eid": "x0", "w": 1}, {"target": tr["t1"], "eid": "x1", "w": 5}, {"target": tr["t1"], "eid": "x2"}]})
        s.sql("INSERT INTO qzd SET id = 'd1', e1 = []")
        s.sql("INSERT INTO qzd SET id = 'd2'")
        edge = "SELECT v AS id FROM (SELECT v FROM (SELECT {c} AS v FROM qzd UNWIND v) WHERE v IS NOT NULL)"
        check("C310", "the doc side's relationship records: a bracket filter over the link documents unwound, an empty or absent list dropped; the rewritten, reversed and arithmetic filters answer alike inside the bracket",
              [col(s, edge.format(c=c), {"p": 0, "c": 1, "q": 1}, key="id") for c in ("e1[w > :p].eid", "e1.eid", "e1[NOT (w <= :p) AND w IS NOT NULL].eid", "e1[:p < w].eid", "e1[(w + :c) > :q AND w IS NOT NULL].eid")],
              [["x0", "x1"], ["x0", "x1", "x2"], ["x0", "x1"], ["x0", "x1"], ["x0", "x1"]])
        TWT = " TIMEOUT 20000 EXCEPTION"

        def tx(write, read):
            return unwrap(s.script(f"BEGIN; {write}; LET $r = ({read}{TWT}); ROLLBACK; RETURN $r"))

        def unwrap(r):
            return [{k: v for k, v in x.items() if not k.startswith("@")} for x in r[0]["value"]] if r and "value" in r[0] else r

        check("C312", "O31 under every kind: `max` / `count` / a projection through a one-sided range an SB-tree index serves miss or invert the written records",
              (tx("UPDATE qzn SET f = 9 WHERE id = 'p0'", "SELECT max(f) AS v FROM qzn WHERE f > 1"), tx("INSERT INTO qzn SET id = 'tx0', f = 3", "SELECT count(*) AS v FROM qzn WHERE f > 2"),
               tx("UPDATE qzn SET f = 9 WHERE id = 'p0'", "SELECT max(f) AS v FROM (SELECT FROM qzn) WHERE f > 1")),
              ([{"v": 5}], [{"v": 1}], [{"v": 9}]))
        for x, y in [("a", "b"), ("b", "c"), ("c", "d"), ("b", "b")]:
            s.sql("INSERT INTO qzj SET src_id = :a, dst_id = :b", {"a": x, "b": y})
        lv = ["(SELECT 0 AS d FROM qzn WHERE id = :a AND id = :b)", "(SELECT 1 AS d FROM qzj WHERE src_id = :a AND dst_id = :b)",
              "(SELECT 2 AS d FROM qzj WHERE src_id IN (SELECT dst_id FROM qzj WHERE src_id = :a) AND dst_id = :b)",
              "(SELECT 3 AS d FROM qzj WHERE src_id IN (SELECT dst_id FROM qzj WHERE src_id IN (SELECT dst_id FROM qzj WHERE src_id = :a)) AND dst_id = :b)"]
        P = {"a": "a", "b": "d"}
        check("C313", "O40: inside `expand(unionall(...))` a part after the first whose `IN (subquery)` nests another subquery answers nothing (first, LET-bound, or alone it is right)",
              (col(s, f"SELECT min(d) + 1 AS v FROM (SELECT expand(unionall({', '.join(lv)})))", P),
               col(s, f"SELECT min(d) + 1 AS v FROM (SELECT expand(unionall({', '.join(reversed(lv))})))", P),
               col(s, "SELECT min(d) + 1 AS v FROM (SELECT expand($c) LET " + ", ".join(f"$l{i} = {x}" for i, x in enumerate(lv)) + ", $c = unionall($l0, $l1, $l2, $l3))", P),
               col(s, f"SELECT d AS v FROM {lv[3]}", P)),
              ([], [4], [4], [3]))
        pv = {k: s.sql_raw("INSERT INTO qzd SET id = :id", {"id": k})[0]["@rid"] for k in ("A", "B", "C", "D")}
        for a, l in {"A": ["B"], "B": ["C", "B"], "C": ["D"]}.items():
            s.sql("UPDATE qzd SET e1 = :l WHERE id = :id", {"id": a, "l": [{"target": pv[b], "eid": f"{a}{b}"} for b in l]})
        check("C314", "the doc side's TRAVERSE over its link lists: an embedded link document adds no depth (`min($depth)` is the hop count, BREADTH_FIRST); MAXDEPTH bounds the hops",
              [col(s, "SELECT min($depth) + 1 AS v FROM (TRAVERSE e1, target FROM (SELECT FROM qzd WHERE id = 'A') MAXDEPTH " + str(k) + " STRATEGY BREADTH_FIRST) WHERE id = :b", {"b": b}) for b, k in (("A", 3), ("C", 3), ("D", 3), ("D", 2))],
              [[1], [3], [4], []])

        print("--- the cov400 triage: DISTINCT (O41), list IN (O43), depth-first TRAVERSE (O44), O5's sibling faces, O31's walk face")
        # one cluster, so the scan order is the insert order whatever the core count
        # (a default class has a cluster per core and the scan interleaves them)
        for q in ["CREATE CLASS q41 CLUSTERS 1", "CREATE PROPERTY q41.v LONG"]:
            s.sql(q)
        # every multiple of 2^32 + 1 hashes to 0 (`v ^ v >>> 32`); the bin treeifies at
        # the tenth, and a repeat of 0 after the eleventh is missed by the tree search
        values41 = ", ".join(f"({v})" for v in [x * (2**32 + 1) for x in range(11)] + [0])
        s.sql(f"INSERT INTO q41 (v) VALUES {values41}")
        check("C315", "O41: DISTINCT answers a value twice once the dedup set's bin of equal-hash rows treeifies (OResultInternal.compareTo answers 1); GROUP BY and set() are right",
              (len(rows(s, "SELECT DISTINCT v FROM q41")), len(rows(s, "SELECT v FROM q41 GROUP BY v")), col(s, "SELECT set(v).size() AS v FROM q41")),
              (12, 11, [11]))
        s.sql("CREATE CLASS q43")
        s.sql('INSERT INTO q43 CONTENT {"e": [{"k": "a", "f": [-1, 0, 1]}, {"k": "b", "f": [0, 0, 0]}, {"k": "c", "f": [9007199254740992]}, {"k": "d", "f": [5]}]}')
        check("C317", "O43: `IN` with a list on the left unwraps a single-element listed list and tests containment, a multi-element one is list equality (in a bracket filter and a plain WHERE alike); over an undeclared (Integer-boxed) list the Long is narrowed to int ([[2^53]] matches the lists holding 0); `=` is list equality",
              (col(s, "SELECT e[f IN :q].k AS v FROM q43", {"q": [[0]]}), col(s, "SELECT e[f IN :q].k AS v FROM q43", {"q": [[2**53]]}), col(s, "SELECT e[f IN :q].k AS v FROM q43", {"q": [[5], [0, 0, 0]]}), col(s, "SELECT e[f = :p].k AS v FROM q43", {"p": [0]}),
               col(s, "SELECT value.k AS v FROM (SELECT expand(e) FROM q43) WHERE value.f IN :q", {"q": [[0]]})),
              ([["a", "b"]], [["a", "b", "c"]], [["b", "d"]], [[]], ["a", "b"]))
        for q in ["CREATE CLASS q44 EXTENDS V", "CREATE PROPERTY q44.id STRING", "CREATE CLASS q44e EXTENDS E"]:
            s.sql(q)
        qv = {k: s.sql_raw("CREATE VERTEX q44 SET id = :id", {"id": k})[0]["@rid"] for k in "0abyxz"}
        for a, b in [("0", "b"), ("0", "a"), ("a", "y"), ("y", "x"), ("b", "x"), ("x", "z")]:
            s.sql(f"CREATE EDGE q44e FROM {qv[a]} TO {qv[b]}")
        reach = {st: sorted(col(s, f"SELECT id AS v FROM (TRAVERSE out('q44e') FROM (SELECT FROM q44 WHERE id = '0') MAXDEPTH 3{st})")) for st in ("", " STRATEGY DEPTH_FIRST", " STRATEGY BREADTH_FIRST")}
        check("C318", "O44: TRAVERSE MAXDEPTH 3 depth-first (the default) expands the start's last neighbour first, meets `x` at depth 3 through 0-a-y-x before 0-b-x and never expands it, missing `z`; breadth-first is complete",
              (reach[""], reach[" STRATEGY DEPTH_FIRST"], reach[" STRATEGY BREADTH_FIRST"]), (["0", "a", "b", "x", "y"], ["0", "a", "b", "x", "y"], ["0", "a", "b", "x", "y", "z"]))
        for q in ["CREATE CLASS q5 ABSTRACT", "CREATE PROPERTY q5.b1 BOOLEAN", "CREATE PROPERTY q5.m EMBEDDEDMAP LONG",
                  "CREATE CLASS q5a EXTENDS q5", "CREATE PROPERTY q5a.f1 DATE", "CREATE PROPERTY q5a.f2 DATETIME",
                  "CREATE CLASS q5b EXTENDS q5", "CREATE PROPERTY q5b.f1 BOOLEAN", "CREATE PROPERTY q5b.f2 EMBEDDEDLIST LONG",
                  "CREATE INDEX q5_b1 ON q5 (b1) NOTUNIQUE_HASH_INDEX", "CREATE INDEX q5a_f2 ON q5a (f2) NOTUNIQUE", "CREATE INDEX q5b_f2 ON q5b (f2) NOTUNIQUE", "CREATE INDEX q5_m ON q5 (m BY VALUE) NOTUNIQUE_HASH_INDEX"]:
            s.sql(q)
        s.sql("INSERT INTO q5a SET b1 = false, f1 = '1980-01-01', f2 = '2024-08-18 08:18:40', m = {'k': 5}")
        s.sql("INSERT INTO q5b SET b1 = false, f1 = true, f2 = [1, 2]")
        D = {"a": "1978-07-01", "b": "2000-02-28"}
        check("C319", "O5's sibling faces: a subclass query a base-class index serves filters the siblings' records before the class (a BETWEEN under an OR throws on a sibling's BOOLEAN), a base query converts the pivot through a sibling's index, CONTAINSVALUE through a hash BY VALUE map index is a 501",
              (err(s, "SELECT FROM q5a WHERE b1 = false AND (b1 = true OR f1 BETWEEN :a AND :b)", D), err(s, "SELECT FROM q5a WHERE b1 = false AND f1 BETWEEN :a AND :b", D),
               err(s, "SELECT FROM q5 WHERE @class = 'q5a' AND f2 = :p", {"p": "2024-08-18 08:18:40"}), err(s, "SELECT FROM q5a WHERE m CONTAINSVALUE :p", {"p": 5}), err(s, "SELECT FROM q5a WHERE m CONTAINSKEY :p", {"p": "k"})),
              ("internal:other", "ok", "internal:other", "internal:unsupported", "ok"))
        for q in ["CREATE CLASS q31", "CREATE PROPERTY q31.id STRING", "CREATE PROPERTY q31.f LONG", "CREATE INDEX q31_f ON q31 (f) NOTUNIQUE METADATA {ignoreNullValues: false}"]:
            s.sql(q)
        for i, v in enumerate([None, 131, 428, 894, 2**63 - 1, -374]):
            s.sql("INSERT INTO q31 SET id = :i, f = :v", {"i": f"r{i}", "v": v})

        def walk(write, order):
            return [r.get("f") for r in unwrap(s.script(f"BEGIN; {write}; LET $r = (SELECT f FROM q31 ORDER BY {order}); ROLLBACK; RETURN $r"))]

        check("C320", "O31's walk face: after the transaction writes the key, a DESC walk of an SB-tree index answers the written record first (after the nulls); ASC, an id tiebreak and a write of null are right",
              (walk("UPDATE q31 SET f = 500 WHERE id = 'r1'", "f DESC"), walk("INSERT INTO q31 SET id = 'n', f = 500", "f DESC LIMIT 2"),
               walk("UPDATE q31 SET f = 500 WHERE id = 'r1'", "f ASC"), walk("UPDATE q31 SET f = 500 WHERE id = 'r1'", "f DESC, id ASC"), walk("UPDATE q31 SET f = null WHERE id = 'r1'", "f DESC")),
              ([None, 500, 2**63 - 1, 894, 428, -374], [None, 500], [-374, 428, 500, 894, 2**63 - 1, None], [None, 2**63 - 1, 894, 500, 428, -374], [None, None, 2**63 - 1, 894, 428, -374]))

        print("--- large data (ganda/orientdb/engine/limits.py): BINARY, the index key bound, the bulk doors, the request body bound, timeouts")
        for q in ["CREATE CLASS lg", "CREATE PROPERTY lg.id STRING", "CREATE PROPERTY lg.b BINARY", "CREATE PROPERTY lg.s STRING", "CREATE PROPERTY lg.l EMBEDDEDLIST LONG",
                  "CREATE INDEX lg_id ON lg (id) UNIQUE", "CREATE INDEX lg_s ON lg (s) NOTUNIQUE METADATA {ignoreNullValues: true}"]:
            s.sql(q)
        b64 = base64.b64encode(bytes(range(256))).decode()
        s.sql("INSERT INTO lg SET id = 'r0', b = :b", {"b": b64})
        s.sql("INSERT INTO lg SET id = 'r1', b = :b", {"b": ""})
        s.sql("INSERT INTO lg CONTENT :d", {"d": {"id": "r2", "b": "AA=="}})
        s.sql("UPDATE lg MERGE {\"b\": \"eno=\"} WHERE id = 'r2'")
        check("C321", "a BINARY takes base64 text (a parameter, a CONTENT document, a MERGE literal) and answers the same text; an EMPTY byte string is stored as null",
              (col(s, "SELECT b AS v FROM lg WHERE id = 'r0'"), col(s, "SELECT b AS v FROM lg WHERE id = 'r1'"), col(s, "SELECT b AS v FROM lg WHERE id = 'r2'")), ([b64], [None], ["eno="]))
        check("C322", "O46: = on a BINARY matches nothing on a scan (the decoded byte[] is compared by identity) ...", col(s, "SELECT id AS v FROM lg WHERE b = :b", {"b": b64}), [])
        s.sql("CREATE INDEX lg_b ON lg (b) NOTUNIQUE METADATA {ignoreNullValues: true}")

        check("C324", "an SB-tree index key is at most 10240 bytes, the UTF-8 value plus 16 (OTooBigIndexKeyException; a hash index has no bound)",
              (err(s, "INSERT INTO lg SET id = 'k1', s = :s", {"s": "a" * 10000}), err(s, "INSERT INTO lg SET id = 'k2', s = :s", {"s": "a" * 10240}), err(s, "INSERT INTO lg SET id = 'k3', s = :s", {"s": chr(0x4E2D) * 3420})),
              ("ok", "rejected", "rejected"))
        # the big values go into unindexed columns (an indexed one is bounded above)
        s.sql("DROP INDEX lg_b")
        s.sql("DROP INDEX lg_s")
        big = "x" * (4 * 1024 * 1024)
        s.sql("INSERT INTO lg SET id = 'big', b = :b", {"b": base64.b64encode(b"\xff" * (3 * 1024 * 1024)).decode()})

        s.sql("UPDATE lg SET l = :l WHERE id = 'big'", {"l": list(range(200000))})
        s.sql("INSERT INTO lg SET id = 'bigs', s = :s", {"s": big})
        check("C325", "a 4 MB string, a 3 MB binary and a 200,000-element list round-trip (the server runs with network.http.maxLength raised past the image's 1 MB; an attached server must be too)",
              (col(s, "SELECT s.length() AS v FROM lg WHERE id = 'bigs'"), col(s, "SELECT b.size() AS v FROM lg WHERE id = 'big'"), col(s, "SELECT l.size() AS v FROM lg WHERE id = 'big'")), ([4 * 1024 * 1024], [3 * 1024 * 1024], [200000]))
        parts, params = [], {}
        for i in range(500):
            params[f"s{i}_id"] = f"c{i}"
            parts.append(f"LET $c{i} = INSERT INTO lg SET id = :s{i}_id;")
        s.script(" ".join(parts) + " RETURN 1;", params)
        check("C326", "a NON-transactional script of 500 `LET $c = INSERT` statements auto-commits each (the plain door's bulk form)", col(s, "SELECT count(*) AS v FROM lg WHERE id LIKE 'c%'"), [500])
        check("C327", "500 ids resolve in one `id IN :ids` lookup", len(s.sql_raw("SELECT id, @rid FROM lg WHERE id IN :ids", {"ids": [f"c{i}" for i in range(500)]})), 500)
        rids = [r["@rid"] for r in s.sql_raw("SELECT @rid FROM lg WHERE id LIKE 'c%'")]
        check("C328", "500 record ids resolve in one `@rid IN :r` lookup (the doc data model's link targets)", len(s.sql_raw("SELECT id, @rid FROM lg WHERE @rid IN :r", {"r": rids})), 500)
        check("C329", "count(*) and sum(list.size()) read a class's sizes without its rows (the sampled state check)",
              (col(s, "SELECT count(*) AS v FROM lg")[0] >= 505, col(s, "SELECT sum(l.size()) AS v FROM lg")), (True, [200000]))

        def timed_out() -> str:
            try:
                s.sql("SELECT count(*) AS v FROM lg LET $x = (SELECT count(*) FROM lg WHERE s LIKE '%zz%') TIMEOUT 1 EXCEPTION", timeout_ms=0)
                return "answered"
            except OrientError as e:
                return "timeout" if e.timeout else "rejected"

        check("C330", "a `TIMEOUT ... EXCEPTION` that fires is OTimeoutException, classified as a timeout (no answer), never as a rejection", timed_out(), "timeout")

        print("--- what the renderers and the mapping assume")
        for q in ["CREATE CLASS cio", "CREATE PROPERTY cio.id STRING", "CREATE PROPERTY cio.s STRING", "ALTER PROPERTY cio.s COLLATE ci",
                  "CREATE CLASS abp", "CREATE PROPERTY abp.id STRING", "CREATE PROPERTY abp.f LONG",
                  "CREATE CLASS exw", "CREATE PROPERTY exw.id STRING", "CREATE PROPERTY exw.e1 EMBEDDEDLIST"]:
            s.sql(q)
        for v in ("a", "B"):
            s.sql("INSERT INTO cio SET id = :v, s = :v", {"v": v})
        ci_ops = {op: col(s, f"SELECT id AS v FROM cio WHERE s {op} :p ORDER BY id", {"p": p}) for op, p in (("=", "A"), ("<>", "A"), ("<", "b"), ("<=", "A"), (">", "A"), (">=", "b"))}
        check("C331", "on a ci column every comparison operator compares case-insensitively on a scan", ci_ops, {"=": ["a"], "<>": ["B"], "<": ["a"], "<=": ["a"], ">": ["B"], ">=": ["B"]})
        s.sql("INSERT INTO abp SET id = 'x', f = 1"); s.sql("INSERT INTO abp SET id = 'y'")
        check("C332", "a projection turns an absent field into an explicit null (IS NOT DEFINED holds on the class only)", (col(s, "SELECT id AS v FROM (SELECT id, f FROM abp) WHERE f IS NOT DEFINED"), col(s, "SELECT id AS v FROM abp WHERE f IS NOT DEFINED")), ([], ["y"]))
        s.sql("INSERT INTO exw SET id = 'r', e1 = [{'w': 1, 'k': 'p'}, {'w': 2, 'k': 'q'}]")
        check("C333", "expand() of an embedded list answers each document under `value`", rows(s, "SELECT expand(e1) FROM exw WHERE id = 'r'"), [{"value": {"w": 1, "k": "p"}}, {"value": {"w": 2, "k": "q"}}])
        m, lst, docs = {"x": 1, "y": "z"}, [1, 2**40], [{"k": 1, "n": {"q": 2}}]
        s.batch([Sql("INSERT INTO exw SET id = :id, m = :m, l = :l, e1 = :e", {"id": "b", "m": m, "l": lst, "e": docs})])
        check("C337", "/batch keeps a map, a list and nested documents bound as parameters", rows(s, "SELECT m, l, e1 FROM exw WHERE id = 'b'"), [{"m": m, "l": lst, "e1": docs}])
        server.drop_database(DB)
        version = server.version()

    from ganda.orientdb import rules

    blocking, fixed = _sort_failures(rules.CATALOG)
    print(f"\n{len(failures)} fact(s) changed on {version}: {len(blocking)} of rules, {len(failures) - len(blocking)} of defects" if failures else f"\nall facts hold on {version}")
    index = rules.read_index() if failures else {}
    for fid in failures:
        for line in consequences(fid, index):
            print(f"  {fid} {line}")
    print(rules.BASELINE.record(version, [fid for fid in dict.fromkeys(ran) if fid not in failures], fixed, blocking))
    if not blocking:
        path = pathlib.Path(rules.__file__).with_name("RULES.md")
        path.write_text(rules.rendered())
        print(f"re-rendered {path}")
    return 1 if blocking else 0


def _sort_failures(catalog) -> "tuple[list, dict]":
    """The failing checks that block (a rule changed, or a fact the catalog does not
    hold), and the defects fixed: {defect id: the failing checks that reproduced it}."""
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
    """The rules a probe check checks and the defects it reproduces (`rules/checks.py`)."""
    from ganda.orientdb.rules import CATALOG

    return [_name(item) for item in CATALOG.checks.get(fid, ())] or ["no rule"]


def consequences(fid: str, index: dict) -> list:
    """What a failing check means: each rule it checks no longer holds, each defect it
    reproduces is gone, with the code reading them (`rules.read_index`)."""
    from ganda.orientdb.rules import CATALOG

    out = []
    for item in CATALOG.checks.get(fid, ()):
        if isinstance(item, Defect):
            what, reads = "no longer reproduces", index["defects"].get(item.id, [])
        else:
            what, reads = "no longer holds", index["rules"].get((item.question.name, item.subject), [])
        sites = ", ".join(f"{r.path.removeprefix('ganda/orientdb/')}:{r.line}" for r in reads)
        out.append(f"{_name(item)} {what}: read by {sites or 'nothing'}")
    return out or ["checks no rule"]


if __name__ == "__main__":
    sys.exit(main())
