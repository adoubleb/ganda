# O1 -- `$parent.$current` is null inside a correlated subquery when the enclosing query is served by an index

**Version:** OrientDB 3.2.56 (docker `orientdb:latest`, 2026-09), REST `/command/<db>/sql`, new SQL executor.
**Found by:** the Ganda OrientDB fuzzer's first probe of the relational representation (`scripts/probe_osql.py`, section "correlated subqueries"); it is the reason the `rel:let` spelling is withheld at this shape (`fuzzer/guards.py`, `o1_shape`).

## Repro

```sql
CREATE CLASS n1; CREATE PROPERTY n1.id STRING; CREATE PROPERTY n1.f1 LONG;
CREATE INDEX n1_id ON n1 (id) UNIQUE;
INSERT INTO n1 SET id = 'n1/r0', f1 = 0;
INSERT INTO n1 SET id = 'n1/r1', f1 = 1;
CREATE CLASS j1; CREATE PROPERTY j1.src_id STRING; CREATE PROPERTY j1.dst_id STRING;
INSERT INTO j1 SET src_id = 'n1/r0', dst_id = 'n1/r1';

-- class scan: correct
SELECT id, $h.v AS v FROM n1 LET $h = (SELECT dst_id AS v FROM j1 WHERE src_id = $parent.$current.id);
-- [{id: n1/r0, v: [n1/r1]}, {id: n1/r1, v: []}]

-- the same LET with the outer query served by the id index: every subquery sees a null parent
SELECT id, $h.v AS v FROM n1 LET $h = (SELECT dst_id AS v FROM j1 WHERE src_id = $parent.$current.id) WHERE id = 'n1/r0';
-- [{id: n1/r0, v: []}]          <- expected [n1/r1]
SELECT id, $h.v AS v FROM n1 LET $h = (SELECT dst_id AS v FROM j1 WHERE src_id = $parent.$current.id) ORDER BY id;
-- [{id: n1/r0, v: []}, {id: n1/r1, v: []}]
SELECT id, (SELECT $parent.$current.id AS x FROM n1 LIMIT 1) AS v FROM n1 ORDER BY id;
-- x is null on every row
```

## Characterisation (each bullet an isolated trial)

- Triggers whenever the enclosing query's target is fetched through an index: `WHERE <indexed> = / IN`, `ORDER BY <indexed>`, `WHERE x IN (SELECT ...)` on an indexed column. Every index kind (UNIQUE, UNIQUE_HASH_INDEX, NOTUNIQUE, NOTUNIQUE_HASH_INDEX).
- Does NOT trigger for a class scan, `WHERE @rid = #x:y`, `FROM #x:y`, `FROM (SELECT FROM n1 ORDER BY id)` (wrapping the indexed query in a subquery is a workaround), or `GROUP BY <indexed>`.
- Both the projection-subquery form `(SELECT ... WHERE x = $parent.$current.id) AS v` and the `LET` form are affected.
- `EXPLAIN` shows `FETCH FROM INDEX VALUES ASC n1_id` / `FETCH FROM INDEX n1_f1` followed by `LET (for each record)`; the LET runs but its `$parent` context is empty.

## Impact

Any correlated subquery over an indexed outer query silently returns the wrong (empty) result -- the typical "children of each parent" query, as soon as the parent lookup is indexed. No error, no warning.

## Report Status
Reported as 10879. Confirmed as bug.