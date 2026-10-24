# O5 -- raw Java exceptions over HTTP for unsupported-but-parseable shapes

**Version:** OrientDB 3.2.56. Three shapes the parser accepts and the executor answers with a bare Java exception (HTTP 500/501, no OrientDB error class):

```sql
SELECT e1.w.sum() FROM d1;                      -- 501 java.lang.UnsupportedOperationException: OMethod call, something missing in the implementation...?
SELECT e1.target.e1.target.id.flatten() FROM d1;  -- 501 (same)
SELECT x AS v LET $x = first((SELECT FROM n1 WHERE id = 'a'));  -- 501 java.lang.UnsupportedOperationException: Execution of select in parentheses is not supported
SELECT src_id, max($f.v) FROM j1 LET $f = (SELECT f1 AS v FROM n1 WHERE id = $parent.$current.dst_id) GROUP BY src_id;
                                                -- 500 java.lang.ClassCastException: java.util.ArrayList cannot be cast to java.lang.Comparable
SELECT f3.asInteger() FROM n1;  -- 500 java.lang.NumberFormatException: For input string: "s0"   (f3 holds a non-numeric string)
```

None of these is a wrong answer; they are catalogued so the fuzzer's harness can classify them (`harness.classify_error`, the `internal` flag) and so the spellings avoid them by construction. The `/batch` endpoint also turns a map-valued positional parameter into its `toString()` (`{x=1}`) and rejects a named-parameter map with `ClassCastException: java.util.LinkedHashMap cannot be cast to [Ljava.lang.Object;`.


**Further face (probed 3.2.56):** `SELECT FROM index:<a NOTUNIQUE_HASH_INDEX> WHERE key BETWEEN :a AND :b` is an HTTP 501 `java.lang.UnsupportedOperationException: iterateEntriesBetween` (the same query on the class scans correctly); the fuzzer's `index:` door never emits a BETWEEN on a hash kind.

**List-index face (campaign c100d, seed 1022).** `CONTAINSANY` on an `EMBEDDEDLIST LONG` property that a HASH or DICTIONARY index covers is planned through the index and then refused -- a 501 instead of the scan an SB-tree kind falls back to correctly:

```sql
CREATE CLASS l1; CREATE PROPERTY l1.f EMBEDDEDLIST LONG; INSERT INTO l1 SET f = [1, 2];
CREATE INDEX l1_f ON l1 (f) NOTUNIQUE_HASH_INDEX;
SELECT FROM l1 WHERE f CONTAINSANY [1];        -- java.lang.UnsupportedOperationException: Cannot evaluate f CONTAINSANY [1] on index l1_f   (DICTIONARY, UNIQUE_HASH_INDEX alike)
SELECT FROM l1 WHERE f CONTAINS 1;             -- [1, 2]   (a scan)
```

**List-comparison face (the select IR's poly join, benchmark seed 2).** A stored LIST compared through `<`, `<=`, `>` or `>=` with a scalar is a `ClassCastException`. The same comparison with the operands' types swapped answers false:

```sql
CREATE CLASS B0 ABSTRACT; CREATE PROPERTY B0.id STRING;
CREATE CLASS a1 EXTENDS B0; CREATE PROPERTY a1.f DOUBLE;
CREATE CLASS a2 EXTENDS B0; CREATE PROPERTY a2.f EMBEDDEDLIST LONG;
INSERT INTO a1 SET id = 'x', f = 1.5; INSERT INTO a2 SET id = 'l', f = [1, 2];

SELECT id FROM a2 WHERE f <= 2.5;          -- java.lang.ClassCastException: java.util.ArrayList cannot be cast to java.lang.Comparable
SELECT id FROM a1 WHERE f <= [1, 2];       -- []   (a scalar field against a list)
SELECT id FROM a2 WHERE f = 2;             -- []
SELECT id, $m.size() FROM B0 LET $m = (SELECT id FROM a1 WHERE $parent.$current.f >= f) WHERE @class = 'a1';
                                           -- ClassCastException: the LET runs for every B0 record, a2's included, before the WHERE
SELECT id, $m.size() FROM B0 LET $m = (SELECT id FROM a1 WHERE f <= $parent.$current.f) WHERE @class = 'a1';
                                           -- right: a Double (or a Long) on the left answers false against the list
```

With a DECIMAL column on the left the forward form throws too (`java.util.ArrayList cannot be cast to java.math.BigDecimal`, seed 2).

**Mixed-type face (the same benchmark, seed 3).** Two declared properties of different types compared field against field. Probed on one record with every pair of types:

| left | right | `=` | `<` |
|---|---|---|---|
| DECIMAL | LONG | ClassCastException (`Long cannot be cast to BigDecimal`) | ClassCastException |
| DECIMAL | INTEGER, FLOAT, DOUBLE, STRING | answers | answers |
| DECIMAL | DATETIME, BOOLEAN | answers | ClassCastException |
| DATETIME | BOOLEAN (and the reverse) | answers | ClassCastException |
| LIST | a scalar | -- | ClassCastException |
| any other pair | | answers (converting the right operand to the left one's type, O32) | answers |

```sql
CREATE CLASS x; CREATE PROPERTY x.d DECIMAL; CREATE PROPERTY x.l LONG; CREATE PROPERTY x.i INTEGER;
INSERT INTO x SET d = 2, l = 2, i = 2;
SELECT count(*) FROM x WHERE d = i;   -- 1
SELECT count(*) FROM x WHERE d = l;   -- java.lang.ClassCastException: java.lang.Long cannot be cast to java.math.BigDecimal
SELECT count(*) FROM x WHERE l = d;   -- 1
```

The select IR's base-class join reaches this through a sibling's same-named property. `SELECT id, $m.id FROM N0 LET $m = (SELECT id FROM n2 WHERE $parent.$current.f1 = f1) WHERE @class = 'n2'` throws where the sibling `n1.f1` is a DECIMAL and `n2.f1` a LONG. **Fuzzer handling:** `guards.mixed_compare_throws` holds the matrix; `o5_poly_join` applies it with the sibling's value on the right in the forward form and on the left under `jrev`.

A base-class query's `WHERE @class = :cls AND (f <= :p)` never reaches the sibling record, because the AND stops at the class test. A LET, which precedes the WHERE, does reach it. **Fuzzer handling:** `guards.o5_poly_join` withholds a select-IR `join` over the base class (`poly` / `polyin`) wherever the correlation would compare a sibling class's same-named property of another type through a throwing pair (`mixed_compare_throws`, below). Fact: `scripts/probe_osql.py`, "the IR's parity with the node families".


## The cov400 faces (2026-09-28): sibling classes through an index plan, and a hash map index

- **A subclass query served by a base-class index evaluates the siblings' records** (seed 5092). The plan is `FETCH FROM INDEX ix_N0_b1` over every subclass's clusters, then `FILTER ITEMS WHERE`, and only then `FILTER ITEMS BY CLASS n1`. A `BETWEEN` under an OR then meets a sibling's same-named field of another type. `SELECT FROM n1 WHERE b1 = false AND (b1 = true OR f1 BETWEEN '1978-07-01' AND '2000-02-28')`, with `n1.f1` a DATE and sibling `n2.f1` a BOOLEAN, is `IllegalArgumentException: Value is not boolean`. The same BETWEEN as a top-level conjunct answers, and so does the query with no base index or no sibling. **Fuzzer handling:** `guards.o5_sibling_filter`, in `node_case_known` (so the select IR's class targets, a semi-join's inner query, the node families and the write IR's filters).
- **A base-class query converts the pivot through a sibling's index** (seed 5272, the graph side only). `SELECT id FROM N0 WHERE @class = 'n2' AND f2 = '2024-08-18 08:18:40'` is planned over both subclasses' `f2` indexes when both hold one. `n1`'s index is on an `EMBEDDEDLIST LONG`, so the date string becomes a Long key: `NumberFormatException`. **Fuzzer handling:** `guards.o5_poly_sibling_index` (conservative: any sibling index on the field under a type the pivot does not convert to), on the select IR's and the node families' `poly` / `polyin` sides.
- **`m CONTAINSVALUE :p` through a `BY VALUE` map index of a hash or dictionary kind** (seed 5372: `NOTUNIQUE_HASH_INDEX`) is a 501, `UnsupportedOperationException: Cannot evaluate m CONTAINSVALUE :p on index`, for every pivot. This covers all five kinds but the SB-tree `NOTUNIQUE` / `UNIQUE`. `CONTAINSKEY` answers through every kind. **Fuzzer handling:** `guards.map_value_index_known`.

Facts: `scripts/probe_osql.py`, "the cov400 triage".
