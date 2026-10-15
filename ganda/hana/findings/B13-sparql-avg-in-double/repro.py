"""B13 -- the triple store averages xsd:long in DOUBLE: `AVG(?w)` and `SUM(?w) / COUNT(?w)`
answer double(SUM) / COUNT -- the sum is converted to a double first, so a sum above 2^53
loses its low bits (608 + 608 + 9007199254740993 = 9007199254742209, over three rows:
3002399751580736 instead of the exact 3002399751580736.333...), and a quotient keeps seventeen
digits; HANA SQL's AVG over BIGINT is DECIMAL(25,6), the exact quotient truncated to six
places. SPARQL 1.1 says integer division yields xsd:decimal and AVG is Sum / Count.
Run: python3 repro.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _conn import connect, sparql_values, version  # noqa: E402

G = "<urn:ganda:b13>"
P = "PREFIX c: <urn:ganda:c:> PREFIX p: <urn:ganda:p:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"
S = f"B13_{int(time.time())}"
# -5479 over 29 rows: the quotient -188.93103448275862068...; 7 over 128 rows: 0.0546875;
# the campaign's group (camp12h-16): 608 + 608 + 9007199254740993 = 9007199254742209, a sum
# just above 2^53 (not a double: its nearest is 9007199254742208), over three rows: the exact
# mean 3002399751580736.333..., double(sum) / 3 = 3002399751580736
GROUPS = {"a": [-5479] + [0] * 28, "b": [7] + [0] * 127, "c": [608, 608, 9007199254740993]}


def avg(cur, cls: str, expr: str) -> str:
    try:
        return sparql_values(cur, f"{P} SELECT (STR({expr}) AS ?v) WHERE {{ GRAPH {G} {{ ?n a c:{cls} ; p:w ?w }} }}", "v")[0]
    except Exception as e:  # noqa: BLE001
        return f"[{getattr(e, 'errorcode', '?')}] {str(getattr(e, 'errortext', e))[:80]}"


def main():
    conn = connect()
    cur = conn.cursor()
    print("HANA version:", version(cur), "\n")
    try:
        from _conn import sparql

        sparql(cur, f"{P} DROP SILENT GRAPH {G}", update=True)
        triples = " . ".join(f'<urn:ganda:r:{cls}{k}> a c:{cls} ; p:w "{v}"^^xsd:long' for cls, vs in GROUPS.items() for k, v in enumerate(vs))
        sparql(cur, f"{P} INSERT DATA {{ GRAPH {G} {{ {triples} }} }}", update=True)
        cur.execute(f"CREATE SCHEMA {S}")
        cur.execute(f"SET SCHEMA {S}")
        cur.execute("CREATE COLUMN TABLE t (cls NVARCHAR(1), w BIGINT)")
        for cls, vs in GROUPS.items():
            for v in vs:
                cur.execute("INSERT INTO t VALUES (?, ?)", (cls, v))
        for cls, label in (("a", "-5479 / 29  "), ("b", "7 / 128     "), ("c", "9007199254742209 / 3 (a sum above 2^53)")):
            print(f"{label}")
            print(f"  SPARQL AVG(?w)               : {avg(cur, cls, 'AVG(?w)')}")
            print(f"  SPARQL SUM(?w) / COUNT(?w)   : {avg(cur, cls, 'SUM(?w) / COUNT(?w)')}")
            print(f"  SPARQL IF(COUNT > 0, AVG, MAX) : {avg(cur, cls, 'IF(COUNT(?w) > 0, AVG(?w), MAX(?w))')}")
            cur.execute("SELECT TO_NVARCHAR(AVG(w)) FROM t WHERE cls = ?", (cls,))
            print(f"  SQL    AVG(w) DECIMAL(25,6)  : {cur.fetchone()[0]}")
        print("\n  => the exact means are -188.931034482758620689..., 0.0546875 and 3002399751580736.333...: SQL answers")
        print("     the DECIMAL(25,6) truncation (-188.931034, 0.054687, 3002399751580736.333333); the triple store answers")
        print("     double(SUM) / COUNT (-188.93103448275863, 0.0546875, and 3002399751580736: the sum's low bits lost).")
    finally:
        try:
            cur.execute(f"DROP SCHEMA {S} CASCADE")
        except Exception:  # noqa: BLE001
            pass
        from _conn import sparql

        sparql(cur, f"{P} DROP SILENT GRAPH {G}", update=True)
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
