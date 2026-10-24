# `=` on a BINARY property matches nothing on a scan but matches through an index

**OrientDB version:** 3.2.57 (official Docker image `orientdb:latest`, pulled 2026-10-07). The
comparison code is the same on the `3.2.x` branch head (`OQueryOperatorEquals.equals`, last
changed 2026-09-01) and on `develop` (where it moved into `OEqualsCompareOperator.equals` on
2026-08-19 with the same `byte[]` branch), so the fix is needed on both.
**Java version:** OpenJDK 1.8.0_504 (Temurin, the image's)
**Client:** REST, `POST /command/<db>/sql`, the new SQL executor; a literal or a named parameter.

## Steps to reproduce

```sql
CREATE CLASS t;
CREATE PROPERTY t.id STRING;
CREATE PROPERTY t.b BINARY;
CREATE PROPERTY t.b2 BINARY;
-- the bytes 00 01 02 'hello', as the base64 text a BINARY travels as in JSON (and reads back as)
INSERT INTO t SET id = 'r0', b = 'AAECaGVsbG8=', b2 = 'AAECaGVsbG8=';
INSERT INTO t SET id = 'r1', b = 'b3RoZXI=',     b2 = 'AAECaGVsbG8=';

SELECT id FROM t WHERE b = 'AAECaGVsbG8=';            -- no rows
SELECT id FROM t WHERE b IN ['AAECaGVsbG8='];         -- no rows
SELECT id FROM t WHERE b <> 'AAECaGVsbG8=';           -- r0, r1  (every row, r0 included)
SELECT id FROM t WHERE b = b2;                        -- r0      (two BINARY operands compare fine)

CREATE INDEX t_b ON t (b) NOTUNIQUE;
SELECT id FROM t WHERE b = 'AAECaGVsbG8=';            -- r0      (EXPLAIN: FETCH FROM INDEX t_b)
SELECT id FROM t WHERE b IN ['AAECaGVsbG8='];         -- r0
SELECT id FROM (SELECT FROM t) WHERE b = 'AAECaGVsbG8=';   -- no rows (the subquery target forces a scan)

UPDATE t SET id = id WHERE b = 'AAECaGVsbG8=';        -- 1 record with the index, 0 without it
DELETE FROM t WHERE b = 'AAECaGVsbG8=';               -- deletes r0 with the index, nothing without it
```

The same text through a named parameter (`b = :p` with `{"p": "AAECaGVsbG8="}`) behaves the same
way, as does the `'_AAECaGVsbG8=_'` form, a `LET $x = :p ... WHERE b = $x`, a `MATCH ... where: (b
= :p)` and an `IN (SELECT ...)` subquery. The answer depends on the plan alone: with a
`NOTUNIQUE_HASH_INDEX` on `b` and a `UNIQUE` index on `id`, `WHERE id = 'r0' AND b = 'AAECaGVsbG8='`
is planned through the `id` index and answers no rows, while with a `NOTUNIQUE` index on `b` the
planner reads `t_b` and answers `r0`.

## Expected behavior

A BINARY value equals the text it was written from. The engine itself converts that text to the
stored bytes on write, converts it to the index key (which is why the index finds the record, and
why a UNIQUE index on `b` rejects a second `'AAECaGVsbG8='`), and emits it on read. `WHERE b =
'AAECaGVsbG8='` must return `r0` whatever indexes exist, and `UPDATE` / `DELETE` must touch the same
records with or without an index.

## Actual behavior

On a class scan (and in any `FilterStep`) `b = <text>` is false for every record, `b IN [<text>]`
is false and `b <> <text>` is true for every record, so a record's own value does not equal itself.
Through an index lookup on `b` (NOTUNIQUE, UNIQUE, NOTUNIQUE_HASH_INDEX, UNIQUE_HASH_INDEX) the
same predicate is answered correctly. `UPDATE ... WHERE b = <text>` and `DELETE ... WHERE b =
<text>` therefore change data only when an index on `b` exists.

## Analysis

The new executor's `OEqualsCompareOperator.execute(left, right)` calls the untyped
`OQueryOperatorEquals.equals(Object iLeft, Object iRight)`. With a stored `byte[]` on the left and
the `String` on the right it reaches the general case:

```java
// ALL OTHER CASES
final Object right = OType.convert(iRight, iLeft.getClass());   // String -> byte[]: the correct bytes
if (right == null) return false;
if (iLeft instanceof byte[] && iRight instanceof byte[]) {        // tests the UNCONVERTED operand
  return Arrays.equals((byte[]) iLeft, (byte[]) iRight);
}
return iLeft.equals(right);                                       // byte[].equals: reference identity
```

`OType.convert(String, byte[].class)` goes through `OStringSerializerHelper.getBinaryContent`,
which strips an optional `_..._` and `Base64`-decodes the text, so `right` holds exactly the stored
bytes. But the `Arrays.equals` branch is guarded on `iRight`, the original `String`, not on the
converted `right`, so the comparison falls through to `iLeft.equals(right)`, and an array's `equals`
is identity. `<>` is this result negated and `IN` applies it per element, which is why all three
agree on the wrong answer. Two BINARY operands take the `Arrays.equals` branch, which is why
`b = b2` works.

The index path never meets that code: `OPropertyIndexDefinition.createValue` converts the key with
the same `OType.convert(value, byte[].class)` and the binary key serializer compares keys by
content, so the lookup matches.

The fix is to test the converted operand (`right instanceof byte[]`), or to compare arrays by
content whenever `iLeft` is a `byte[]`. On `develop` the same block lives in
`OEqualsCompareOperator.equals(Object, Object)` (`core/.../sql/parser/OEqualsCompareOperator.java`),
with the same guard on the unconverted operand.


## Scope

- **Affected:** `=`, `IN` and `<>` on a declared BINARY property against a String literal or
  parameter, wherever the predicate is evaluated rather than served by an index on that property:
  a class scan, a filter after another index, a subquery target, `MATCH`, and the `WHERE` of
  `UPDATE` and `DELETE`. Every JSON client is in this position, since JSON carries a BINARY as
  base64 text and that text is what the engine returns on read. A STRING property holding the same
  text (`b = s`) misses too.
- **Not affected:** an index lookup on the BINARY property; two BINARY operands (`b = b2`); a
  `byte[]` passed as a parameter through the Java API (it takes the `Arrays.equals` branch).
- **Nearby, same family as O5:** `b BETWEEN x AND y` throws a raw
  `ClassCastException: [B cannot be cast to java.lang.Comparable`; `s.asBinary()` is an HTTP 501
  `UnsupportedOperationException: OMethod call, something missing in the implementation`;
  `b.asString()` answers the array's identity (`[B@5814674b`), so there is no textual form to
  compare against either.
- **Workaround:** none on a scan through REST or JSON; an index on the property changes the plan
  (and the answer), or the comparison is done in the application.

## How Ganda met it

The probe of the large-data mode (`ganda orientdb probe`, the "large data" section) recorded it
while confirming that a BINARY value round-trips as base64 text on OrientDB 3.2.57; the steps
above condense a triage over every operator, index kind, literal and parameter form, and the write
path (2026-10-07). Ganda's model treats a BINARY

column as a blob -- written, stored, projected and read back, never a comparison's field
(`SCALAR_TYPES` excludes it, `predicates.draw` answers only the null tests) and never indexed
(`_without_blob_indexes`) -- so no campaign asks the question and no known-bug guard is needed; the
probe checks the fact on every run (`ganda/orientdb/RULES.md`, the large-data rule). Were the model
to ask it, every data model would scan alike (the same false answer on all three), so only the
reference leg or a one-sided index would see it: an index-vs-scan divergence inside one database.
