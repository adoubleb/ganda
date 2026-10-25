# F28: an index over an array field answers a whole-array equality with nothing

**Version.** SurrealDB 3.3.4 (`surrealdb/surrealdb:v3.3.4`, memory backend, over `/sql`).
Found 2026-10-26 by Ganda's first SurrealDB smoke campaign (seed 1: a `distinct` question
and a write on class `n5`, whose doc data model alone held an index on the LIST column
`f4`; the doc data model answered no record for `f4 = [1]`, graph and rel two).

**Reproduction.**

```sql
DEFINE TABLE t SCHEMALESS;
DEFINE INDEX ix_t_f4 ON TABLE t FIELDS f4;
CREATE t:a SET f4 = [1];
CREATE t:b SET f4 = [1, 2];
CREATE t:c SET f4 = [];
SELECT VALUE id FROM t WHERE f4 = [1];            -- []        expected ["t:a"]
SELECT VALUE id FROM t WHERE f4 = [1, 2];         -- []        expected ["t:b"]
SELECT VALUE id FROM t WHERE f4 = [];             -- []        expected ["t:c"]
SELECT VALUE id FROM t WHERE f4 IN [[1], [2]];    -- []        expected ["t:a"]
SELECT VALUE id FROM t WHERE [1] = f4;            -- []        expected ["t:a"]
SELECT VALUE id FROM t WHERE f4 != [1];           -- t:b, t:c  (a scan: right)
SELECT VALUE id FROM t WHERE f4 CONTAINS 1;       -- t:a, t:b  (a scan: right)
SELECT VALUE id FROM t WHERE f4 = [1] EXPLAIN;    -- IndexScan ix_t_f4, access "= [1]"
```

Without the index every equality answers the right records.

A second face (campaigns ref12 seed 1008 and off24 seeds 2015 / 2023): a presence test
served by the same index answers a record ONCE PER ELEMENT:

```sql
SELECT VALUE id FROM t WHERE f4 IS NOT NONE;     -- t:a, t:b, t:b, t:c  (t:b twice: two elements)
SELECT VALUE id FROM t WHERE f4 IS NOT NONE AND f4 IS NOT NULL;   -- the same duplicates
```

**Analysis.** An index over an array field stores one key per ELEMENT (which is what
serves `CONTAINS`; `EXPLAIN` shows the element-keyed `IndexScan` for `e1.w CONTAINS 2`).
The planner nevertheless routes a whole-array equality `f4 = [1]` (and `IN` over arrays,
as a `UnionIndexScan`) to that index with the array itself as the key, which no entry
holds. The scan path compares the stored array with the constant and is right.

**Scope.** Every table with an index on an array-valued field; equality and `IN` with an
array constant. The analogue of ArangoDB's A25 (an `IN` of arrays served by an expanded
array index).

**How Ganda met it.** The oracle compared the three data models of one instance; the data
model holding the index on the LIST column answered an empty set where the other two
answered the matching records, in a read and then in a write's `WHERE id IN (SELECT ...)`
(whose stored state then differed on that data model); campaign c8 (seed 103) met it
again through reversed operands (`$p = f1`). Guarded in `known_bugs/predicate.py`
(`f28_predicate`): a `=` / `IN` leaf with an array constant, either operand order, or a
presence test (`IS NOT NULL`, `IS PRESENT`), on a LIST column an index of the data model
serves; pinned by probe checks C40 and C45.
