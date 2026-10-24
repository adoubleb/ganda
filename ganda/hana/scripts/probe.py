"""Re-check live every SAP HANA fact the provider rests on: `ganda hana probe`.

Each fact is one small statement against the attached instance (HANA_HOST, ...), run in
a scratch schema and graph that are dropped afterwards. A fact is either REQUIRED (the
provider's rendering or its semantics assume it: a failure means the provider must
change: the probe names the rules of `rules/catalog.py` the fact checks) or OBSERVED (recorded so a later run can see a change; never
fails). Every line prints what the engine answered.

    python3 -m ganda hana probe            # every fact
    python3 -m ganda hana probe K5 D6      # some of them (ids or prefixes: R, D, K, G, P)
"""
from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from ganda.core.engine.provider import EngineError
from ganda.hana.concrete_syntax import hints
from ganda.hana.storage.dialect import opencypher_table, record_iri

# the custom-key styles the generator draws (`data_generator._custom_key`): the unicode
# bases (an accented letter, an astral emoji, a CJK ideograph, a dotted capital I, a
# combining sequence), punctuation, a word, the long form, an integer above 2^62
CUSTOM_KEYS = ["é1", "\U0001F6002", "中3", "İ4", "é5", "it's6", 'q"q7', "a b8", "%9", "k:110", "k" * 190 + "11", "SELECT12", "4611686018427387905", "null13"]


def _utf16_order(keys: list) -> list:
    """The order HANA SQL compares NVARCHAR in (UTF-16 code units, `semantics.hana_key`)."""
    return sorted(keys, key=lambda k: k.encode("utf-16-be", "surrogatepass"))

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


def _peer(p: Probe) -> Probe:
    """A second connection on the probe's database: another session's view of it (the
    transaction door's peer check, `connection.HanaSession.execute`)."""
    from ganda.hana.engine.connection import HanaSession, connect

    return Probe(HanaSession(connect(p.s.database), p.s.database, p.s.data_model, p.s.timeout_ms))


def _close(peer: Probe) -> None:
    try:
        peer.s.conn.close()
    except Exception:  # noqa: BLE001
        pass


def _try(fn, width: int = 90):
    """A sub-observation's answer, or the engine's refusal as text."""
    try:
        return fn()
    except EngineError as e:
        return f"ERR {str(e)[:width]}"


_PY_CMP = {">": lambda a, b: a > b, "<": lambda a, b: a < b, "=": lambda a, b: a == b, ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "<>": lambda a, b: a != b}


def _sql_double(v: float) -> str:
    """A double as a SQL approximate numeric literal: `repr` with an exponent (without one
    the text is an exact numeric literal, a DECIMAL)."""
    r = repr(v)
    return r if "e" in r or "E" in r or r in ("inf", "-inf", "nan") else r + "E0"


# --- SQL on column tables -------------------------------------------------------------------


def r_setup(p: Probe):
    p.run("CREATE COLUMN TABLE pt (id NVARCHAR(256) PRIMARY KEY, i BIGINT, d DOUBLE, s NVARCHAR(5000), b BOOLEAN, l NVARCHAR(5000))")
    rows = [("a", 1, 0.5, "a", True, "[1,2]"), ("b", 2, -1.5, "B", False, "[]"), ("c", None, None, None, None, None), ("d", 9223372036854775807, 1.7976931348623157e308, "a ", True, "[3]")]
    for r in rows:
        p.run("INSERT INTO pt (id, i, d, s, b, l) VALUES (:a, :b, :c, :d, :e, :f)", dict(zip("abcdef", r)))
    return True, "pt with 4 rows"


def r18(p: Probe):
    """Every custom-key style round-trips through an NVARCHAR(256) PRIMARY KEY, by equality
    and in ORDER BY (UTF-16 code-unit order, `hana_key`'s)."""
    p.run("CREATE COLUMN TABLE pk (id NVARCHAR(256) PRIMARY KEY, v BIGINT)")
    for n, k in enumerate(CUSTOM_KEYS):
        p.run("INSERT INTO pk (id, v) VALUES (:k, :v)", {"k": k, "v": n})
    found = [p.sql("SELECT id AS id FROM pk WHERE id = :k", {"k": k}) for k in CUSTOM_KEYS]
    hits = [r[0]["id"] == k for r, k in zip(found, CUSTOM_KEYS) if r]
    order = [r["id"] for r in p.sql("SELECT id AS id FROM pk ORDER BY id ASC")]
    emoji_len = p.one("SELECT LENGTH(id) AS v FROM pk WHERE v = 1")
    ok = len(hits) == len(CUSTOM_KEYS) and all(hits) and order == _utf16_order(CUSTOM_KEYS)
    return ok, f"{sum(hits)}/{len(CUSTOM_KEYS)} keys found by equality; ORDER BY {'is' if order == _utf16_order(CUSTOM_KEYS) else 'is NOT'} UTF-16 order; LENGTH of the emoji key {emoji_len}"


def r19(p: Probe):
    p.run("CREATE COLUMN TABLE pb1 (id NVARCHAR(256) PRIMARY KEY, b1 BIGINT, b2 NVARCHAR(5000), f1 DOUBLE)")
    p.run("CREATE COLUMN TABLE pb2 (id NVARCHAR(256) PRIMARY KEY, b1 BIGINT, b2 NVARCHAR(5000), f1 BOOLEAN)")
    p.run("CREATE COLUMN TABLE pb1__b2 (owner NVARCHAR(256) NOT NULL, pos INTEGER NOT NULL, v BIGINT)")
    p.run("CREATE COLUMN TABLE pb2__b2 (owner NVARCHAR(256) NOT NULL, pos INTEGER NOT NULL, v BIGINT)")
    p.run("CREATE VIEW PB0 AS SELECT id, b1, b2 FROM pb1 UNION ALL SELECT id, b1, b2 FROM pb2")
    p.run("CREATE VIEW PB0__b2 AS SELECT owner, pos, v FROM pb1__b2 UNION ALL SELECT owner, pos, v FROM pb2__b2")
    p.run("CREATE INDEX ix_PB0_b1_pb1 ON pb1 (b1)")
    p.run("CREATE INDEX ix_PB0_b1_pb2 ON pb2 (b1)")
    for r in [("a", 1, "[5]", 0.5), ("b", None, "[]", None), ("c", 3, None, 1.5)]:
        p.run("INSERT INTO pb1 (id, b1, b2, f1) VALUES (:a, :b, :c, :d)", dict(zip("abcd", r)))
    for r in [("x", 2, "[5,6]", True), ("y", 3, "[7]", False)]:
        p.run("INSERT INTO pb2 (id, b1, b2, f1) VALUES (:a, :b, :c, :d)", dict(zip("abcd", r)))
    for t, rows in (("pb1__b2", [("a", 0, 5)]), ("pb2__b2", [("x", 0, 5), ("x", 1, 6), ("y", 0, 7)])):
        for r in rows:
            p.run(f"INSERT INTO {t} (owner, pos, v) VALUES (:a, :b, :c)", dict(zip("abc", r)))
    filtered = sorted(r["id"] for r in p.sql("SELECT t.id AS id FROM PB0 t WHERE t.b1 > :p", {"p": 1}))
    joined = sorted((r["id"], r["v"]) for r in p.sql("SELECT t.id AS id, m.id AS v FROM PB0 t JOIN PB0 m ON m.b1 = t.b1 WHERE t.id <> m.id"))
    member = sorted(r["id"] for r in p.sql("SELECT t.id AS id FROM PB0 t WHERE EXISTS (SELECT 1 FROM PB0__b2 x WHERE x.owner = t.id AND x.v = :e)", {"e": 5}))
    ordered = [r["id"] for r in p.sql("SELECT t.id AS id FROM PB0 t ORDER BY t.b1 ASC NULLS FIRST, t.id ASC LIMIT 3 OFFSET 1")]
    n = p.run("DELETE FROM pb1 WHERE b1 > :p", {"p": 1}) + p.run("DELETE FROM pb2 WHERE b1 > :p", {"p": 1})
    left = p.one("SELECT COUNT(*) AS v FROM PB0")
    got = (filtered, joined, member, ordered, n, left)
    return _eq(got, (["c", "x", "y"], [("c", "y"), ("y", "c")], ["a", "x"], ["a", "x", "c"], 3, 2))


def r20(p: Probe):
    """AVG over BIGINT answers DECIMAL(25,6) (documented): the exact quotient rounded to six
    places; which way a half at the seventh place rounds (7/128 = 0.0546875, -7/128), and
    whether the sixth place rounds or truncates (2/3, -2/3)."""
    p.run("CREATE COLUMN TABLE pavg (id INTEGER PRIMARY KEY, a BIGINT, b BIGINT, c BIGINT, d BIGINT)")
    for k in range(128):
        p.run("INSERT INTO pavg VALUES (:k, :a, :b, :c, :d)", {"k": k, "a": 7 if k == 0 else 0, "b": -7 if k == 0 else 0, "c": (2 if k < 2 else 0) if k < 3 else None, "d": (-2 if k < 2 else 0) if k < 3 else None})
    texts = p.sql("SELECT TO_NVARCHAR(AVG(a)) AS a, TO_NVARCHAR(AVG(b)) AS b, TO_NVARCHAR(AVG(c)) AS c, TO_NVARCHAR(AVG(d)) AS d FROM pavg")[0]
    raw = p.sql("SELECT AVG(a) AS v FROM pavg")[0]["v"]
    # truncation, not rounding: 7/128 = 0.0546875 answers 0.054687 (probed 2026-10-12)
    ok = texts["c"] == "1.333333" and texts["d"] == "-1.333333" and texts["a"] == "0.054687" and texts["b"] == "-0.054687"
    return ok, f"7/128 -> {texts['a']}, -7/128 -> {texts['b']}, 4/3 over three rows: 2,2,0 -> {texts['c']}, -2,-2,0 -> {texts['d']} (the texts HANA sends); the client's value for 7/128: {raw!r} ({type(raw).__name__})"


def r21(p: Probe):
    """A pivot of the other numeric type (`pivots.cross_type`): a BIGINT column against
    `CAST(? AS DOUBLE)` and a DOUBLE column against a bound integer compare as Python
    compares an int with a float -- exactly, over the whole BIGINT range, for a double
    pivot below 10^12 (the provider's bound). Recorded: a bare `?` holding 1.5 against
    BIGINT (what the provider avoids), and a double pivot of 2^63 against 2^63 - 1 (the
    column converts to DOUBLE there, so the engine and Python part)."""
    p.run("CREATE COLUMN TABLE pct (id NVARCHAR(256) PRIMARY KEY, i BIGINT, d DOUBLE)")
    rows = {"a": (1, 0.5), "b": (2, 2.0), "c": (-3, -3.0), "d": (2**63 - 1, 1e300), "e": (-(2**63), -1e300), "f": (None, None), "g": (2**53 + 1, 0.0)}
    for k, (i, d) in rows.items():
        p.run("INSERT INTO pct (id, i, d) VALUES (:k, :i, :d)", {"k": k, "i": i, "d": d})
    ints = {k: v[0] for k, v in rows.items() if v[0] is not None}
    dbls = {k: v[1] for k, v in rows.items() if v[1] is not None}
    checks = []
    for op, pv in ((">", 1.5), ("<", -0.5), ("=", 2.0), (">=", -2.5), ("<=", 1.5), ("<>", 2.0)):
        want = sorted(k for k, v in ints.items() if _PY_CMP[op](v, pv))
        got = sorted(r["id"] for r in p.sql(f"SELECT id AS id FROM pct WHERE i {op} CAST(:p AS DOUBLE)", {"p": pv}))
        checks.append((f"i {op} {pv}", got, want))
    checks.append(("i BETWEEN 0.5 AND 2.5", sorted(r["id"] for r in p.sql("SELECT id AS id FROM pct WHERE i BETWEEN CAST(:a AS DOUBLE) AND CAST(:b AS DOUBLE)", {"a": 0.5, "b": 2.5})), ["a", "b"]))
    checks.append(("i IN (1.5, 2.0)", sorted(r["id"] for r in p.sql("SELECT id AS id FROM pct WHERE i IN (CAST(:a AS DOUBLE), CAST(:b AS DOUBLE))", {"a": 1.5, "b": 2.0})), ["b"]))
    for op, pv in ((">", 1), ("=", 2), ("<", 0), ("<>", 2)):
        want = sorted(k for k, v in dbls.items() if _PY_CMP[op](v, pv))
        got = sorted(r["id"] for r in p.sql(f"SELECT id AS id FROM pct WHERE d {op} :p", {"p": pv}))
        checks.append((f"d {op} {pv}", got, want))
    bad = [f"{label}: got {got} want {want}" for label, got, want in checks if got != want]
    bare = _try(lambda: sorted(r["id"] for r in p.sql("SELECT id AS id FROM pct WHERE i > :p", {"p": 1.5})), 70)
    edge = sorted(r["id"] for r in p.sql("SELECT id AS id FROM pct WHERE i < CAST(:p AS DOUBLE)", {"p": float(2**63)}))
    return not bad, (f"{len(checks)} comparisons exact" if not bad else " | ".join(bad)) + f"; a bare ? = 1.5 against BIGINT: {bare}; i < 2^63 as DOUBLE: {edge} (Python keeps every row; HANA converts 2^63 - 1 to DOUBLE)"


def r22(p: Probe):
    """Constants written into a SQL text (the `inline` position) mean what the bound
    parameter means: a DOUBLE as an approximate numeric literal (`repr` with an exponent)
    over the boundary pool, BIGINT's two ends, `N'...'` strings (a quote, an astral code
    point, 5000 characters), TRUE / FALSE. Recorded: a DOUBLE constant without an exponent
    (an exact numeric literal, a DECIMAL) against the stored double."""
    p.run("CREATE COLUMN TABLE plit (id NVARCHAR(256) PRIMARY KEY, i BIGINT, d DOUBLE, s NVARCHAR(5000), b BOOLEAN)")
    doubles = [0.1, 0.30000000000000004, 1e-300, 1.7976931348623157e308, float(2**53) + 2, 2.5e-308, 123456789.123456789, 1.0]
    strings = ["it's", "\U0001F600x", "x" * 4999 + "y", "a\\b"]
    ints = [2**63 - 1, -(2**63), 0]
    for k, v in enumerate(doubles):
        p.run("INSERT INTO plit (id, d) VALUES (:k, :v)", {"k": f"d{k}", "v": v})
    for k, v in enumerate(strings):
        p.run("INSERT INTO plit (id, s) VALUES (:k, :v)", {"k": f"s{k}", "v": v})
    for k, v in enumerate(ints):
        p.run("INSERT INTO plit (id, i) VALUES (:k, :v)", {"k": f"i{k}", "v": v})
    p.run("INSERT INTO plit (id, b) VALUES ('bt', TRUE)")
    p.run("INSERT INTO plit (id, b) VALUES ('bf', FALSE)")
    bad = []
    plain = []
    for k, v in enumerate(doubles):
        got = sorted(r["id"] for r in p.sql(f"SELECT id AS id FROM plit WHERE d = {_sql_double(v)}"))
        if got != [f"d{k}"]:
            bad.append(f"d = {_sql_double(v)}: {got}")
        plain.append(f"{v!r}: {_try(lambda: sorted(r['id'] for r in p.sql(f'SELECT id AS id FROM plit WHERE d = {v!r}')), 50)}")
    for k, v in enumerate(strings):
        lit = "N'" + v.replace("'", "''") + "'"
        got = sorted(r["id"] for r in p.sql(f"SELECT id AS id FROM plit WHERE s = {lit}"))
        if got != [f"s{k}"]:
            bad.append(f"s = {lit[:30]}...: {got}")
    for k, v in enumerate(ints):
        got = sorted(r["id"] for r in p.sql(f"SELECT id AS id FROM plit WHERE i = {v}"))
        if got != [f"i{k}"]:
            bad.append(f"i = {v}: {got}")
    for lit, want in (("TRUE", ["bt"]), ("FALSE", ["bf"])):
        got = sorted(r["id"] for r in p.sql(f"SELECT id AS id FROM plit WHERE b = {lit}"))
        if got != want:
            bad.append(f"b = {lit}: {got}")
    ops = sorted(r["id"] for r in p.sql(f"SELECT id AS id FROM plit WHERE d > 0.2E0 AND d < {_sql_double(1e300)} AND d IN (0.30000000000000004E0, 1.0E0, 2.0E0)"))
    if ops != ["d1", "d7"]:
        bad.append(f"ranges and IN over double literals: {ops}")
    return not bad, (f"{len(doubles)} doubles, {len(strings)} strings, {len(ints)} integers, TRUE / FALSE, ranges: constants mean the bound value" if not bad else " | ".join(bad)) + "; without an exponent: " + ", ".join(plain)


def r23(p: Probe):
    """The set-operation form (`tlp`): the records a filter keeps are every record EXCEPT
    those where it is false EXCEPT those where it is unknown (`(CASE WHEN p THEN 1 WHEN
    NOT (p) THEN 0 END) IS NULL`): a comparison, a disjunction with LIKE, IN, a NOT with an
    EXISTS over a child table, IS NULL, `<>`; the same counted."""
    p.run("CREATE COLUMN TABLE ptl (id NVARCHAR(256) PRIMARY KEY, i BIGINT, s NVARCHAR(5000))")
    p.run("CREATE COLUMN TABLE ptl__l (owner NVARCHAR(256) NOT NULL, pos INTEGER NOT NULL, v BIGINT)")
    for r in [("a", 1, "ab"), ("b", 2, None), ("c", None, "zz"), ("d", 2, "ac"), ("e", None, None)]:
        p.run("INSERT INTO ptl (id, i, s) VALUES (:a, :b, :c)", dict(zip("abc", r)))
    for r in [("a", 0, 5), ("d", 0, 7), ("d", 1, 5)]:
        p.run("INSERT INTO ptl__l (owner, pos, v) VALUES (:a, :b, :c)", dict(zip("abc", r)))
    preds = ["t.i > :p", "t.i > :p OR t.s LIKE 'a%'", "t.i IN (:p, :q)", "NOT (t.i > :p) AND EXISTS (SELECT 1 FROM ptl__l x WHERE x.owner = t.id AND x.v = :e)", "t.s IS NULL", "t.i <> :p", "t.i NOT IN (:p, :q)"]
    binds = {"p": 1, "q": 2, "e": 5}
    bad = []
    for pred in preds:
        direct = sorted(r["id"] for r in p.sql(f"SELECT t.id AS id FROM ptl t WHERE {pred}", binds))
        text = f"SELECT t.id AS id FROM ptl t EXCEPT SELECT t.id AS id FROM ptl t WHERE NOT ({pred}) EXCEPT SELECT t.id AS id FROM ptl t WHERE (CASE WHEN {pred} THEN 1 WHEN NOT ({pred}) THEN 0 END) IS NULL"
        try:
            tlp = sorted(r["id"] for r in p.sql(text, binds))
            count = p.one(f"SELECT COUNT(*) AS v FROM ({text}) z", binds)
        except EngineError as e:
            bad.append(f"{pred}: {str(e)[:80]}")
            continue
        if tlp != direct or count != len(direct):
            bad.append(f"{pred}: direct {direct}, set form {tlp}, count {count}")
    return not bad, (f"{len(preds)} filters agree with their set form and its count" if not bad else " | ".join(bad))


def r24(p: Probe):
    """Another connection sees none of this one's uncommitted writes (the transaction
    door's peer check): while the transaction is open the peer's count and value are the
    committed ones, this connection's are its own; the rollback leaves the committed ones."""
    p.run("CREATE COLUMN TABLE piso (id NVARCHAR(256) PRIMARY KEY, i BIGINT)")
    p.run("INSERT INTO piso (id, i) VALUES ('a', 1)")
    p.run("INSERT INTO piso (id, i) VALUES ('b', 2)")
    peer = _peer(p)
    seen = {}
    try:

        def inside():
            p.run("INSERT INTO piso (id, i) VALUES ('z', 9)")
            p.run("UPDATE piso SET i = 99 WHERE id = 'a'")
            p.run("DELETE FROM piso WHERE id = 'b'")
            seen["own"] = (p.one("SELECT COUNT(*) AS v FROM piso"), p.one("SELECT i AS v FROM piso WHERE id = 'a'"))
            seen["peer"] = (peer.one("SELECT COUNT(*) AS v FROM piso"), peer.one("SELECT i AS v FROM piso WHERE id = 'a'"))

        p.s._transaction(inside, commit=False)
        after = (peer.one("SELECT COUNT(*) AS v FROM piso"), p.one("SELECT i AS v FROM piso WHERE id = 'a'"))
    finally:
        _close(peer)
    return _eq((seen.get("own"), seen.get("peer"), after), ((2, 99), (2, 1), (2, 1)))


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
    Fact("R9", "PERCENTILE_CONT(0.5) is the interpolating median (of 1 and 2: 1.5); HANA's own MEDIAN does not interpolate an even count of integers, so the renderings use PERCENTILE_CONT", lambda p: _eq(p.one("SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY i) AS v FROM pt WHERE id IN ('a', 'b')"), 1.5)),
    Fact("R10", "LIKE is case-sensitive and `_` is one character", lambda p: _eq([r["id"] for r in p.sql("SELECT id FROM pt WHERE s LIKE 'a%' ORDER BY id")], ["a", "d"])),
    Fact("R11", "two typed parameters compare in DUMMY's FROM (`CAST(? AS NVARCHAR(256)) = CAST(? AS ...)`)", lambda p: _eq(p.one("SELECT 1 AS v FROM DUMMY WHERE CAST(:a AS NVARCHAR(256)) = CAST(:b AS NVARCHAR(256))", {"a": "x", "b": "x"}), 1)),
    Fact("R12", "UPSERT ... VALUES ... WHERE id = ? updates an existing row and inserts a new one", lambda p: (p.run("UPSERT pt (id, i) VALUES (:k, 5) WHERE id = :k", {"k": "a"}), p.run("UPSERT pt (id, i) VALUES (:k, 6) WHERE id = :k", {"k": "e"}), _eq([(r["id"], r["i"]) for r in p.sql("SELECT id, i FROM pt WHERE id IN ('a', 'e') ORDER BY id")], [("a", 5), ("e", 6)]))[-1]),
    Fact("R13", "MERGE INTO t USING (SELECT ...) s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.i = ...", lambda p: (p.run("MERGE INTO pt USING (SELECT id FROM pt WHERE id = 'e') s ON pt.id = s.id WHEN MATCHED THEN UPDATE SET pt.i = 7"), _eq(p.one("SELECT i AS v FROM pt WHERE id = 'e'"), 7))[-1]),
    Fact("R14", "MERGE DELTA OF, a composite index, a unique index, a hash-partitioned table", lambda p: (p.run("MERGE DELTA OF pt"), p.run("CREATE INDEX ix_pt_is ON pt (i, s)"), p.run("CREATE UNIQUE INDEX ix_pt_s ON pt (d)"), p.run("CREATE COLUMN TABLE pp (id NVARCHAR(256) NOT NULL, v BIGINT) PARTITION BY HASH (id) PARTITIONS 3"), (True, "accepted"))[-1]),
    Fact("R15", "an UPDATE answers its row count", lambda p: _eq(p.run("UPDATE pt SET i = i + 1 WHERE i IS NOT NULL AND i < 100"), 3)),
    Fact("R16", "a derived table, a correlated scalar subquery, UNION ALL in FROM, EXISTS", lambda p: _eq(p.one("SELECT COUNT(*) AS v FROM (SELECT x.id FROM (SELECT id FROM pt UNION ALL SELECT id FROM pt) x WHERE EXISTS (SELECT 1 FROM pt y WHERE y.id = x.id AND (SELECT COUNT(*) FROM pt z WHERE z.id = y.id) = 1)) w"), 10)),
    Fact("R17", "a unique violation is error 301 (a rejection, not an internal error)", lambda p: (lambda e: (" 301]" in e or "[301]" in e, e[:120]))(p.error(lambda: p.run("INSERT INTO pt (id) VALUES ('a')")))),    Fact("R18", "custom keys: an astral emoji, a combining sequence, quotes, spaces, `%`, a 193-character key and a 19-digit one round-trip through an NVARCHAR(256) key, by equality and in UTF-16 order (the `custom_key_probability` axis)", r18),
    Fact("R19", "the base class as a UNION ALL view over the subclass tables (the inheritance axis): filtered, joined to itself, a LIST base field's child-table union inside EXISTS, an index per subclass table; a DELETE through the tables is what the view then shows", r19),
    Fact("R20", "AVG over BIGINT answers DECIMAL(25,6): the exact quotient TRUNCATED to six places (7/128 is 0.054687; the offline stand-in's `hana_avg`)", r20),
    Fact("R21", "a cross-type numeric pivot: a BIGINT column against CAST(? AS DOUBLE) and a DOUBLE column against a bound integer compare exactly, as Python does, for a double pivot below 10^12 (`pivots.cross_type`); a bare ? and a pivot of 2^63 recorded", r21),
    Fact("R22", "constants in a SQL text mean the bound value: DOUBLE as an approximate literal (with an exponent) over the boundary pool, BIGINT's ends, N'...' strings (a quote, an astral code point, 5000 characters), TRUE / FALSE (the `inline` position); without an exponent recorded", r22),
    Fact("R23", "the set-operation form: a filter's records are every record EXCEPT the false ones EXCEPT the unknown ones (CASE over the predicate IS NULL), for comparisons, LIKE under OR, IN, NOT IN, NOT with EXISTS, IS NULL; counted too (the `tlp` form)", r23),
    Fact("R24", "another connection sees none of this one's uncommitted INSERT / UPDATE / DELETE while the transaction is open, and the committed state after the rollback (the transaction door's peer check)", r24),
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


def _pcn(p: Probe) -> None:
    """A collection of its own for the NOT facts (the `pc` documents change along the D facts)."""
    p.run("CREATE COLLECTION pcn")
    for d in [{"id": "a", "i": 1, "l": [5], "s": "ab"}, {"id": "b", "i": 2, "l": [1], "s": "zz"}, {"id": "c"}, {"id": "d", "i": None}]:
        p.run("INSERT INTO pcn VALUES(:doc)", {"doc": json.dumps(d)})


def d19(p: Probe):
    _pcn(p)

    def ids(where: str, binds=None) -> list:
        return sorted(r["id"] for r in p.sql(f'SELECT $."id" AS id FROM pcn WHERE {where}', binds))

    got = (ids('NOT ($."i" = CAST(:v AS BIGINT))', {"v": 1}), ids('NOT ($."i" = CAST(:v AS BIGINT) OR $."i" IS NULL)', {"v": 1}), ids('NOT (NOT ($."i" > CAST(:v AS BIGINT)))', {"v": 1}), ids('NOT ($."s" LIKE \'a%\')'))
    return _eq(got, (["b"], ["b"], ["b"], ["b"]))


def d20(p: Probe):
    def ids(where: str, binds=None) -> list:
        return sorted(r["id"] for r in p.sql(f'SELECT $."id" AS id FROM pcn WHERE {where}', binds))

    got = (ids('NOT (FOR ANY "e" IN $."l" SATISFIES "e" = CAST(:v AS BIGINT) END)', {"v": 5}), ids('NOT ((FOR ANY "e" IN $."l" SATISFIES "e" = CAST(:v AS BIGINT) END) OR $."i" = CAST(:w AS BIGINT))', {"v": 5, "w": 2}))
    return _eq(got, (["b", "c", "d"], []))


def d22(p: Probe):
    p.run("CREATE COLLECTION pck")
    for n, k in enumerate(CUSTOM_KEYS):
        p.run("INSERT INTO pck VALUES(:doc)", {"doc": json.dumps({"id": k, "v": n}, ensure_ascii=False)})
    found = [p.sql('SELECT $."id" AS id FROM pck WHERE $."id" = CAST(:k AS NVARCHAR(256))', {"k": k}) for k in CUSTOM_KEYS]
    hits = [r[0]["id"] == k for r, k in zip(found, CUSTOM_KEYS) if r]
    order = [r["id"] for r in p.sql('SELECT $."id" AS id FROM pck ORDER BY $."id" ASC')]
    ok = len(hits) == len(CUSTOM_KEYS) and all(hits)
    return ok, f"{sum(hits)}/{len(CUSTOM_KEYS)} keys found by equality; ORDER BY $.\"id\" {'is' if order == _utf16_order(CUSTOM_KEYS) else 'is NOT'} UTF-16 order"


def d23(p: Probe):
    p.run("CREATE COLLECTION pd1")
    p.run("CREATE COLLECTION pd2")
    for d in [{"id": "a", "b1": 1, "f1": "x"}, {"id": "b"}, {"id": "c", "b1": 3}]:
        p.run("INSERT INTO pd1 VALUES(:doc)", {"doc": json.dumps(d)})
    for d in [{"id": "x", "b1": 2, "f1": True}, {"id": "y", "b1": 3}]:
        p.run("INSERT INTO pd2 VALUES(:doc)", {"doc": json.dumps(d)})
    views = 'WITH raw_v_pd1 AS (SELECT $."id" AS id, $."b1" AS b1 FROM pd1), v_pd1 AS (SELECT id, TO_BIGINT(b1) AS b1 FROM raw_v_pd1), raw_v_pd2 AS (SELECT $."id" AS id, $."b1" AS b1 FROM pd2), v_pd2 AS (SELECT id, TO_BIGINT(b1) AS b1 FROM raw_v_pd2), v_PD0 AS (SELECT id, b1 FROM v_pd1 UNION ALL SELECT id, b1 FROM v_pd2)'
    ids = sorted(r["id"] for r in p.sql(f"{views} SELECT t.id AS id FROM v_PD0 t WHERE t.b1 > :p", {"p": 1}))
    grouped = sorted(((r["k"], r["v"]) for r in p.sql(f"{views} SELECT t.b1 AS k, COUNT(*) AS v FROM v_PD0 t GROUP BY t.b1")), key=repr)
    union = p.error(lambda: p.sql('SELECT $."id" AS id FROM pd1 WHERE $."b1" > CAST(:p AS BIGINT) UNION ALL SELECT $."id" AS id FROM pd2 WHERE $."b1" > CAST(:p AS BIGINT)', {"p": 1}))[:70] or "accepted"
    ok = ids == ["c", "x", "y"] and grouped == sorted([(None, 1), (1, 1), (2, 1), (3, 2)], key=repr)
    return ok, f"filtered {ids}, grouped {grouped}; a UNION ALL of collection queries: {union}"


def _pcx(p: Probe) -> None:
    """A collection of its own for the cross-type, set-operation and hint facts (created
    once; the D facts may run in any subset)."""
    try:
        p.run("CREATE COLLECTION pcx")
    except EngineError:
        return
    for d in [{"id": "a", "i": 1, "f": 0.5, "s": "ab"}, {"id": "b", "i": 2, "f": 2.0}, {"id": "c", "i": -3, "f": -3.0, "s": "zz"}, {"id": "d", "i": 9007199254740993, "f": 1e300}, {"id": "e"}, {"id": "f", "i": None, "s": "ac"}]:
        p.run("INSERT INTO pcx VALUES(:doc)", {"doc": json.dumps(d)})


_PCX_INTS = {"a": 1, "b": 2, "c": -3, "d": 9007199254740993}
_PCX_DBLS = {"a": 0.5, "b": 2.0, "c": -3.0, "d": 1e300}


def d24(p: Probe):
    """A cross-type pivot in a collection filter: an integer field against `CAST(? AS
    DOUBLE)` and a fractional field against `CAST(? AS BIGINT)` compare as Python does
    (the store compares integer and number as one type, D4) for a double pivot below
    10^12; recorded: `CAST(? AS DECIMAL)` for the same pivots, and a double pivot of 2^53
    against 2^53 + 1."""
    _pcx(p)
    checks = []
    for op, pv in ((">", 1.5), ("<", -0.5), ("=", 2.0), (">=", -2.5), ("<=", 1.5), ("<>", 2.0)):
        want = sorted(k for k, v in _PCX_INTS.items() if _PY_CMP[op](v, pv))
        got = sorted(r["id"] for r in p.sql(f'SELECT $."id" AS id FROM pcx WHERE $."i" {op} CAST(:p AS DOUBLE)', {"p": pv}))
        checks.append((f"i {op} {pv}", got, want))
    checks.append(("i BETWEEN 0.5 AND 2.5", sorted(r["id"] for r in p.sql('SELECT $."id" AS id FROM pcx WHERE $."i" BETWEEN CAST(:a AS DOUBLE) AND CAST(:b AS DOUBLE)', {"a": 0.5, "b": 2.5})), ["a", "b"]))
    checks.append(("i = 1.5 OR i = 2.0", sorted(r["id"] for r in p.sql('SELECT $."id" AS id FROM pcx WHERE $."i" = CAST(:a AS DOUBLE) OR $."i" = CAST(:b AS DOUBLE)', {"a": 1.5, "b": 2.0})), ["b"]))
    for op, pv in ((">", 1), ("=", 2), ("<", 0), ("<>", 2)):
        want = sorted(k for k, v in _PCX_DBLS.items() if _PY_CMP[op](v, pv))
        got = sorted(r["id"] for r in p.sql(f'SELECT $."id" AS id FROM pcx WHERE $."f" {op} CAST(:p AS BIGINT)', {"p": pv}))
        checks.append((f"f {op} {pv}", got, want))
    bad = [f"{label}: got {got} want {want}" for label, got, want in checks if got != want]
    dec = _try(lambda: sorted(r["id"] for r in p.sql('SELECT $."id" AS id FROM pcx WHERE $."i" > CAST(:p AS DECIMAL)', {"p": Decimal("1.5")})), 60)
    edge = _try(lambda: sorted(r["id"] for r in p.sql('SELECT $."id" AS id FROM pcx WHERE $."i" < CAST(:p AS DOUBLE)', {"p": float(2**53)})), 60)
    return not bad, (f"{len(checks)} comparisons exact" if not bad else " | ".join(bad)) + f"; i > CAST(1.5 AS DECIMAL): {dec}; i < 2^53 as DOUBLE: {edge} (Python: a, b, c)"


def d25(p: Probe):
    """The set-operation form over the collection views (the only doc path with set
    operations): EXCEPT the false EXCEPT the unknown equals the direct filter and its
    count; EXCEPT of two collection queries themselves (recorded)."""
    _pcx(p)
    views = 'WITH raw_v AS (SELECT $."id" AS id, $."i" AS i, $."s" AS s FROM pcx), v AS (SELECT id, TO_BIGINT(i) AS i, TO_NVARCHAR(s) AS s FROM raw_v) '
    preds = ["t.i > :p", "t.i > :p OR t.s LIKE 'a%'", "t.i IN (:p, :q)", "t.i IS NULL", "t.i <> :p AND t.s IS NOT NULL"]
    binds = {"p": 1, "q": 2}
    bad = []
    for pred in preds:
        direct = sorted(r["id"] for r in p.sql(f"{views}SELECT t.id AS id FROM v t WHERE {pred}", binds))
        body = f"SELECT t.id AS id FROM v t EXCEPT SELECT t.id AS id FROM v t WHERE NOT ({pred}) EXCEPT SELECT t.id AS id FROM v t WHERE (CASE WHEN {pred} THEN 1 WHEN NOT ({pred}) THEN 0 END) IS NULL"
        try:
            tlp = sorted(r["id"] for r in p.sql(views + body, binds))
            count = p.one(f"{views}SELECT COUNT(*) AS v FROM ({body}) z", binds)
        except EngineError as e:
            bad.append(f"{pred}: {str(e)[:80]}")
            continue
        if tlp != direct or count != len(direct):
            bad.append(f"{pred}: direct {direct}, set form {tlp}, count {count}")
    raw = _try(lambda: sorted(r["id"] for r in p.sql('SELECT $."id" AS id FROM pcx EXCEPT SELECT $."id" AS id FROM pcx WHERE $."i" > CAST(:p AS BIGINT)', {"p": 1})), 70)
    return not bad, (f"{len(preds)} filters agree with their set form over the views" if not bad else " | ".join(bad)) + f"; EXCEPT of two collection queries: {raw}"


def d26(p: Probe):
    """Another connection sees none of this one's uncommitted collection writes (the
    transaction door's peer check on doc)."""
    p.run("CREATE COLLECTION piso")
    p.run("INSERT INTO piso VALUES(:doc)", {"doc": '{"id": "a", "i": 1}'})
    p.run("INSERT INTO piso VALUES(:doc)", {"doc": '{"id": "b", "i": 2}'})
    count = "SELECT COUNT(*) AS v FROM piso"
    value = 'SELECT TO_BIGINT($."i") AS v FROM piso WHERE $."id" = CAST(:k AS NVARCHAR(256))'
    peer = _peer(p)
    seen = {}
    try:

        def inside():
            p.run("INSERT INTO piso VALUES(:doc)", {"doc": '{"id": "z", "i": 9}'})
            p.run('UPDATE piso SET $."i" = CAST(:v AS BIGINT) WHERE $."id" = CAST(:k AS NVARCHAR(256))', {"v": 99, "k": "a"})
            p.run('DELETE FROM piso WHERE $."id" = CAST(:k AS NVARCHAR(256))', {"k": "b"})
            seen["own"] = (p.one(count), p.one(value, {"k": "a"}))
            seen["peer"] = (peer.one(count), peer.one(value, {"k": "a"}))

        p.s._transaction(inside, commit=False)
        after = (peer.one(count), p.one(value, {"k": "a"}))
    finally:
        _close(peer)
    return _eq((seen.get("own"), seen.get("peer"), after), ((2, 99), (2, 1), (2, 1)))


def d27(p: Probe):
    """The collection query under every hint profile answers its plain rows (a filter, an
    ordering with LIMIT, a count): the hinted `doc:<profile>` variant."""
    _pcx(p)
    shapes = [
        ('SELECT $."id" AS id FROM pcx WHERE $."i" > CAST(:p AS BIGINT) OR $."s" LIKE \'a%\'', {"p": 1}),
        ('SELECT $."id" AS id FROM pcx ORDER BY $."i" ASC, $."id" ASC LIMIT 3', {}),
        ('SELECT COUNT(*) AS v FROM pcx WHERE $."i" IS NULL', {}),
        ('SELECT $."s" AS k, COUNT(*) AS v FROM pcx WHERE $."i" >= CAST(:p AS BIGINT) GROUP BY $."s"', {"p": -3}),
    ]
    bad = []
    for text, binds in shapes:
        plain = _rows(p, text, binds)
        for profile in hints.NAMES:
            try:
                steered = _rows(p, hints.hinted(text, profile), binds)
            except EngineError as e:
                bad.append(f"{profile} on {text[:30]}...: {str(e)[:70]}")
                continue
            if steered != plain:
                bad.append(f"{profile} on {text[:30]}...: {steered} vs {plain}")
    return not bad, (f"{len(shapes)} collection queries answer alike under {len(hints.NAMES)} profiles" if not bad else " | ".join(bad[:6]))


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
    Fact("D19", "NOT kept as written in a collection filter is three-valued as SQL: NOT (i = 1) drops an absent and a null i; NOT (i = 1 OR i IS NULL), NOT (NOT (i > 1)), NOT (s LIKE 'a%') likewise (the `neg` position)", d19),
    Fact("D20", "NOT over FOR ANY and over a disjunction holding one: NOT (FOR ANY ...) holds for an absent array (FOR ANY is false there, D8); NOT (FOR ANY ... OR i = 2) drops a document where the OR is unknown", d20),
    Fact("D22", "custom keys in a collection: every style found by $.\"id\" = CAST(? AS NVARCHAR(256)); ORDER BY $.\"id\" against UTF-16 order (recorded)", d22),
    Fact("D23", "the base class over collections: the views of two collections UNION ALLed in a CTE, filtered and counted in SQL (how the provider reads the base class on doc); a UNION ALL of two collection queries themselves (recorded)", d23),
    Fact("D24", "a cross-type pivot in a collection filter: an integer field against CAST(? AS DOUBLE), a fractional field against CAST(? AS BIGINT), exactly as Python for a double pivot below 10^12 (`pivots.cross_type`); CAST(? AS DECIMAL) and a pivot of 2^53 recorded", d24),
    Fact("D25", "the set-operation form over the collection views: EXCEPT the false EXCEPT the unknown equals the direct filter and its count (the `tlp` form on doc); EXCEPT of two collection queries recorded", d25),
    Fact("D26", "another connection sees none of this one's uncommitted collection INSERT / UPDATE / DELETE, and the committed documents after the rollback (the transaction door's peer check)", d26),
    Fact("D27", "the collection query under every hint profile answers its plain rows: a filter with LIKE, an ordering with LIMIT, a count, a GROUP BY (the hinted `doc:<profile>` variant)", d27),
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


def k21(p: Probe):
    from ganda.hana.storage.dialect import sparql_string

    triples = " . ".join(f"{record_iri(k)} a c:pk ; p:id {sparql_string(k)}" for k in CUSTOM_KEYS)
    p.update(f"INSERT DATA {{ GRAPH {G} {{ {triples} }} }}")
    found = [p.sparql(f"SELECT ?id WHERE {{ GRAPH {G} {{ {record_iri(k)} p:id ?id }} }}") for k in CUSTOM_KEYS]
    hits = [r[0]["id"] == k for r, k in zip(found, CUSTOM_KEYS) if r]
    order = [r["id"] for r in p.sparql(f"SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:pk ; p:id ?id }} }} ORDER BY ASC(?id)")]
    ok = len(hits) == len(CUSTOM_KEYS) and all(hits) and order == _utf16_order(CUSTOM_KEYS)
    return ok, f"{sum(hits)}/{len(CUSTOM_KEYS)} keys found by IRI; ORDER BY ?id {'is' if order == _utf16_order(CUSTOM_KEYS) else 'is NOT'} the UTF-16 order"


def k22(p: Probe):
    p.update(f'INSERT DATA {{ GRAPH {G} {{ <urn:ganda:r:k22a> a c:k22n1 , c:K22 ; p:id "k22a" ; p:b1 "1"^^xsd:long . <urn:ganda:r:k22b> a c:k22n2 , c:K22 ; p:id "k22b" ; p:b1 "2"^^xsd:long . <urn:ganda:r:k22c> a c:k22n2 , c:K22 ; p:id "k22c" . }} }}')
    base = p.ids(f"SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:K22 ; p:id ?id . OPTIONAL {{ ?n p:b1 ?b }} FILTER (!BOUND(?b) || ?b > 1) }} }}")
    sub = p.ids(f"SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:k22n2 ; p:id ?id }} }}")
    count = p.sparql(f"SELECT (COUNT(*) AS ?v) WHERE {{ GRAPH {G} {{ ?n a c:k22n1 }} }}")[0]["v"]
    joined = sorted((r["id"], r["v"]) for r in p.sparql(f"SELECT ?id ?v WHERE {{ GRAPH {G} {{ ?n a c:K22 ; p:id ?id ; p:b1 ?f . ?m a c:K22 ; p:b1 ?mf ; p:id ?v . FILTER (?mf > ?f) }} }}"))
    p.update(f"DELETE WHERE {{ GRAPH {G} {{ <urn:ganda:r:k22a> ?p ?o }} }}")
    gone = p.sparql(f"SELECT (COUNT(*) AS ?v) WHERE {{ GRAPH {G} {{ <urn:ganda:r:k22a> ?p ?o }} }}")[0]["v"]
    return _eq((base, sub, count, joined, gone), (["k22b", "k22c"], ["k22b", "k22c"], 1, [("k22a", "k22b")], 0))


def k23(p: Probe):
    """How the triple store averages longs: AVG itself (the campaign of 2026-10-12 saw a
    sum above 2^53 lose its last bit: computed in double), SUM / COUNT (xsd:decimal
    division per SPARQL 1.1), and a rounding to six places with a half away from zero."""
    rows = " . ".join(f'<urn:ganda:r:k23{k}> a c:k23 ; p:k23 "{v}"^^xsd:long' for k, v in enumerate([-5479] + [0] * 28))
    big = " . ".join(f'<urn:ganda:r:k23b{k}> a c:k23b ; p:k23 "{v}"^^xsd:long' for k, v in enumerate([6004799503160473, 0]))
    neg = " . ".join(f'<urn:ganda:r:k23c{k}> a c:k23c ; p:k23 "{v}"^^xsd:long' for k, v in enumerate([-7] + [0] * 127))
    p.update(f"INSERT DATA {{ GRAPH {G} {{ {rows} . {big} . {neg} }} }}")

    def one(cls: str, expr: str):
        try:
            return p.sparql(f"SELECT (STR({expr}) AS ?v) WHERE {{ GRAPH {G} {{ ?n a c:{cls} ; p:k23 ?w }} }}")[0]["v"]
        except EngineError as e:
            return f"ERR {str(e)[:70]}"

    avg29, div29 = one("k23", "AVG(?w)"), one("k23", "SUM(?w) / COUNT(?w)")
    avg_big, div_big = one("k23b", "AVG(?w)"), one("k23b", "SUM(?w) / COUNT(?w)")
    rounded = one("k23", "ROUND((SUM(?w) / COUNT(?w)) * 1000000) / 1000000")
    half = one("k23c", "ROUND((SUM(?w) / COUNT(?w)) * 1000000) / 1000000")
    away = one("k23c", "IF(SUM(?w) >= 0, FLOOR((SUM(?w) / COUNT(?w)) * 1000000 + 0.5), CEIL((SUM(?w) / COUNT(?w)) * 1000000 - 0.5)) / 1000000")
    # recorded: AVG and `/` over longs print a double's digits (B13); the provider compares
    # an rdf average only where double arithmetic and DECIMAL(25,6) agree (`known_bugs`)
    return True, f"AVG -5479/29 = {avg29}; SUM/COUNT = {div29}; AVG of 6004799503160473 over 2 = {avg_big}; SUM/COUNT = {div_big}; ROUND to 6 = {rounded}; -7/128 ROUND = {half}; half away from zero = {away}"


def _k_class(p: Probe, cls: str, nodes: dict) -> None:
    """Nodes of a class of their own (`cls`): id -> {predicate local name: typed literal text}."""
    triples = " . ".join(f'<urn:ganda:r:{cls}{k}> a c:{cls} ; p:id "{k}"' + "".join(f" ; p:{pred} {lit}" for pred, lit in props.items()) for k, props in nodes.items())
    p.update(f"INSERT DATA {{ GRAPH {G} {{ {triples} }} }}")


def _k_ids(p: Probe, cls: str, pattern: str, filt: str) -> list:
    return p.ids(f"SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:{cls} ; p:id ?id . {pattern} FILTER ({filt}) }} }}")


def k24(p: Probe):
    """A cross-type pivot in SPARQL: an xsd:long value against an xsd:double literal and
    an xsd:double value against an integer literal compare numerically, as Python does,
    for a double pivot below 10^12; recorded: a double pivot of 2^63 against 2^63 - 1."""
    ints = {"a": 1, "b": 2, "c": -3, "d": 2**63 - 1, "g": 2**53 + 1}
    dbls = {"a": 0.5, "b": 2.0, "d": 1e300}
    nodes = {k: {} for k in "abcdeg"}
    for k, v in ints.items():
        nodes[k]["i"] = f'"{v}"^^xsd:long'
    for k, v in dbls.items():
        nodes[k]["f"] = f'"{v!r}"^^xsd:double'
    _k_class(p, "k24", nodes)
    checks = []
    for op, pv in ((">", 1.5), ("<", -0.5), ("=", 2.0), (">=", -2.5), ("<=", 1.5), ("!=", 2.0)):
        want = sorted(k for k, v in ints.items() if _PY_CMP["<>" if op == "!=" else op](v, pv))
        got = _k_ids(p, "k24", "?n p:i ?i .", f'?i {op} "{pv!r}"^^xsd:double')
        checks.append((f"i {op} {pv}", got, want))
    checks.append(("i IN (1.5, 2.0)", _k_ids(p, "k24", "?n p:i ?i .", '?i IN ("1.5"^^xsd:double, "2.0"^^xsd:double)'), ["b"]))
    checks.append(("i between 0.5 and 2.5", _k_ids(p, "k24", "?n p:i ?i .", '?i >= "0.5"^^xsd:double && ?i <= "2.5"^^xsd:double'), ["a", "b"]))
    for op, pv in ((">", 1), ("=", 2), ("<", 0), ("!=", 2)):
        want = sorted(k for k, v in dbls.items() if _PY_CMP["<>" if op == "!=" else op](v, pv))
        got = _k_ids(p, "k24", "?n p:f ?f .", f"?f {op} {pv}")
        checks.append((f"f {op} {pv}", got, want))
    bad = [f"{label}: got {got} want {want}" for label, got, want in checks if got != want]
    edge = _try(lambda: _k_ids(p, "k24", "?n p:i ?i .", f'?i < "{float(2**63)!r}"^^xsd:double'), 60)
    return not bad, (f"{len(checks)} comparisons exact" if not bad else " | ".join(bad)) + f"; i < 2^63 as xsd:double: {edge} (Python keeps every value)"


def k25(p: Probe):
    """Whether the triple store can average longs the way SQL does (B13, `known_bugs`):
    the sum cast to xsd:decimal before the division, and a truncation to six places in
    decimal arithmetic, on the campaign's groups -- recorded; the outcome decides whether
    an rdf `avg` is compared beyond the cases where double arithmetic agrees."""
    groups = {"k25a": [-5479] + [0] * 28, "k25b": [7] + [0] * 127, "k25c": [608, 608, 9007199254740993], "k25d": [1, 2]}
    want = {"k25a": "-188.931034", "k25b": "0.054687", "k25c": "3002399751580736.333333", "k25d": "1.5"}
    for cls, vs in groups.items():
        _k_class(p, cls, {str(k): {"w": f'"{v}"^^xsd:long'} for k, v in enumerate(vs)})

    def one(cls: str, expr: str):
        return _try(lambda: p.sparql(f"SELECT (STR({expr}) AS ?v) WHERE {{ GRAPH {G} {{ ?n a c:{cls} ; p:w ?w }} }}")[0]["v"], 60)

    scaled = "(xsd:decimal(SUM(?w)) * 1000000 / COUNT(?w))"
    forms = {
        "decimal(SUM) / COUNT": "xsd:decimal(SUM(?w)) / COUNT(?w)",
        "SUM(decimal) / COUNT": "SUM(xsd:decimal(?w)) / COUNT(?w)",
        "truncated to 6": f"IF(SUM(?w) >= 0, FLOOR({scaled}), -FLOOR(-{scaled})) / 1000000",
        "truncated, decimal divisor": f"IF(SUM(?w) >= 0, FLOOR({scaled}), -FLOOR(-{scaled})) / xsd:decimal(1000000)",
    }
    out = []
    agree = []
    for cls in groups:
        got = {label: one(cls, expr) for label, expr in forms.items()}
        agree.append(any(str(v).rstrip("0").rstrip(".") == want[cls].rstrip("0").rstrip(".") for v in got.values()))
        out.append(f"{cls} (SQL {want[cls]}): " + "; ".join(f"{k} = {v}" for k, v in got.items()))
    return True, f"SQL's value reachable on {sum(agree)}/{len(groups)} groups | " + " | ".join(out)


def k26(p: Probe):
    """The set-operation form in SPARQL (`tlp`): the solutions a filter keeps are every
    solution MINUS those where it is false MINUS those where it is an error
    (`COALESCE(IF(p, 1, 0), 2) = 2`), equal to the direct FILTER under error propagation:
    a comparison, a disjunction with STRSTARTS, IN, !BOUND, !=; counted too."""
    _k_class(p, "k26", {"a": {"i": '"1"^^xsd:long', "s": '"ab"'}, "b": {"i": '"2"^^xsd:long'}, "c": {"s": '"zz"'}, "d": {"i": '"2"^^xsd:long', "s": '"ac"'}, "e": {}})
    pattern = "OPTIONAL { ?n p:i ?i } OPTIONAL { ?n p:s ?s }"
    preds = ["?i > 1", '?i > 1 || STRSTARTS(?s, "a")', "?i IN (1, 2)", "!BOUND(?i)", "?i != 2", "!(?i > 1) && BOUND(?s)"]
    # the right-hand side of a MINUS (or the body of a NOT EXISTS) holding the partition's
    # filter, four ways: the first run (2026-10-12) saw the plain group remove EVERY
    # solution, because a plain FILTER over COALESCE of an OPTIONAL variable is true
    # for every solution here (A10, narrowed 2026-10-13); the BIND form is right
    base = "?n a c:k26 ; p:id ?id ."
    forms = {
        "group": lambda f: f"MINUS {{ {base} {pattern} FILTER ({f}) }}",
        "subselect": lambda f: f"MINUS {{ SELECT ?n ?id WHERE {{ {base} {pattern} FILTER ({f}) }} }}",
        "bind": lambda f: f"MINUS {{ {base} {pattern} BIND (({f}) AS ?t) FILTER (?t) }}",
        "not exists": lambda f: f"FILTER NOT EXISTS {{ {pattern} FILTER ({f}) }}",
    }
    agree: dict = {k: [] for k in forms}
    notes = []
    for pred in preds:
        direct = _k_ids(p, "k26", pattern, pred)
        for form, rhs in forms.items():
            body = f"{{ {base.rstrip(' .')} }} {rhs(f'!({pred})')} {rhs(f'COALESCE(IF({pred}, 1, 0), 2) = 2')}"
            if form == "not exists":
                body = f"{base} {rhs(f'!({pred})')} {rhs(f'COALESCE(IF({pred}, 1, 0), 2) = 2')}"
            try:
                tlp = p.ids(f"SELECT ?id WHERE {{ GRAPH {G} {{ {body} }} }}")
                count = p.sparql(f"SELECT (COUNT(?id) AS ?v) WHERE {{ GRAPH {G} {{ {body} }} }}")[0]["v"]
            except EngineError as e:
                notes.append(f"{form} / {pred}: {str(e)[:70]}")
                continue
            if tlp == direct and count == len(direct):
                agree[form].append(pred)
            else:
                notes.append(f"{form} / {pred}: direct {direct}, set form {tlp}, count {count}")
    whole = [form for form, preds_ok in agree.items() if len(preds_ok) == len(preds)]
    return bool(whole), f"forms agreeing on every filter: {whole or 'NONE'}; per form: " + ", ".join(f"{k} {len(v)}/{len(preds)}" for k, v in agree.items()) + (" | " + " | ".join(notes[:8]) if notes else "")


def k27(p: Probe):
    """Another connection sees none of this one's uncommitted SPARQL updates (the
    transaction door's peer check on rdf): the updates join the SQL transaction (K15)."""
    _k_class(p, "k27", {"a": {"i": '"1"^^xsd:long'}, "b": {"i": '"2"^^xsd:long'}})
    count = f"SELECT (COUNT(?n) AS ?v) WHERE {{ GRAPH {G} {{ ?n a c:k27 }} }}"
    value = f'SELECT ?v WHERE {{ GRAPH {G} {{ ?n a c:k27 ; p:id "a" ; p:i ?v }} }}'
    peer = _peer(p)
    seen = {}
    try:

        def inside():
            p.update(f'INSERT DATA {{ GRAPH {G} {{ <urn:ganda:r:k27z> a c:k27 ; p:id "z" ; p:i "9"^^xsd:long }} }}')
            p.update(f'DELETE {{ GRAPH {G} {{ ?n p:i ?o }} }} INSERT {{ GRAPH {G} {{ ?n p:i "99"^^xsd:long }} }} WHERE {{ GRAPH {G} {{ ?n a c:k27 ; p:id "a" ; p:i ?o }} }}')
            p.update(f"DELETE WHERE {{ GRAPH {G} {{ <urn:ganda:r:k27b> ?p ?o }} }}")
            seen["own"] = (p.sparql(count)[0]["v"], p.sparql(value)[0]["v"])
            seen["peer"] = (peer.sparql(count)[0]["v"], peer.sparql(value)[0]["v"])

        p.s._transaction(inside, commit=False)
        after = (peer.sparql(count)[0]["v"], p.sparql(value)[0]["v"])
    finally:
        _close(peer)
    return _eq((seen.get("own"), seen.get("peer"), after), ((2, 99), (2, 1), (2, 1)))


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
    Fact("K16", "MEDIAN is a SPARQL aggregate, but only with a GROUP BY (an internal error without one), so the renderings group on a bound constant; it interpolates an even count (of 1, 2, 3: 2)", lambda p: _eq(p.sparql(f"SELECT (MEDIAN(?w) AS ?v) WHERE {{ GRAPH {G} {{ ?e p:w ?w . BIND(1 AS ?mg) }} }} GROUP BY ?mg")[0]["v"], 3)),
    Fact("K17", "ORDER BY puts an unbound key first ascending; strings in code point order (BMP)", lambda p: _eq([r["id"] for r in p.sparql(f"SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:t ; p:id ?id . OPTIONAL {{ ?n p:s ?s }} }} }} ORDER BY ASC(?s) ASC(?id)")], ["c", "b", "a"])),
    Fact("K19", "an EXISTS inside GRAPH in an update's WHERE reads the named graph (the rdf write marks its records so)", lambda p: (p.update(f"INSERT {{ GRAPH {G} {{ ?n <urn:ganda:w> true }} }} WHERE {{ GRAPH {G} {{ ?n a c:t . FILTER (EXISTS {{ ?n i:l \"1\"^^xsd:long }}) }} }}"), _eq(p.ids(f"SELECT ?id WHERE {{ GRAPH {G} {{ ?n <urn:ganda:w> true ; p:id ?id }} }}"), ["a"]), p.update(f"DELETE WHERE {{ GRAPH {G} {{ ?n <urn:ganda:w> ?x }} }}"))[1]),
    Fact("K20", "SUM over a value bound where present, or over COALESCE(?w, 0), is exact on a predicate that has held several datatypes (a plain SUM over a group with an unbound value is not: H3)", k20),
    Fact("K18", "UCASE, LCASE, STRLEN, SUBSTR, CONCAT, ABS, STRSTARTS, STRENDS, CONTAINS, REGEX", lambda p: _eq(p.sparql('SELECT (CONCAT(UCASE("ab"), LCASE("C"), SUBSTR("xyz", 1, 1), STR(STRLEN("abc")), STR(ABS(-2))) AS ?v) WHERE { FILTER (STRSTARTS("abc", "a") && STRENDS("abc", "c") && CONTAINS("abc", "b") && REGEX("abc", "^a")) }')[0]["v"], "ABcx32")),
    Fact("K21", "custom keys as percent-encoded IRIs (`dialect.record_iri`) and `p:id` literals: every style round-trips, and ORDER BY ?id (code points) is the UTF-16 order on the key pool (no key holds U+E000..U+FFFF)", k21),
    Fact("K22", "a node typed with its class AND the base class (`a c:n1, c:N0`, the inheritance axis): a query on either class sees it once; the per-class count is unchanged; a DELETE WHERE { ?n ?p ?o } removes both types", k22),
    Fact("K23", "an average of longs: AVG(?w) and SUM(?w) / COUNT(?w) compute in DOUBLE (B13: a sum above 2^53 loses its last bit, the quotient keeps every double digit, unlike SQL's DECIMAL(25,6)); recorded", k23, required=False),
    Fact("K24", "a cross-type pivot in SPARQL: xsd:long against an xsd:double literal and xsd:double against an integer literal compare numerically, exactly as Python for a double pivot below 10^12 (`pivots.cross_type`); a pivot of 2^63 recorded", k24),
    Fact("K25", "an average of longs in xsd:decimal (the sum cast before the division) and its truncation to six places: whether SQL's DECIMAL(25,6) value is reachable in SPARQL on the campaign's groups (B13; recorded, decides the rdf `avg` rendering)", k25, required=False),
    Fact("K26", "the set-operation form in SPARQL: a filter's solutions are every solution MINUS the false ones MINUS the error ones (COALESCE(IF(p, 1, 0), 2) = 2), equal to the direct FILTER; counted too (the `tlp` form on rdf)", k26),
    Fact("K27", "another connection sees none of this one's uncommitted SPARQL INSERT DATA / DELETE-INSERT / DELETE WHERE, and the committed triples after the rollback (the transaction door's peer check)", k27),
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
    # every edge view carries its table's name as the constant attribute KIND (one NVARCHAR
    # type across the edge tables): GraphScript's SubGraph selects one edge type by it (G35)
    p.run("CREATE VIEW gv_pe1 AS SELECT eid, src_id, dst_id, w AS pe1_w, s AS pe1_s, CAST('pe1' AS NVARCHAR(256)) AS kind FROM pe1")
    p.run("CREATE VIEW gv_pe2 AS SELECT eid, src_id, dst_id, w AS pe2_w, CAST('pe2' AS NVARCHAR(256)) AS kind FROM pe2")
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


def g28(p: Probe):
    for n, k in enumerate(CUSTOM_KEYS):
        p.run("INSERT INTO pv2 (id, f1) VALUES (:k, :v)", {"k": k, "v": f"k{n}"})
    try:
        found = [_oc(p, "MATCH (a:pv2) WHERE a.ID = $k RETURN a.ID AS ID", {"k": k}, {"k": ":k"}) for k in CUSTOM_KEYS]
        hits = [r[0]["id"] == k for r, k in zip(found, CUSTOM_KEYS) if r]
        order = [r["id"] for r in _oc(p, "MATCH (a:pv2) WHERE a.PV2_F1 STARTS WITH 'k' RETURN a.ID AS ID ORDER BY a.ID ASC")]
    finally:
        for k in CUSTOM_KEYS:
            p.run("DELETE FROM pv2 WHERE id = :k", {"k": k})
    ok = len(hits) == len(CUSTOM_KEYS) and all(hits) and order == _utf16_order(CUSTOM_KEYS)
    return ok, f"{sum(hits)}/{len(CUSTOM_KEYS)} keys matched by $k; ORDER BY a.ID {'is' if order == _utf16_order(CUSTOM_KEYS) else 'is NOT'} UTF-16 order"


def g29(p: Probe):
    """pv1 and pv2 both have an `f1` of DIFFERENT types (the reason for the views); the
    shared attribute here is the key, and `ID` is what every vertex table has. A second
    shared attribute of one type: add `b1 BIGINT` to both views."""
    p.run("ALTER TABLE pv1 ADD (b1 BIGINT)")
    p.run("ALTER TABLE pv2 ADD (b1 BIGINT)")
    p.run("UPDATE pv1 SET b1 = 1 WHERE id = 'a'")
    p.run("UPDATE pv1 SET b1 = 2 WHERE id = 'b'")
    p.run("UPDATE pv2 SET b1 = 2 WHERE id = 'x'")
    p.run("CREATE VIEW gv2_pv1 AS SELECT id, b1, f1 AS pv1_f1 FROM pv1")
    p.run("CREATE VIEW gv2_pv2 AS SELECT id, b1, f1 AS pv2_f1 FROM pv2")
    p.run("CREATE VIEW gv2_pe1 AS SELECT eid, src_id, dst_id FROM pe1")
    p.run("CREATE VIEW gv2_pe2 AS SELECT eid, src_id, dst_id FROM pe2")
    p.run("CREATE GRAPH WORKSPACE gw6 VERTEX TABLE gv2_pv1 KEY COLUMN id LABEL 'pv1' VERTEX TABLE gv2_pv2 KEY COLUMN id LABEL 'pv2' EDGE TABLE gv2_pe1 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv2_pv1 TARGET COLUMN dst_id REFERENCES gv2_pv2 LABEL 'pe1' EDGE TABLE gv2_pe2 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv2_pv1 TARGET COLUMN dst_id REFERENCES gv2_pv1 LABEL 'pe2'")

    def oc(cypher: str, binds=None, params=None):
        return p.sql(f"SELECT * FROM {opencypher_table(cypher, params).replace('GRAPH WORKSPACE gw ', 'GRAPH WORKSPACE gw6 ')}", binds)

    filtered = sorted(r["id"] for r in oc("MATCH (a) WHERE a.B1 > $p RETURN a.ID AS ID", {"p": 1}, {"p": ":p"}))
    nulls = sorted(r["id"] for r in oc("MATCH (a) WHERE a.B1 IS NULL RETURN a.ID AS ID"))
    joined = sorted((r["id"], r["v"]) for r in oc("MATCH (t), (m) WHERE (m.B1 > t.B1) RETURN t.ID AS ID, m.ID AS V"))
    grouped = sorted(((r["k"], r["v"]) for r in oc("MATCH (a) RETURN a.B1 AS K, COUNT(a.ID) AS V")), key=repr)
    total = oc("MATCH (a) RETURN COUNT(a.ID) AS V")[0]["v"]
    got = (filtered, nulls, joined, grouped, total)
    return _eq(got, (["b", "x"], ["c", "d", "y"], [("a", "b"), ("a", "x")], sorted([(None, 3), (1, 1), (2, 2)], key=repr), 6))


def g30(p: Probe):
    """Integer constants parse exactly (2^63 - 1, -5)."""
    big = _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F1 = 9223372036854775807 RETURN a.ID AS ID")
    neg = _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F1 = -5 RETURN a.ID AS ID")
    return _eq((big, neg), (["b"], ["d"]))


def g31(p: Probe):
    """A DOUBLE constant in the query text is parsed to about fifteen significant digits
    (one and two ulps above 0.5 match the stored 0.5: A8), while a bound parameter one
    or two ulps away does not match. Over pv1.d = 0.5 (a), 2^53 (b), 1e-300 (d)."""
    import math

    one_ulp = math.nextafter(0.5, 1)
    two_ulp = math.nextafter(one_ulp, 1)
    p_one = _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_D = $d RETURN a.ID AS ID", {"d": one_ulp}, {"d": ":d"})
    p_two = _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_D = $d RETURN a.ID AS ID", {"d": two_ulp}, {"d": ":d"})
    l_one = _oc_ids(p, f"MATCH (a:pv1) WHERE a.PV1_D = {one_ulp!r} RETURN a.ID AS ID")
    l_two = _oc_ids(p, f"MATCH (a:pv1) WHERE a.PV1_D = {two_ulp!r} RETURN a.ID AS ID")
    l_far = _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_D = 0.5000000001 RETURN a.ID AS ID")
    return p_one == [] and p_two == [], f"param one ulp above 0.5: {p_one}, two ulps: {p_two} (want []: bound doubles compare exactly); constant {one_ulp!r}: {l_one}, {two_ulp!r}: {l_two}, 0.5000000001: {l_far} (a constant matching 'a' is A8: the renderings never write one)"


def g32(p: Probe):
    """Two OPENCYPHER_TABLE calls in one statement, the second declaring a string parameter
    next to a numeric one: the second call's bare parameters are typed in the wrong order
    (A9: the string one as a number, `-10427` from the client); the call alone is fine."""
    paths = "MATCH (b:pv1)-[e:pe2]->(a:pv1) WHERE (a.PV1_F2 >= $s1 AND a.PV1_F2 <= $s2) AND (e.PE2_W > $w) RETURN a.ID AS ID, COUNT(b.ID) AS V"
    alone = p.error(lambda: _oc(p, paths, {"s1": "a", "s2": "z", "w": 0.0}, {"s1": ":s1", "s2": ":s2", "w": ":w"}))[:90] or "answers"
    starts = opencypher_table("MATCH (a:pv1) WHERE (a.PV1_F2 >= $s1 AND a.PV1_F2 <= $s2) RETURN a.ID AS ID", {"s1": ":s1", "s2": ":s2"})
    both = opencypher_table(paths, {"s1": ":s1", "s2": ":s2", "w": ":w"})
    two = p.error(lambda: p.sql(f"SELECT s.ID AS id, p.V AS v FROM {starts} s LEFT JOIN {both} p ON p.ID = s.ID", {"s1": "a", "s2": "z", "w": 0.0}))[:90] or "answers"
    return True, f"the paths call alone: {alone}; the starts call LEFT JOINed to it: {two}"


def g33(p: Probe):
    """A BOOLEAN attribute inside COUNT, and two BOOLEAN attributes compared: refused
    (`4906`; G10 says BOOLEAN is projected only); a BOOLEAN as a group key answers."""
    count = p.error(lambda: _oc(p, "MATCH (a:pv1) RETURN COUNT(a.PV1_B) AS V"))[:80] or "answers"
    join = p.error(lambda: _oc(p, "MATCH (t:pv1), (m:pv1) WHERE (m.PV1_B <> t.PV1_B) RETURN t.ID AS ID, m.ID AS V"))[:80] or "answers"
    key = p.error(lambda: _oc(p, "MATCH (a:pv1) RETURN a.PV1_B AS K, COUNT(a.ID) AS V"))[:80] or "answers"
    return "answers" not in (count, join) and key == "answers", f"COUNT(bool): {count} | bool <> bool: {join} | bool group key: {key}"


def g34(p: Probe):
    """String constants in the filter: a quote, a 5000-character value and a LIST's JSON
    text inline exactly (the renderings bind no string: A9)."""
    long = "x" * 4999 + "y"
    p.run("INSERT INTO pv2 (id, f1) VALUES ('g34', :v)", {"v": long})
    try:
        got = (_oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F2 = 'it\\'s' RETURN a.ID AS ID"), _oc_ids(p, f"MATCH (a:pv2) WHERE a.PV2_F1 = '{long}' RETURN a.ID AS ID"), _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_L = '[1,2]' RETURN a.ID AS ID"))
    finally:
        p.run("DELETE FROM pv2 WHERE id = 'g34'")
    return _eq(got, (["d"], ["g34"], ["a"]))


def _gs_procedures(p: Probe, ws: str, suffix: str = "") -> list:
    """GraphScript procedures over a workspace: a shortest path OUTGOING, one with the
    direction as a parameter, the neighbours within k hops, a shortest path over the
    subgraph of one edge KIND; the engine's refusals, if any, as text."""
    # every GraphScript variable is `sv_*`: the texts go through `connection.positional`,
    # which binds any other `:name`
    head = f'GRAPH sv_g = Graph("{p.s.database.upper()}", "{ws}");'
    out = 'sv_o = SELECT :sv_v."ID" FOREACH sv_v IN Vertices(:sv_p);'
    ends = "VERTEX sv_va = Vertex(:sv_g, :sv_ia); VERTEX sv_vb = Vertex(:sv_g, :sv_ib);"
    bodies = {
        "p_gs_path_out": ("IN sv_ia NVARCHAR(256), IN sv_ib NVARCHAR(256), OUT sv_o TABLE (ID NVARCHAR(256))", f"{head} {ends} WeightedPath<BIGINT> sv_p = Shortest_Path(:sv_g, :sv_va, :sv_vb, 'OUTGOING'); {out}"),
        "p_gs_path_dir": ("IN sv_ia NVARCHAR(256), IN sv_ib NVARCHAR(256), IN sv_dir NVARCHAR(16), OUT sv_o TABLE (ID NVARCHAR(256))", f"{head} {ends} WeightedPath<BIGINT> sv_p = Shortest_Path(:sv_g, :sv_va, :sv_vb, :sv_dir); {out}"),
        "p_gs_reach": ("IN sv_ia NVARCHAR(256), IN sv_k BIGINT, OUT sv_o TABLE (ID NVARCHAR(256))", f'{head} VERTEX sv_va = Vertex(:sv_g, :sv_ia); MULTISET<Vertex> sv_m = Neighbors(:sv_g, :sv_va, 1, :sv_k, \'OUTGOING\'); sv_o = SELECT :sv_v."ID" FOREACH sv_v IN :sv_m;'),
        "p_gs_path_kind": ("IN sv_ia NVARCHAR(256), IN sv_ib NVARCHAR(256), IN sv_kind NVARCHAR(256), OUT sv_o TABLE (ID NVARCHAR(256))", f'{head} GRAPH sv_sg = SubGraph(:sv_g, sv_e IN Edges(:sv_g) WHERE :sv_e."KIND" == :sv_kind); VERTEX sv_va = Vertex(:sv_sg, :sv_ia); VERTEX sv_vb = Vertex(:sv_sg, :sv_ib); WeightedPath<BIGINT> sv_p = Shortest_Path(:sv_sg, :sv_va, :sv_vb, \'OUTGOING\'); {out}'),
    }
    errors = []
    for name, (sig, body) in bodies.items():
        err = p.error(lambda: p.run(f"CREATE PROCEDURE {name}{suffix} ({sig}) LANGUAGE GRAPH READS SQL DATA AS BEGIN {body} END"))
        if err:
            errors.append(f"{name}: {err[:110]}")
    return errors


def _gs_workspace(p: Probe) -> str:
    """A HOMOGENEOUS workspace beside `gw` for GraphScript (which refused the labelled
    heterogeneous one: `UNKNOWN is not supported as vertex label data type`): one view
    uniting every node table's keys, one uniting every junction table's keys and ends,
    each row carrying its table's name as KIND; no LABEL clauses."""
    p.run("CREATE VIEW gs_v AS SELECT id, CAST('pv1' AS NVARCHAR(256)) AS kind FROM pv1 UNION ALL SELECT id, CAST('pv2' AS NVARCHAR(256)) AS kind FROM pv2")
    p.run("CREATE VIEW gs_e AS SELECT eid, src_id, dst_id, CAST('pe1' AS NVARCHAR(256)) AS kind FROM pe1 UNION ALL SELECT eid, src_id, dst_id, CAST('pe2' AS NVARCHAR(256)) AS kind FROM pe2")
    return p.error(lambda: p.run("CREATE GRAPH WORKSPACE gws VERTEX TABLE gs_v KEY COLUMN id EDGE TABLE gs_e KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id"))


def _gs(p: Probe, call: str, binds: dict) -> list:
    """The ids a GraphScript procedure answers, read through a SQLScript anonymous block
    (the header binds the markers, the body reads `:sv_*` variables; `sv_o` is the
    procedure's table output)."""
    header = ", ".join(f"IN sv_{k} {'BIGINT' if isinstance(v, int) else 'NVARCHAR(256)'} => :{k}" for k, v in binds.items())
    return sorted(r["id"] for r in p.sql(f"DO ({header}) BEGIN CALL {call}; SELECT ID AS id FROM :sv_o; END", binds))


def g35(p: Probe):
    """GraphScript (`DO ... LANGUAGE GRAPH` has no result set, so a procedure per shape,
    read through a SQLScript block): `Shortest_Path` OUTGOING answers the vertices of a
    shortest path (a -> b -> d over pe2: three; a -> x over pe1: two) -- required; the
    direction as a parameter, 'ANY' and 'INCOMING', the unreachable pair, start = goal,
    `Neighbors` within k, and the SubGraph of one edge KIND (recorded)."""
    over_gw = _gs_procedures(p, "GW", "_gw")
    ws_err = _gs_workspace(p)
    errors = _gs_procedures(p, "GWS")
    path = "p_gs_path_out(:sv_a, :sv_b, sv_o)"
    ad = _try(lambda: _gs(p, path, {"a": "a", "b": "d"}))
    ax = _try(lambda: _gs(p, path, {"a": "a", "b": "x"}))
    none = _try(lambda: _gs(p, path, {"a": "d", "b": "a"}), 60)
    same = _try(lambda: _gs(p, path, {"a": "a", "b": "a"}), 60)
    dir_any = _try(lambda: _gs(p, "p_gs_path_dir(:sv_a, :sv_b, :sv_d, sv_o)", {"a": "d", "b": "a", "d": "ANY"}), 60)
    dir_in = _try(lambda: _gs(p, "p_gs_path_dir(:sv_a, :sv_b, :sv_d, sv_o)", {"a": "d", "b": "a", "d": "INCOMING"}), 60)
    reach1 = _try(lambda: _gs(p, "p_gs_reach(:sv_a, :sv_k, sv_o)", {"a": "a", "k": 1}), 60)
    reach2 = _try(lambda: _gs(p, "p_gs_reach(:sv_a, :sv_k, sv_o)", {"a": "a", "k": 2}), 60)
    kind_ab = _try(lambda: _gs(p, "p_gs_path_kind(:sv_a, :sv_b, :sv_k, sv_o)", {"a": "a", "b": "b", "k": "pe2"}), 60)
    kind_ax = _try(lambda: _gs(p, "p_gs_path_kind(:sv_a, :sv_b, :sv_k, sv_o)", {"a": "a", "b": "x", "k": "pe2"}), 60)
    kind_c = _try(lambda: _gs(p, "p_gs_path_kind(:sv_a, :sv_b, :sv_k, sv_o)", {"a": "c", "b": "a", "k": "pe2"}), 60)
    ok = ad == ["a", "b", "d"] and ax == ["a", "x"]
    return ok, (f"over the labelled heterogeneous gw: {(over_gw[0].split(': ', 1)[1] if over_gw else 'compiled')[:90]} | the homogeneous gws (UNION ALL views, no labels): {ws_err[:80] or 'created'}; procedures: {' ; '.join(errors) or 'all four created'} | a->d OUTGOING: {ad} (want a, b, d); a->x: {ax} (want a, x) | d->a OUTGOING: {none}; a->a: {same} | direction parameter: d->a ANY {dir_any}, INCOMING {dir_in} | Neighbors(a, 1..1): {reach1}, (a, 1..2): {reach2} (BFS from a over every type: 1 hop a, b, x; 2 hops + d, y) | SubGraph KIND pe2: a->b {kind_ab}, a->x {kind_ax} (no pe2 path), c->a {kind_c} (c has no pe2 edge)")


def g36(p: Probe):
    """A cross-type pivot in openCypher: the engine refuses a BIGINT attribute against a
    float outright (`Type mismatch between bigint and float`, recorded), so both sides
    are doubles -- `TOFLOAT(a.F) > TOFLOAT('<repr>')` (exact: a BIGINT below 2^53
    converts exactly, and a pivot below 10^12 orders the same against any BIGINT) -- and
    an integer pivot against a DOUBLE attribute is `TOFLOAT('<int>')`; recorded: a bound
    double parameter against a BIGINT attribute."""
    checks = [
        ("f1 > 0.5", _oc_ids(p, "MATCH (a:pv1) WHERE TOFLOAT(a.PV1_F1) > TOFLOAT('0.5') RETURN a.ID AS ID"), ["a", "b"]),
        ("f1 < -0.5", _oc_ids(p, "MATCH (a:pv1) WHERE TOFLOAT(a.PV1_F1) < TOFLOAT('-0.5') RETURN a.ID AS ID"), ["d"]),
        ("f1 = 1.0", _oc_ids(p, "MATCH (a:pv1) WHERE TOFLOAT(a.PV1_F1) = TOFLOAT('1.0') RETURN a.ID AS ID"), ["a"]),
        ("f1 <> 1.0", _oc_ids(p, "MATCH (a:pv1) WHERE TOFLOAT(a.PV1_F1) <> TOFLOAT('1.0') RETURN a.ID AS ID"), ["b", "d"]),
        ("f1 = 1.5 OR f1 = 1.0", _oc_ids(p, "MATCH (a:pv1) WHERE (TOFLOAT(a.PV1_F1) = TOFLOAT('1.5') OR TOFLOAT(a.PV1_F1) = TOFLOAT('1.0')) RETURN a.ID AS ID"), ["a"]),
        ("f1 >= -5.5 AND f1 <= 1.5", _oc_ids(p, "MATCH (a:pv1) WHERE (TOFLOAT(a.PV1_F1) >= TOFLOAT('-5.5') AND TOFLOAT(a.PV1_F1) <= TOFLOAT('1.5')) RETURN a.ID AS ID"), ["a", "d"]),
        ("f1 > 1e12 (2^63 - 1 converted)", _oc_ids(p, "MATCH (a:pv1) WHERE TOFLOAT(a.PV1_F1) > TOFLOAT('999999999999.5') RETURN a.ID AS ID"), ["b"]),
        ("d > 0", _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_D > TOFLOAT('0') RETURN a.ID AS ID"), ["a", "b", "d"]),
        ("d = 1e-300", _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_D = TOFLOAT('1e-300') RETURN a.ID AS ID"), ["d"]),
        ("d = 2^53", _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_D = TOFLOAT('9007199254740992') RETURN a.ID AS ID"), ["b"]),
        ("d <> 0", _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_D <> TOFLOAT('0') RETURN a.ID AS ID"), ["a", "b", "d"]),
    ]
    bad = [f"{label}: got {got} want {want}" for label, got, want in checks if got != want]
    mixed = _try(lambda: _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F1 > TOFLOAT('0.5') RETURN a.ID AS ID"), 70)
    plain_int = _try(lambda: _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_D > 0 RETURN a.ID AS ID"), 70)
    bound = _try(lambda: _oc_ids(p, "MATCH (a:pv1) WHERE a.PV1_F1 >= $p RETURN a.ID AS ID", {"p": 1.5}, {"p": ":p"}), 70)
    return not bad, (f"{len(checks)} comparisons exact with both sides TOFLOAT" if not bad else " | ".join(bad)) + f"; BIGINT attribute against TOFLOAT: {mixed}; DOUBLE attribute against an integer constant: {plain_int}; f1 >= $p with 1.5 bound: {bound}"


def g37(p: Probe):
    """Another connection's OPENCYPHER_TABLE sees none of this one's uncommitted writes to
    the workspace's tables (the dynamic cache; the transaction door's peer check)."""
    peer = _peer(p)
    seen = {}
    count = "MATCH (a:pv1) RETURN COUNT(a.ID) AS V"
    value = "MATCH (a:pv1) WHERE a.ID = 'a' RETURN a.PV1_F1 AS V"
    try:

        def inside():
            p.run("INSERT INTO pv1 (id, f1) VALUES ('z', 9)")
            p.run("UPDATE pv1 SET f1 = 99 WHERE id = 'a'")
            seen["own"] = (_oc(p, count)[0]["v"], _oc(p, value)[0]["v"])
            seen["peer"] = (_oc(peer, count)[0]["v"], _oc(peer, value)[0]["v"])

        p.s._transaction(inside, commit=False)
        after = (_oc(peer, count)[0]["v"], _oc(p, value)[0]["v"])
    finally:
        _close(peer)
    return _eq((seen.get("own"), seen.get("peer"), after), ((5, 99), (4, 1), (4, 1)))


# `GF`, not `G`: `G` is the graph IRI token the K and P facts interpolate at call time
GF = [
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
    Fact("G28", "custom keys through the workspace: every style matched by `a.ID = $k` (bound bare) and projected back; ORDER BY a.ID is the UTF-16 order", g28),
    Fact("G29", "the base class as an UNLABELLED vertex (the inheritance axis): `MATCH (a) WHERE a.F1 ...` over an attribute both vertex tables share (one type), a Cartesian `MATCH (t), (m)` correlated on it, COUNT grouped by it; a LIST attribute's text", g29),
    Fact("G30", "integer constants parse exactly (2^63 - 1, -5): the renderings inline every integer pivot", g30),
    Fact("G31", "a bound DOUBLE parameter compares exactly (one ulp off is not equal); a DOUBLE constant in the text loses digits beyond the fifteenth (A8, recorded in the detail)", g31),
    Fact("G32", "two OPENCYPHER_TABLE calls in one statement: the second call's bare parameters typed in the wrong order when they mix a string and a number (A9; recorded -- the renderings bind floats only)", g32, required=False),
    Fact("G33", "a BOOLEAN attribute inside COUNT and two BOOLEAN attributes compared are refused (4906); a BOOLEAN group key answers", g33),
    Fact("G34", "string constants inline exactly: a quote, a 5000-character value, a LIST's JSON text", g34),
    Fact("G35", "GraphScript through a procedure and a SQLScript block: Shortest_Path OUTGOING answers a shortest path's vertices; the direction as a parameter, the unreachable pair, start = goal, Neighbors within k, SubGraph of one edge KIND (recorded)", g35),
    Fact("G36", "a cross-type pivot in openCypher: TOFLOAT('<repr>') against a BIGINT attribute and an integer constant against a DOUBLE attribute compare exactly (`pivots.cross_type`); a bound double against BIGINT recorded", g36),
    Fact("G37", "another connection's OPENCYPHER_TABLE sees none of this one's uncommitted INSERT / UPDATE on the workspace's tables, and the committed rows after the rollback (the transaction door's peer check)", g37),
    Fact("G26", "DROP SCHEMA ... CASCADE removes the schema's graph workspace", g26),
]


# --- plan steering: the hint profiles over the statement shapes the renderers emit --------------


def p_setup(p: Probe):
    """Column tables with a child table, a collection, a view over it, a heterogeneous
    workspace and a few triples in the session's graph: one of each statement shape the
    renderers close with a hint clause."""
    p.run("CREATE COLUMN TABLE ph1 (id NVARCHAR(256) PRIMARY KEY, i BIGINT, s NVARCHAR(5000))")
    p.run("CREATE COLUMN TABLE ph2 (id NVARCHAR(256) PRIMARY KEY, i BIGINT)")
    p.run("CREATE COLUMN TABLE ph1__l (owner NVARCHAR(256) NOT NULL, pos INTEGER NOT NULL, v BIGINT)")
    p.run("CREATE COLUMN TABLE phe1 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL)")
    p.run("CREATE COLUMN TABLE phe2 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL)")
    for r in [("a", 1, "x"), ("b", 2, "y"), ("c", None, None), ("d", 2, "x"), ("e", 5, "y")]:
        p.run("INSERT INTO ph1 (id, i, s) VALUES (:a, :b, :c)", dict(zip("abc", r)))
    for r in [("m", 2), ("n", None), ("o", 5)]:
        p.run("INSERT INTO ph2 (id, i) VALUES (:a, :b)", dict(zip("ab", r)))
    for r in [("a", 0, 1), ("a", 1, 2), ("d", 0, 7)]:
        p.run("INSERT INTO ph1__l (owner, pos, v) VALUES (:a, :b, :c)", dict(zip("abc", r)))
    for r in [("f0", "a", "b"), ("f1", "a", "d"), ("f2", "b", "b")]:
        p.run("INSERT INTO phe1 (eid, src_id, dst_id) VALUES (:a, :b, :c)", dict(zip("abc", r)))
    p.run("INSERT INTO phe2 (eid, src_id, dst_id) VALUES ('f3', 'd', 'a')")
    p.run("CREATE VIEW gv_ph1 AS SELECT id, i AS ph1_i, s AS ph1_s FROM ph1")
    p.run("CREATE VIEW gv_phe1 AS SELECT eid, src_id, dst_id FROM phe1")
    p.run("CREATE VIEW gv_phe2 AS SELECT eid, src_id, dst_id FROM phe2")
    p.run("CREATE GRAPH WORKSPACE phw VERTEX TABLE gv_ph1 KEY COLUMN id LABEL 'ph1' EDGE TABLE gv_phe1 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_ph1 TARGET COLUMN dst_id REFERENCES gv_ph1 LABEL 'phe1' EDGE TABLE gv_phe2 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_ph1 TARGET COLUMN dst_id REFERENCES gv_ph1 LABEL 'phe2'")
    p.run("CREATE COLLECTION phc")
    for d in [{"id": "a", "i": 1, "l": [1, 2]}, {"id": "b", "i": 2}, {"id": "c"}]:
        p.run("INSERT INTO phc VALUES(:doc)", {"doc": json.dumps(d)})
    p.update(f'INSERT DATA {{ GRAPH {G} {{ <urn:ganda:r:ph:a> a c:ph ; p:id "a" ; p:i "1"^^xsd:long . <urn:ganda:r:ph:b> a c:ph ; p:id "b" . }} }}')
    blocked = p.error(lambda: p.sql("SELECT 1 AS v FROM DUMMY WITH HINT (THROW_ERROR)"))
    unknown = p.error(lambda: p.sql("SELECT 1 AS v FROM DUMMY WITH HINT (GANDA_NO_SUCH_HINT)")) or "accepted (ignored)"
    return bool(blocked), f"THROW_ERROR: {blocked[:80] or 'NOT blocked'}; an unknown hint name: {unknown[:80]}"


def _oct(ws: str, cypher: str) -> str:
    return f"OPENCYPHER_TABLE(GRAPH WORKSPACE {ws} QUERY '{cypher}')"


# (label, SQL, binds): the shapes a hinted variant takes
P_SHAPES = [
    ("cte, derived table, LEFT JOIN, correlated scalar subquery, GROUP BY", "WITH v AS (SELECT id, i, s FROM ph1) SELECT COUNT(*) AS v FROM (SELECT x.id FROM v x LEFT JOIN phe1 e ON e.src_id = x.id WHERE (SELECT COUNT(*) FROM v y WHERE y.id = x.id) = 1 GROUP BY x.id) z", {}),
    ("filter, GROUP BY, ORDER BY NULLS, LIMIT OFFSET", "SELECT s AS k, COUNT(*) AS v FROM ph1 WHERE i > :p OR i IS NULL GROUP BY s ORDER BY s ASC NULLS FIRST LIMIT 2 OFFSET 1", {"p": 1}),
    ("semi-join IN (SELECT ...), EXISTS over the child table, a disjunction", "SELECT t.id AS id FROM ph1 t WHERE t.i IN (SELECT u.i FROM ph2 u WHERE u.i IS NOT NULL) OR EXISTS (SELECT 1 FROM ph1__l x WHERE x.owner = t.id AND x.v = :e)", {"e": 7}),
    ("a self-join's count per record, DISTINCT, NOT IN", "SELECT DISTINCT t.id AS id, (SELECT COUNT(*) FROM ph1 m WHERE m.i > t.i) AS v FROM ph1 t WHERE t.id NOT IN (SELECT e.src_id FROM phe2 e)", {}),
    ("PERCENTILE_CONT in a scalar subquery over DUMMY", "SELECT (SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY i) FROM ph1) AS v FROM DUMMY", {}),
    ("a view over a collection (the doc views)", 'WITH raw_v AS (SELECT $."id" AS id, $."i" AS i FROM phc), v AS (SELECT id, TO_BIGINT(i) AS i FROM raw_v) SELECT COUNT(*) AS v FROM v t WHERE t.i > :p', {"p": 0}),
    ("the starts LEFT JOINed to OPENCYPHER_TABLE paths (the graph wrappers)", f"SELECT s.ID AS id, COALESCE(p.V, 0) AS v FROM {_oct('phw', 'MATCH (a:ph1) RETURN a.ID AS ID')} s LEFT JOIN {_oct('phw', 'MATCH (a:ph1)-[e:phe1]->(b:ph1) RETURN a.ID AS ID, COUNT(b.ID) AS V')} p ON p.ID = s.ID", {}),
    ("SPARQL_TABLE (rdf's table_fn position)", f"SELECT * FROM SPARQL_TABLE('{PREFIX} SELECT ?id WHERE {{ GRAPH {G} {{ ?n a c:ph ; p:id ?id }} }}')", {}),
]
# the collection query (hinted in the provider since D27; here its acceptance is recorded
# per profile, D27 compares the rows)
P_COLLECTION = ("the collection query itself", 'SELECT $."id" AS id FROM phc WHERE $."i" > CAST(:p AS BIGINT)', {"p": 0})


def _rows(p: Probe, text: str, binds: dict):
    return sorted((tuple(sorted(r.items())) for r in p.sql(text, binds)), key=repr)


def p_profile(profile: str):
    def check(p: Probe):
        bad = []
        for label, text, binds in P_SHAPES:
            plain = _rows(p, text, binds)
            try:
                steered = _rows(p, hints.hinted(text, profile), binds)
            except EngineError as e:
                bad.append(f"{label}: {str(e)[:90]}")
                continue
            if steered != plain:
                bad.append(f"{label}: plain {plain} hinted {steered}")
        coll = p.error(lambda: p.sql(hints.hinted(P_COLLECTION[1], profile), P_COLLECTION[2]))[:60] or "accepted"
        return not bad, (f"{len(P_SHAPES)} shapes answer alike; collection query: {coll}" if not bad else " | ".join(bad))

    return check


def p_obs(hint: str):
    """A hint the provider does not use: what each shape does with it (recorded)."""

    def check(p: Probe):
        out = []
        for label, text, binds in P_SHAPES:
            err = p.error(lambda: p.sql(f"{text} WITH HINT ({hint})", binds))
            out.append(f"{label.split(',')[0]}: {err[:60] if err else 'ok'}")
        return True, " | ".join(out)

    return check


def _block(text: str, binds: dict, form: str) -> str:
    """A statement as a SQLScript anonymous block (the `block` positions): the binds typed
    BIGINT in the header (every P bind is one), the body reading `:sv_*` variables;
    `select` runs the statement as the block's own SELECT, `table` through a table
    variable, `noinline` with NO_INLINE on the assignment."""
    header = ", ".join(f"IN sv_{k} BIGINT => :{k}" for k in binds)
    body = text
    for k in binds:
        body = body.replace(f":{k}", f":sv_{k}")
    do = f"DO ({header}) BEGIN" if header else "DO BEGIN"
    if form == "select":
        return f"{do} {body}; END"
    hint = " WITH HINT (NO_INLINE)" if form == "noinline" else ""
    return f"{do} sv_t = {body}{hint}; SELECT * FROM :sv_t; END"


def p20(p: Probe):
    """SQLScript anonymous blocks: every statement shape answers its plain rows as the
    block's own SELECT, through a table variable, and with NO_INLINE on the assignment,
    the binds typed in the header; a hinted inner SELECT and the collection query inside
    a block (recorded)."""
    bad = []
    for label, text, binds in P_SHAPES:
        plain = _rows(p, text, binds)
        for form in ("select", "table", "noinline"):
            try:
                got = _rows(p, _block(text, binds, form), binds)
            except EngineError as e:
                bad.append(f"{label.split(',')[0]} / {form}: {str(e)[:80]}")
                continue
            if got != plain:
                bad.append(f"{label.split(',')[0]} / {form}: {got} vs {plain}")
    label, text, binds = P_SHAPES[1]
    hinted = _try(lambda: _rows(p, _block(hints.hinted(text, "hex"), binds, "table"), binds) == _rows(p, text, binds), 70)
    coll = _try(lambda: _rows(p, _block(P_COLLECTION[1], P_COLLECTION[2], "table"), P_COLLECTION[2]) == _rows(p, P_COLLECTION[1], P_COLLECTION[2]), 70)
    return not bad, (f"{len(P_SHAPES)} shapes x 3 block forms answer alike" if not bad else " | ".join(bad)) + f"; a hinted inner SELECT: {hinted}; the collection query in a block: {coll}"


P = [Fact("P0", "WITH HINT (...) closes a SELECT; THROW_ERROR blocks a statement (the clause is parsed); an unknown hint name (recorded)", p_setup)]
P += [Fact(f"P{n}", f"profile `{name}` ({', '.join(hint_names)}): every statement shape answers as its plain twin does", p_profile(name)) for n, (name, hint_names) in enumerate(hints.PROFILES.items(), 1)]
# P20: a fixed id (the profile facts above are numbered after `hints.PROFILES`; the
# former P17, IGNORE_PLAN_CACHE as an observation, is the profile `plan_cache` now)
P += [Fact("P20", "SQLScript anonymous blocks: every statement shape answers its plain rows as the block's own SELECT, through a table variable, and with NO_INLINE on the assignment, the binds typed in the header (the `block` positions); a hinted inner SELECT and the collection query in a block recorded", p20)]

FACTS = R + D + K + GF + P


def main(argv=None) -> int:
    from ganda.hana.engine.connection import HanaServer

    wanted = list(argv if argv is not None else sys.argv[1:])
    facts = [f for f in FACTS if not wanted or any(f.id == w or f.id.startswith(w) and w.isalpha() for w in wanted)]
    failed = []
    with HanaServer() as server:
        sessions = {}
        for kind in ("rel", "doc", "rdf", "graph"):
            sessions[kind] = server.fresh_session(f"fz_probe_{kind}")
        try:
            for f in facts:
                p = Probe(sessions[{"R": "rel", "D": "doc", "K": "rdf", "G": "graph", "P": "rel"}[f.id[0]]])
                try:
                    ok, detail = f.check(p)
                except Exception as e:  # noqa: BLE001 -- the fact did not hold: say how
                    ok, detail = False, f"{type(e).__name__}: {str(e)[:300]}"
                    if not isinstance(e, EngineError):
                        detail += "\n" + traceback.format_exc(limit=2)
                mark = "ok  " if ok else ("FAIL" if f.required else "obs ")
                if not ok and f.required:
                    failed.append(f.id)
                print(f"{mark} {f.id:4s} {f.text}\n          {detail}")
        finally:
            for kind in sessions:
                try:
                    server.drop_database(f"fz_probe_{kind}")
                except Exception:  # noqa: BLE001
                    pass
    print(f"{len(facts) - len(failed)}/{len(facts)} facts hold" + (f"; {len(failed)} REQUIRED facts fail: change the provider, never the probe" if failed else ""))
    for fid in failed:
        print(f"  {fid} backs: {', '.join(backed_by(fid))}")
    return 1 if failed else 0


def backed_by(fid: str) -> list:
    """The rules of `rules/catalog.py` a probe fact checks (`rules/checks.py`)."""
    from ganda.hana.rules import CATALOG

    return [f"{r.question.name} ({r.subject.value})" for r in CATALOG.checks.get(fid, ())] or ["no rule"]


if __name__ == "__main__":
    sys.exit(main())
