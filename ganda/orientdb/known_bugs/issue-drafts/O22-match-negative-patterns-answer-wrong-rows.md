# O22 -- a MATCH negative (`NOT`) pattern answers nothing for the records that lack the path, and repeats one record when the negated pattern can match nobody

**Version:** OrientDB 3.2.56. Found while probing the documented negative-pattern form for a `nexists` spelling of the fuzzer's traversal questions; the spelling was not added because every form answers wrongly.

```sql
CREATE CLASS gv EXTENDS V; CREATE PROPERTY gv.id STRING; CREATE PROPERTY gv.f LONG; CREATE CLASS ge EXTENDS E;
CREATE VERTEX gv SET id = 'a', f = 1; CREATE VERTEX gv SET id = 'b', f = 2; CREATE VERTEX gv SET id = 'c'; CREATE VERTEX gv SET id = 'd', f = 4;
CREATE EDGE ge FROM (SELECT FROM gv WHERE id = 'a') TO (SELECT FROM gv WHERE id = 'b');
CREATE EDGE ge FROM (SELECT FROM gv WHERE id = 'b') TO (SELECT FROM gv WHERE id = 'c');
CREATE EDGE ge FROM (SELECT FROM gv WHERE id = 'd') TO (SELECT FROM gv WHERE id = 'a');

SELECT id FROM gv WHERE out('ge').size() = 0;                                              -- [c]
MATCH {class: gv, as: a}, NOT {as: a}.out('ge') {as: b} RETURN a.id;                       -- []       expected [c]
MATCH {class: gv, as: a}, NOT {as: a}.out('ge') {class: gv} RETURN a.id;                   -- []
MATCH {class: gv, as: a}, {class: gv, as: b}, NOT {as: a}.out('ge') {as: b} RETURN a.id, b.id;   -- []   expected the 13 pairs without an edge
MATCH {class: gv, as: a}, NOT {as: a}.out('ge') {as: b, where: (f = 100)} RETURN a.id;    -- [a, a, a, a]   expected [a, b, c, d]
MATCH {class: gv, as: a}, NOT {as: a}.in('ge') {as: b} RETURN a.id;                        -- []       expected [d]
```

With an unbound target alias the negative pattern excludes every record; with a bound one it excludes every pair; with a `where:` that no record satisfies it returns the first record once per record of the class. The equivalent `out('ge').size() = 0`, the anti-join `id NOT IN (SELECT out.id FROM ge)` and `MATCH ... {optional: true}` with a null test are all correct.
