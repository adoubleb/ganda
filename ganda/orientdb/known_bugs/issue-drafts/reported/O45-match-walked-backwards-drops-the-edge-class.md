# MATCH `outE('X')` also returns edges of other classes when the target vertex class has fewer records

**OrientDB version:** 3.2.57 (official Docker image `orientdb:3.2.57`; also seen on 3.2.56)
**Java version:** OpenJDK 1.8.0_504
**OS:** Ubuntu 26.04 (container)
**Found by:** the LLM-card smoke `cards-round2-localsmoke` (seeds 9000 / 9001: 7 of 29 bundles -- cards `aggre-round2-0487`, `funct-round2-0306`, `group-round2-0488`, `order-round2-0452`, `predi-round2-0502`, `trave-round2-0151`); pinned in `scripts/probe_osql.py` ("O45"). Guarded for the cards in `fuzzer/cards/known.py` (`_o45_match`); the fuzzer's own MATCH spellings step with `out('e')`, which keeps the class.

## Steps to reproduce

On an empty database:

```sql
CREATE CLASS A EXTENDS V;
CREATE CLASS B EXTENDS V;
CREATE CLASS X EXTENDS E;
CREATE CLASS Y EXTENDS E;
CREATE VERTEX A SET name = 'a1';
CREATE VERTEX A SET name = 'a2';   -- A now holds more records than B
CREATE VERTEX B SET name = 'b1';
-- a1 -> b1 twice: once through an X edge, once through a Y edge
CREATE EDGE X FROM (SELECT FROM A WHERE name = 'a1') TO (SELECT FROM B WHERE name = 'b1');
CREATE EDGE Y FROM (SELECT FROM A WHERE name = 'a1') TO (SELECT FROM B WHERE name = 'b1');

-- e is bound through outE('X'), so it can only be the X edge
MATCH {class: A, as: a}.outE('X'){as: e}.inV(){class: B, as: b} RETURN e.@class AS cls;   -- X, Y   (Wrong: expected X)
MATCH {class: A, as: a}.outE('X'){as: e}.inV(){as: b} RETURN e.@class AS cls;            -- X      (no class on b)
```

## Expected behavior

Both queries return one row, `cls = X`: the only `X` edge.

## Actual behavior

The first query returns two rows, `X` and `Y`. The second query (no `class:` on `b`) returns only `X`.

With as many `A` records as `B` records (leave out `a2`), the first query also returns only `X`.

## Why this is a bug

`outE('X')` must bind only edges of class `X`. Here the `Y` edge is returned under the alias `e`, and whether that happens depends on how many records each endpoint class holds, not on the query.
