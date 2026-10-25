# O25 -- a bracket filter (`out('e')[s = :p]`, `e1.target[s = :p]`) and a dotted link field (`in.s = :p`) compare a `COLLATE ci` property case-sensitively; the same predicate over the class, in a MATCH `where:` or in a LET subquery is case-insensitive

**Version:** OrientDB 3.2.56. Found while probing case-insensitive collation inside the fuzzer's traversal spellings (`graph:chain`, `graph:bag`, `graph:edgescan`, `doc:path`, `doc:unwind`, `doc:let_expand`, `doc:union`); campaign smoke3 withheld the shape 3 times on its first run.

```sql
CREATE CLASS cv EXTENDS V; CREATE PROPERTY cv.id STRING; CREATE PROPERTY cv.s STRING; ALTER PROPERTY cv.s COLLATE ci;
CREATE CLASS ce EXTENDS E;
CREATE VERTEX cv SET id = 'r0', s = 'a';  CREATE VERTEX cv SET id = 'r4', s = 'AB';
CREATE EDGE ce FROM (SELECT FROM cv WHERE id = 'r0') TO (SELECT FROM cv WHERE id = 'r4');

SELECT id FROM cv WHERE s = 'ab';                                                     -- [r4]      (collated)
MATCH {class: cv, as: a, where: (id = 'r0')}.out('ce') {as: b, where: (s = 'ab')} RETURN b.id;   -- [r4]
SELECT $n.id FROM cv LET $n = (SELECT id FROM cv WHERE id = $parent.$current.id AND s = 'ab') WHERE id = 'r4';   -- [[r4]]
SELECT out('ce')[s = 'ab'].id FROM cv WHERE id = 'r0';                                -- [[]]      expected [[r4]]
SELECT out('ce')[s = 'AB'].id FROM cv WHERE id = 'r0';                                -- [[r4]]
SELECT out.id FROM ce WHERE in.s = 'ab';                                              -- []        expected [r0]
SELECT out('ce')[s LIKE 'a%'].id FROM cv WHERE id = 'r0';                             -- [[r4]]    (LIKE is case-insensitive everywhere)
```

The bracket filter and the dotted field evaluate the comparison on the plain value without resolving the property's collation, so the same question has two answers depending on where the predicate is written. The fuzzer withholds the bracket and dotted spellings when a target predicate (or, for the edge-class scan, the source predicate) holds a `=` / `<>` / `<` / `<=` / `>` / `>=` leaf on a ci field (`guards.o25_shape`, `spellings.BRACKET`).

**Projected face (campaign c100, seed 670):** a ci value that reaches the comparison through a projection loses the collation too: `SELECT id FROM (SELECT id, s FROM cv) WHERE s = 'A'` answers `[r1]` where `SELECT id FROM (SELECT FROM cv) WHERE s = 'A'` (whole records projected) and the plain scan answer `[r0, r1]`. The fuzzer's hoisted forms, which lift the source predicate over such a projection, are not emitted for a ci comparison (`spellings.hoistable`).
