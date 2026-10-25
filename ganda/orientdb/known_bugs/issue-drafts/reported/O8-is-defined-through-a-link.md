# `IS DEFINED` / `IS NOT DEFINED` on a dotted path in a WHERE is a constant (true / false), never evaluated

**Version:** OrientDB 3.2.56 (`orientdb:latest`), new SQL executor, REST `/command`.

## Reproduction

```sql
CREATE CLASS V1 EXTENDS V;  CREATE PROPERTY V1.id STRING;  CREATE PROPERTY V1.f LONG;
CREATE CLASS E1 EXTENDS E;
CREATE VERTEX V1 SET id = 'a', f = 1;
CREATE VERTEX V1 SET id = 'b';                -- f absent
CREATE VERTEX V1 SET id = 'c', f = null;      -- f present, null
CREATE EDGE E1 FROM (SELECT FROM V1 WHERE id = 'a') TO (SELECT FROM V1);   -- a -> a, a -> b, a -> c

CREATE CLASS D;  CREATE PROPERTY D.id STRING;  CREATE PROPERTY D.l LINK;  CREATE PROPERTY D.m EMBEDDEDMAP;
INSERT INTO D SET id = 'x', l = (SELECT FROM V1 WHERE id = 'b'), m = {'k': 1};
INSERT INTO D SET id = 'y', l = (SELECT FROM V1 WHERE id = 'a'), m = {};
INSERT INTO D SET id = 'z';                   -- no link, no map

-- direct field: correct
SELECT id FROM V1 WHERE f IS DEFINED;                 -- [a, c]
SELECT id FROM V1 WHERE f IS NOT DEFINED;             -- [b]

-- the same test one hop away: every row / no row, regardless of the data
SELECT in.id FROM E1 WHERE in.f IS DEFINED;           -- [a, b, c]  expected [a, c]
SELECT in.id FROM E1 WHERE in.f IS NOT DEFINED;       -- []         expected [b]
SELECT id FROM D WHERE l.f IS DEFINED;                -- [x, y, z]  expected [y]   (z has no link at all)
SELECT id FROM D WHERE l.f IS NOT DEFINED;            -- []         expected [x, z]
SELECT id FROM D WHERE m.k IS DEFINED;                -- [x, y, z]  expected [x]
SELECT id FROM D WHERE m.nope IS DEFINED;             -- [x, y, z]  expected []    (no such key anywhere)

-- every other operator resolves the same path
SELECT in.id FROM E1 WHERE in.f IS NULL;              -- [b, c]     correct
SELECT in.id FROM E1 WHERE in.f > 0;                  -- [a]        correct

-- the same predicate in a bracket filter or a MATCH where: is evaluated
SELECT out('E1')[f IS DEFINED].id FROM V1 WHERE id = 'a';                                   -- [a, c]
MATCH {class: V1, as: x, where: (id = 'a')}.out('E1') {as: y, where: (f IS DEFINED)} RETURN y.id;  -- [a, c]
```

## What is wrong

In a `WHERE` clause, `<path>.<field> IS DEFINED` holds for every record and
`IS NOT DEFINED` for none, whatever the path resolves to: a linked record with the
field, without it, a record with no link, a map without the key, a record with no
map. The right-hand segments are never looked at. The same path under `IS NULL` or
`>` is resolved correctly, and the same `IS DEFINED` test inside a bracket filter or
a MATCH `where:` is evaluated correctly, so the defect is specific to the WHERE
executor's handling of `IS [NOT] DEFINED` over a multi-segment expression.

## Expected

`in.f IS DEFINED` should answer `[a, c]` like the bracket and MATCH forms do, and
`l.f IS NOT DEFINED` should answer the records whose link or field is missing
(`[x, z]`).

## Report Status
Reported as 10883. Confirmed as bug.
