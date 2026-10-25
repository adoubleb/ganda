#!/usr/bin/env python3
"""Re-check every SurrealQL fact the fuzzer's design rests on, live, and exit non-zero
if one has changed. Each check is one statement (or a few) with the expected answer
observed on 3.3.4; the output names the rule a failing check checks, or the defect it
reproduces, and the code reading it. A failing check of a rule (or of nothing in the
catalog) fails the run; a defect that no longer reproduces is recorded as fixed on the
build. A run in which every check of a rule holds writes `rules/baseline.json` and
re-renders `RULES.md`.

    python3 -m ganda surrealdb probe                # Docker
    SURREAL_URL=http://127.0.0.1:8000 python3 -m ganda surrealdb probe
    python3 -m ganda surrealdb probe C4 C2          # some checks (an id or a prefix)
"""
from __future__ import annotations

import pathlib
import sys

from ganda.core.engine.provider import EngineError
from ganda.core.rules.model import Defect
from ganda.surrealdb.engine.connection import SurrealServer
from ganda.surrealdb.engine.limits import HTTP_MAX_SQL_BODY_SIZE

DB = "fz_probe_surql"
ran: list = []
failures: list = []
_only: list = []


def check(fid: str, name: str, got, expected) -> None:
    """One fact: `fid` names it in `rules/checks.py`, which lists the rules it checks and
    the defects it reproduces. `got` may be a callable, run only when the check is asked."""
    if _only and not any(fid == o or fid.startswith(o) for o in _only):
        return
    try:
        got = got() if callable(got) else got
    except EngineError as e:
        got = f"rejected: {str(e)[:120]}"
    ok = got == expected
    print(f"{'ok  ' if ok else 'FAIL'} {fid:5s} {name}: {got!r}" + ("" if ok else f"  (expected {expected!r})"))
    ran.append(fid)
    if not ok and fid not in failures:
        failures.append(fid)


def val(s, text: str):
    """The last statement's value."""
    return s.query(text)[-1]


def err(s, text: str) -> str:
    try:
        s.query(text)
        return "ok"
    except EngineError as e:
        return "timeout" if e.timeout else "internal" if e.internal else "rejected"


def main(facts: "list | None" = None) -> int:
    global _only
    _only = list(facts or [])
    ran.clear()
    failures.clear()
    with SurrealServer() as server:
        s = server.fresh_session(DB)
        s.query("DEFINE TABLE n SCHEMALESS; DEFINE TABLE m SCHEMALESS; DEFINE TABLE e SCHEMALESS; CREATE n:a SET f = 1, s = 'a'; CREATE n:b SET f = NULL; CREATE n:c; CREATE m:x SET f = 10; CREATE m:y;")
        s.query("RELATE n:a->e:1->m:x SET w = 1; RELATE n:a->e:2->m:x SET w = 2; RELATE n:b->e:3->m:y; RELATE n:c->e:4->n:a;")

        print("--- values")
        check("C1", "strings order by code point", lambda: val(s, 'RETURN ["\U0001F600" > "￿", "Z" < "a", "é" > "z"];'), [True, True, True])
        check("C2", "NONE and NULL sort below every value", lambda: val(s, "RETURN [NONE < 5, NULL < 5, NONE < '', NULL < false, NONE <= 0, NULL != 5, NONE = 5, NULL >= 5];"), [True, True, True, True, True, True, False, False])
        check("C3", "NONE and NULL are not equal", lambda: val(s, "RETURN [NONE = NULL, NONE == NULL, NONE IS NULL, NULL IS NONE];"), [False, False, False, False])
        check("C4", "an omitted field is NONE, not NULL; an explicit null is NULL", lambda: val(s, "SELECT VALUE [f IS NONE, f IS NULL] FROM [n:b, n:c];"), [[False, True], [True, False]])
        check("C5", "numbers compare by value across int, float and decimal", lambda: val(s, "RETURN [1 = 1.0, 0.1dec = 0.1, 1 = 1.0dec, 2 < 2.5, 2.5 < 3dec];"), [True, True, True, True, True])
        check("C6", "-0.0 equals 0.0 and is not below it", lambda: val(s, "RETURN [-0.0 = 0.0, -0.0 < 0.0];"), [True, False])
        check("C7", "a decimal arrives as its text", lambda: val(s, "RETURN [1.5dec, 5.0dec, 1000000.25dec, 0.001dec];"), ["1.5", "5", "1000000.25", "0.001"])
        check("C8", "a datetime arrives as ISO text", lambda: val(s, 'RETURN [d"1970-01-01T00:00:00Z", d"2024-02-29T23:59:59Z"];'), ["1970-01-01T00:00:00Z", "2024-02-29T23:59:59Z"])
        check("C9", "int addition past the 64-bit range is refused", lambda: err(s, "RETURN 9223372036854775807 + 1;"), "rejected")
        check("C10", "math::sum over ints wraps past the 64-bit range (F26)", lambda: val(s, "RETURN math::sum([9223372036854775807, 1]);"), -(2**63))
        check("C11", "math::sum over nothing is 0", lambda: val(s, "RETURN math::sum([]);"), 0)
        check("C12", "array::max / array::min / math::max over nothing are NONE", lambda: val(s, "RETURN [array::max([]), array::min([]), math::max([])];"), [None, None, None])
        check("C27", "a sum of ints inside the 64-bit range is exact", lambda: val(s, "RETURN math::sum([4611686018427387904, 4611686018427387903, -1]);"), 2**63 - 2)
        check("C28", "a decimal sum is exact", lambda: val(s, "RETURN [math::sum([0.1dec, 0.2dec]) = 0.3dec, 0.1dec + 0.2dec = 0.3dec];"), [True, True])

        print("--- statement forms")
        check("C13", "the arrows answer parallel edges twice", lambda: val(s, "SELECT VALUE ->e->m FROM n:a;"), [["m:x", "m:x"]])
        check("C14", "a SELECT over an undeclared table is refused", lambda: err(s, "SELECT * FROM nope;"), "rejected")
        check("C19", "$parent inside a closure is the enclosing record", lambda: val(s, "SELECT VALUE (SELECT VALUE array::len(array::filter(->e->m, |$t| $t = $parent.k)) FROM ONLY n:a) FROM (SELECT m:x AS k FROM n:a);"), [2])
        check("C20", "$parent inside a bracket filter is not", lambda: val(s, "SELECT VALUE (SELECT VALUE array::len((->e->m)[WHERE $this = $parent.k]) FROM ONLY n:a) FROM (SELECT m:x AS k FROM n:a);"), [0])
        check("C26", "a SELECT reads several tables at once", lambda: val(s, "SELECT VALUE id FROM n, m WHERE id IN [n:a, m:x] ORDER BY id;"), ["m:x", "n:a"])
        check("C24", "a SCHEMAFULL field admits an explicit null beside its type", lambda: err(s, "DEFINE TABLE sf SCHEMAFULL; DEFINE FIELD f ON sf TYPE option<int | null>; CREATE sf:a SET f = NULL; CREATE sf:b SET f = 1; CREATE sf:c;"), "ok")

        print("--- recursion")
        s.query("CREATE n:p; CREATE n:q; CREATE n:r; RELATE n:p->e:5->n:q; RELATE n:q->e:6->n:r;")
        check("C21", "the shortest path idiom answers the path without the start", lambda: val(s, "RETURN n:p.{..+shortest=n:r}(->e->n);"), ["n:q", "n:r"])
        check("C22", "... NONE when unreachable", lambda: val(s, "RETURN n:r.{..+shortest=n:p}(->e->n);"), None)
        check("C23", "... NONE for the same record", lambda: val(s, "RETURN n:p.{..+shortest=n:p}(->e->n);"), None)
        check("C41", "F6: a bound below the distance answers nested paths instead of NONE", lambda: val(s, "RETURN n:p.{..1+shortest=n:r}(->e->n);"), [["n:q"]])

        print("--- writes and transactions")
        check("C25", "RELATE takes an explicit edge id", lambda: val(s, "RELATE n:c->e:99->m:y SET w = 9; SELECT VALUE id FROM e:99;"), ["e:99"])
        check("C15", "deleting a record cascades its relations", lambda: val(s, "CREATE n:d; RELATE n:d->e:7->m:y; RELATE m:x->e:8->n:d; DELETE n:d; SELECT VALUE id FROM e WHERE id IN [e:7, e:8];"), [])
        check("C16", "a link to a deleted record reads NONE through a field", lambda: val(s, "CREATE m:z SET f = 1; CREATE n:l SET e1 = [{ target: m:z }]; DELETE m:z; SELECT VALUE [(e1 ?? [])[WHERE target.id IS NOT NONE], (e1 ?? []).target.f] FROM ONLY n:l;"), [[], [None]])
        check("C17", "a LET bound inside a cancelled transaction keeps its value", lambda: val(s, "BEGIN TRANSACTION; UPDATE n:a SET f = 7; LET $r = (SELECT VALUE f FROM ONLY n:a); CANCEL TRANSACTION; RETURN [$r, n:a.f];"), [7, 1])
        check("C18", "every statement of a cancelled transaction reports itself cancelled", lambda: [x.get("status") for x in s._http.post(f"{s.base_url}/sql", data=f"USE NS ganda DB {DB}; BEGIN TRANSACTION; UPDATE n:a SET f = 8; SELECT VALUE f FROM n:a; CANCEL TRANSACTION;".encode()).json()][2:4], ["ERR", "ERR"])
        check("C42", "F21: an index defined in a transaction after its data stays empty", lambda: val(s, "DEFINE TABLE t21 SCHEMALESS; BEGIN TRANSACTION; CREATE t21:a SET f = 1; DEFINE INDEX ix_t21_f ON TABLE t21 FIELDS f; COMMIT TRANSACTION; SELECT VALUE id FROM t21 WHERE f = 1;"), [])

        print("--- indexes")
        s.query("DEFINE TABLE t28 SCHEMALESS; DEFINE INDEX ix_t28_f ON TABLE t28 FIELDS f; CREATE t28:a SET f = [1]; CREATE t28:b SET f = [1, 2]; CREATE t28:c SET f = [];")
        check("C40", "F28: an index over an array field answers a whole-array equality with nothing", lambda: val(s, "RETURN [(SELECT VALUE id FROM t28 WHERE f = [1]), (SELECT VALUE id FROM t28 WHERE f IN [[1], [2]]), (SELECT VALUE id FROM t28 WHERE [1] = f), (SELECT VALUE id FROM t28 WHERE f != [1]), (SELECT VALUE id FROM t28 WHERE f CONTAINS 1)];"), [[], [], [], ["t28:b", "t28:c"], ["t28:a", "t28:b"]])
        s.query("DEFINE TABLE t29 SCHEMALESS; DEFINE INDEX ix_t29_f ON TABLE t29 FIELDS f; DEFINE TABLE s29 SCHEMALESS; CREATE t29:a SET f = 0.3dec; CREATE t29:b SET f = 2.5dec; CREATE s29:a SET f = 0.3dec; CREATE s29:b SET f = 2.5dec;")
        check("C44", "F29: an index over a decimal column misses a non-dyadic float constant the scan matches", lambda: val(s, "RETURN [(SELECT VALUE id FROM t29 WHERE f = 0.3), (SELECT VALUE id FROM s29 WHERE f = 0.3), (SELECT VALUE id FROM t29 WHERE f = 2.5), (SELECT VALUE id FROM t29 WHERE f IN [0.3, 2.5]), (SELECT VALUE id FROM t29 WHERE f = 0.3dec)];"), [[], ["s29:a"], ["t29:b"], ["t29:b"], ["t29:a"]])
        check("C31", "a 16 KiB string is an index key", lambda: val(s, "DEFINE TABLE t31 SCHEMALESS; DEFINE INDEX ix_t31_f ON TABLE t31 FIELDS f; CREATE t31:a SET f = string::repeat('x', 16384); SELECT VALUE string::len(f) FROM t31 WHERE f = string::repeat('x', 16384);"), [16384])

        print("--- limits and errors")
        check("C29", f"a {HTTP_MAX_SQL_BODY_SIZE // 2 // (1024 * 1024)} MiB request body is read (the server runs with the body bound)", lambda: val(s, "RETURN string::len('" + "x" * (HTTP_MAX_SQL_BODY_SIZE // 2) + "');"), HTTP_MAX_SQL_BODY_SIZE // 2)
        check("C30", "500 statements run in one request", lambda: len(s.query("\n".join(f"CREATE t30:r{i} SET f = {i};" for i in range(500)))), 500)
        check("C43", "a fired TIMEOUT is read as a timeout", lambda: err(s, "SELECT * FROM n TIMEOUT 1ns;"), "timeout")

        server.drop_database(DB)

    from ganda.surrealdb.rules import BASELINE, CATALOG, read_index, rendered

    blocking, fixed = _sort_failures(CATALOG)
    index = read_index()
    if failures:
        print("\nfailing checks:")
        for fid in failures:
            for line in consequences(fid, index):
                print(f"  {fid}: {line}")
    version = _version()
    print(BASELINE.record(version or "unknown", [f for f in ran if f not in failures], fixed, blocking, partial=bool(_only)))
    if not blocking:
        pathlib.Path(__file__).with_name("RULES.md").write_text(rendered())
    print(f"\n{len(ran) - len(failures)} of {len(ran)} checks hold" + (f"; fixed defects: {', '.join(fixed)}" if fixed else ""))
    return 1 if blocking else 0


def _version() -> "str | None":
    with SurrealServer() as server:
        return server.version()


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
    from ganda.surrealdb.rules import CATALOG

    return [_name(item) for item in CATALOG.checks.get(fid, ())] or ["no rule"]


def consequences(fid: str, index: dict) -> list:
    """What a failing check means: each rule it checks no longer holds, each defect it
    reproduces is gone, with the code reading them (`rules.read_index`)."""
    from ganda.surrealdb.rules import CATALOG

    out = []
    for item in CATALOG.checks.get(fid, ()):
        if isinstance(item, Defect):
            what, reads = "no longer reproduces", index["defects"].get(item.id, [])
        else:
            what, reads = "no longer holds", index["rules"].get((item.question.name, item.subject), [])
        sites = ", ".join(f"{r.path.removeprefix('ganda/surrealdb/')}:{r.line}" for r in reads)
        out.append(f"{_name(item)} {what}: read by {sites or 'nothing'}")
    return out or ["checks no rule"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
