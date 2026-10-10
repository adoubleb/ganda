# H4 -- SQL over Document Store collections: SUM over a join that matches no row fails with a numeric overflow

**Version:** SAP HANA Cloud, `SYS.M_DATABASE.VERSION` = `4.00.000.00.1788859455` (2026-10), JSON
Document Store (collections) queried through SQL.
**Client:** `hdbcli`, plain SQL (`cursor.execute`).
**Found by:** the Ganda SAP HANA fuzzer's 12-hour campaign `campaign-20261009-165805`, as 4 error
asymmetries on `doc` traversal sums (`proj=esum`): the Document Store variant failed with `[314]
numeric overflow` while the column-table variant answered -- in two of the four, NULL for every
start, because no path exists at all.

## Summary

A `SUM` over a join of collection views fails with `[314] numeric overflow: SQL Error` although the
join matches **no row** (the correct answer is NULL). The only values that can overflow are those of
the rows the join excludes: two links whose values add up past `BIGINT` (`2^63 - 1` and `1`). The same
query over column tables holding the same data answers NULL, and so does the same query summing
`TO_DECIMAL(...)`.

## Steps to reproduce

```sql
CREATE COLLECTION st;
CREATE COLLECTION mid;
INSERT INTO st  VALUES('{"id": "s1", "e1": []}');                       -- a start with no link
INSERT INTO mid VALUES('{"id": "m1", "e2": [{"eid": "x1", "to": "t1", "w": 9223372036854775807},
                                            {"eid": "x2", "to": "t1", "w": 1}]}');

WITH v_mid AS (SELECT $."id" AS id FROM mid),
     v_e1  AS (SELECT $."id" AS src_id, "l"."to" AS dst_id FROM st UNNEST $."e1" AS "l"),
     raw_v_e2 AS (SELECT "l"."eid" AS eid, $."id" AS src_id, "l"."to" AS dst_id, "l"."w" AS w
                  FROM mid UNNEST $."e2" AS "l"),
     v_e2  AS (SELECT eid, src_id, dst_id, TO_BIGINT(w) AS w FROM raw_v_e2)

-- (1) the join's rows: none (st's only document has no link)
SELECT j1.src_id AS strt, j2.w AS v
FROM v_e1 j1 JOIN v_mid t1 ON t1.id = j1.dst_id JOIN v_e2 j2 ON j2.src_id = t1.id;   -- []

-- (2) the SUM over those rows: an overflow, not NULL
SELECT SUM(p.v) FROM (<query (1)>) p;
--   [314] numeric overflow: SQL Error
```

## Expected vs actual

- **Expected:** query (2) answers `NULL`: a `SUM` over no row. The same query over column tables
  holding the same data (`r_mid`, `r_e1`, `r_e2` with a `BIGINT` column `w`) answers `NULL`.
- **Actual:** query (2) aborts with `[314] numeric overflow: SQL Error`.

## Characterisation (each bullet an isolated trial, `H4_repro.py` and the Ganda probes)

- The join matches no row (query (1) is empty), so the overflow comes from adding up values the join
  excludes; `2^63 - 1 + 1` is the only sum in the data that leaves `BIGINT`.
- `SUM(TO_DECIMAL(p.v))` over the same join answers NULL.
- It needs the link view to read more than the value: with `raw_v_e2` reading the value column alone
  (`$."id"`, `"l"."w"`), the same SUM answers NULL; with the link's id and its far end read as well
  (`"l"."eid"`, `"l"."to"`), it overflows. An unused extra view (`v_st`) or the link id in `v_e1` make
  no difference.
- The value is cast in a CTE of its own over the raw collection read (`TO_BIGINT(w)` over
  `raw_v_e2`), the way an application types a JSON field once for several queries.
- The per-group form overflows too: `SELECT s.id, SUM(p.v) FROM v_st s LEFT JOIN (<query (1)>) p ON
  p.strt = s.id GROUP BY s.id`, where every group is empty.
- In the campaign's four bundles the same holds on generated data: replaying a bundle's own setup,
  `COUNT(p.v)` per start is 0 for every start (seed 33) or answers the real path counts (seed 17,
  whose true sums are at most 496), and `SUM(TO_DECIMAL(p.v))` answers the column tables' result,
  while `SUM(p.v)` overflows. In every case the summed link type holds values whose absolute total
  passes `2^63` on links that no path reaches.

## Impact

An aggregate over embedded arrays joined to other documents -- "for each customer, the total of the
amounts on the order lines of their orders" -- fails with an overflow error whenever the store holds,
anywhere in the joined collections, values whose total exceeds `BIGINT`, even if none of them belongs
to the answer, and even if the answer is empty. Whether it fails depends on which fields the views
read, so a harmless change to a view's column list can turn a working query into a failing one.

## Workaround

Sum as `DECIMAL` (`SUM(TO_DECIMAL(x))`), or read the value column alone in the view feeding the
aggregate. The Ganda HANA provider withholds the doc variants of a traversal sum whose summed column
holds an absolute total of `2^62` or more (`known_bugs.py`, H4).

## Repro script

`H4_repro.py` in this folder is a self-contained reproduction (needs only `hdbcli`, connects from
`HANA_*`, works in a scratch schema it drops); `H4_repro_output.txt` is its transcript against HANA
Cloud 4.00.000.00.
