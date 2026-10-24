# A bracket filter after a two-hop link path matches nothing (the filter runs on the nested list, not its elements)

**Version:** OrientDB 3.2.56 (`orientdb:latest`), new SQL executor, REST `/command`.

## Reproduction

```sql
CREATE CLASS D;  CREATE PROPERTY D.id STRING;  CREATE PROPERTY D.f LONG;  CREATE PROPERTY D.n LINKLIST;
INSERT INTO D SET id = 'c', f = 2;
INSERT INTO D SET id = 'd', f = 3;
INSERT INTO D SET id = 'b', f = 1, n = (SELECT FROM D WHERE id IN ['c', 'd']);
INSERT INTO D SET id = 'a', f = 0, n = (SELECT FROM D WHERE id = 'b');       -- a -> b -> {c, d}

-- one hop: the filter works
SELECT n[f > 0].id      FROM D WHERE id = 'a';     -- [b]

-- two hops: field access maps through, the filter does not
SELECT n.n.id           FROM D WHERE id = 'a';     -- [[c, d]]
SELECT n.n.f            FROM D WHERE id = 'a';     -- [[2, 3]]
SELECT n.n[f > 2].id    FROM D WHERE id = 'a';     -- []        expected [d]
SELECT n.n[f = 2].id    FROM D WHERE id = 'a';     -- []        expected [c]
SELECT n.n[f > -1].id   FROM D WHERE id = 'a';     -- []        expected [c, d]   (every record satisfies it)
SELECT n.n[id = 'd'].id FROM D WHERE id = 'a';     -- []        expected [d]

-- why: the path result is a list of one list, and the filter is applied to that outer list
SELECT n.n.size()       FROM D WHERE id = 'a';     -- 1
SELECT n.n[0].id        FROM D WHERE id = 'a';     -- [c, d]
```

The graph form behaves the same. The raw edge bag `out_E1` and its target link
`in` nest like the LINKLIST above; the `out()` function flattens and the filter works:

```sql
CREATE CLASS V1 EXTENDS V;  CREATE PROPERTY V1.id STRING;  CREATE PROPERTY V1.f LONG;
CREATE CLASS E1 EXTENDS E;
CREATE VERTEX V1 SET id = 'a', f = 0;   CREATE VERTEX V1 SET id = 'b', f = 1;
CREATE VERTEX V1 SET id = 'c', f = 2;   CREATE VERTEX V1 SET id = 'd', f = 3;
CREATE EDGE E1 FROM (SELECT FROM V1 WHERE id = 'a') TO (SELECT FROM V1 WHERE id = 'b');
CREATE EDGE E1 FROM (SELECT FROM V1 WHERE id = 'b') TO (SELECT FROM V1 WHERE id IN ['c', 'd']);   -- a -> b -> {c, d}

SELECT out_E1.in[f > 0].id            FROM V1 WHERE id = 'a';   -- [b]        one hop: works
SELECT out_E1.in.out_E1.in.f          FROM V1 WHERE id = 'a';   -- [[2, 3]]
SELECT out_E1.in.out_E1.in[f > 2].id  FROM V1 WHERE id = 'a';   -- []         expected [d]
SELECT out('E1').out('E1').f          FROM V1 WHERE id = 'a';   -- [2, 3]     flattened
SELECT out('E1').out('E1')[f > 2].id  FROM V1 WHERE id = 'a';   -- [d]        works
```

## What is wrong

Traversing a multi-valued link twice produces a nested list. Dotted field access
is mapped through every level of that nesting (`n.n.f` -> `[[2, 3]]`), but a bracket
filter is applied only to the outermost level, whose elements are lists rather
than records. No list satisfies `f > 2` (or `f > -1`), so the filter drops
everything, silently. The two operators disagree about what the path denotes,
and the result is an empty answer instead of an error.

## Expected

A bracket filter should be applied at the same depth as field access, so that
`n.n[f > 2].id` answers `[[d]]` (or `[d]`, matching what `out().out()[...]` does),
never `[]` for a predicate that some reachable record satisfies.

## Report Status
Reported as 10884. Confirmed.