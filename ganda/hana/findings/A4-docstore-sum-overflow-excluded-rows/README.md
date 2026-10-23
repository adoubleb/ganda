# A4 / H4 — Document Store SQL: SUM over a join that matches no row overflows

**Bucket:** A (candidate HANA bug — spurious error). **Version:** HANA Cloud 4.00.000.00.
**Repro:** `python3 -I repro.py`

## Claim
A `SUM` over a join of collection views fails with `[314] numeric overflow` although the join
matches no row (the answer is NULL): it adds up values of links the join excludes (`2^63 - 1` and
`1`). The same query over column tables, or summing `TO_DECIMAL(...)`, answers NULL.

## Steps to reproduce
Plain SQL, in a scratch schema.

1. Create the schema and two collections:
   ```sql
   CREATE SCHEMA A4_REPRO;
   SET SCHEMA A4_REPRO;
   CREATE COLLECTION st;
   CREATE COLLECTION mid;
   ```
2. A start document with NO link, and a middle document whose two links carry `2^63 - 1` and `1`:
   ```sql
   INSERT INTO st VALUES('{"id": "s1", "e1": []}');
   INSERT INTO mid VALUES('{"id": "m1", "e2": [{"eid": "x1", "to": "t1", "w": 9223372036854775807}, {"eid": "x2", "to": "t1", "w": 1}]}');
   ```
3. The join's rows -> `[]` (it matches nothing):
   ```sql
   WITH v_mid AS (SELECT $."id" AS id FROM mid),
        v_e1 AS (SELECT $."id" AS src_id, "l"."to" AS dst_id FROM st UNNEST $."e1" AS "l"),
        raw_v_e2 AS (SELECT "l"."eid" AS eid, $."id" AS src_id, "l"."to" AS dst_id, "l"."w" AS w FROM mid UNNEST $."e2" AS "l"),
        v_e2 AS (SELECT eid, src_id, dst_id, TO_BIGINT(w) AS w FROM raw_v_e2)
   SELECT j1.src_id AS strt, j2.w AS v
   FROM v_e1 j1 JOIN v_mid t1 ON t1.id = j1.dst_id JOIN v_e2 j2 ON j2.src_id = t1.id;
   ```
4. The same `WITH`, summing those rows -> **`[314] numeric overflow`** (bug; expected NULL):
   ```sql
   WITH ... (as in step 3)
   SELECT SUM(p.v) FROM (
     SELECT j1.src_id AS strt, j2.w AS v
     FROM v_e1 j1 JOIN v_mid t1 ON t1.id = j1.dst_id JOIN v_e2 j2 ON j2.src_id = t1.id) p;
   ```
5. Control: the same data and query over column tables -> NULL:
   ```sql
   CREATE COLUMN TABLE r_mid (id NVARCHAR(10));
   INSERT INTO r_mid VALUES ('m1');
   CREATE COLUMN TABLE r_e1 (src_id NVARCHAR(10), dst_id NVARCHAR(10));
   CREATE COLUMN TABLE r_e2 (eid NVARCHAR(10), src_id NVARCHAR(10), dst_id NVARCHAR(10), w BIGINT);
   INSERT INTO r_e2 VALUES ('x1', 'm1', 't1', 9223372036854775807);
   INSERT INTO r_e2 VALUES ('x2', 'm1', 't1', 1);
   SELECT SUM(p.v) FROM (
     SELECT j1.src_id AS strt, j2.w AS v
     FROM r_e1 j1 JOIN r_mid t1 ON t1.id = j1.dst_id JOIN r_e2 j2 ON j2.src_id = t1.id) p;
   ```
6. Clean up: `DROP SCHEMA A4_REPRO CASCADE;`

Reproduced on 4.00.000.00.1788856605 (2026-10-23).

## Trigger
The link view reads the link whole (its id and far end as well as its value), and the value is cast
in a CTE of its own over the raw collection read. With the value column read alone, it answers NULL.

## Expected
`NULL`, as the column tables answer.

## Actual (from repro.py)
```
collections: the join's rows       : []
collections: SUM over the join     : [314] numeric overflow: SQL Error
collections: SUM(TO_DECIMAL(...))  : [(None,)]
collections: link view, value only : [(None,)]
column tables: SUM over the join   : [(None,)]
```

## How to judge
An overflow for a SUM over no row cannot come from the answer; it is a plan that aggregates before
(or regardless of) the join. Whether it fires depends on which columns a view reads.

## What the provider does
Withholds the doc variants of a traversal sum whose summed column holds an absolute total of `2^62`
or more (`known_bugs.py`, H4); the rel and rdf variants still answer.
