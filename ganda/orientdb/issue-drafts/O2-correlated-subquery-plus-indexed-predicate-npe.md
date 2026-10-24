# O2 -- a correlated subquery with a second predicate on an indexed field answers `java.lang.NullPointerException`

**Version:** OrientDB 3.2.56, REST `/command/<db>/sql` (HTTP 500, body `java.lang.NullPointerException`).
**Found by:** the fuzzer's first live smoke (seed 1, `rel:let` with a target predicate); minimal shape pinned in `scripts/probe_osql.py`. Guarded in `fuzzer/guards.py` (`o2_shape`).

## Repro

```sql
CREATE CLASS m1; CREATE PROPERTY m1.id STRING; CREATE PROPERTY m1.f1 LONG;
INSERT INTO m1 SET id = 'm1/r0', f1 = 0; INSERT INTO m1 SET id = 'm1/r1', f1 = 1;
CREATE CLASS jm; CREATE PROPERTY jm.src_id STRING; CREATE PROPERTY jm.dst_id STRING;
INSERT INTO jm SET src_id = 'm1/r0', dst_id = 'm1/r1';

-- fine while f1 is not indexed
SELECT src_id AS id, $n.f1 AS v FROM jm LET $n = (SELECT FROM m1 WHERE id = $parent.$current.dst_id AND f1 > 0);

CREATE INDEX m1_f1 ON m1 (f1) NOTUNIQUE;
SELECT src_id AS id, $n.f1 AS v FROM jm LET $n = (SELECT FROM m1 WHERE id = $parent.$current.dst_id AND f1 > 0);
-- HTTP 500: java.lang.NullPointerException
```

## Characterisation

- Needs: a correlated equality (`x = $parent.$current.y`) AND a second condition on a field with an index (any kind), in the same WHERE of a subquery (LET or inline). The index on the *correlated* column is irrelevant (present or not).
- Fires with `=`, `>`, `<`, `IN`, `BETWEEN`, `IS NULL`, a literal or a bind parameter, in either operand order. `<>` on the indexed field (a scan) does not fire.
- The same WHERE uncorrelated (`id = 'm1/r1' AND f1 > 0`) is fine. Wrapping the class in a subquery (`FROM (SELECT FROM m1)`) avoids it -- but see the FROM face below: moving the INDEXED condition into that subquery brings it back.
- The NPE escapes as the whole HTTP response body: no OrientDB error class, no message.

## Impact

Any "look the parent up and filter it" subquery becomes a server-side NPE the moment a DBA adds an index on the filtered field.


**Further face (probed 3.2.56):** `LET $h = (SELECT dst_id FROM j WHERE src_id IN (SELECT dst_id FROM j WHERE src_id = $parent.$parent.$current.id))` -- an index-served `IN (subquery)` whose subquery is correlated to the grandparent -- is the same `NullPointerException` once `src_id` is indexed, and answers correctly without the index. The fuzzer's variable-depth relational form nests correlated LETs with an UNWIND per level instead.

## The FROM face (probed 3.2.56; `scripts/probe_osql.py`, "O2 (FROM face)")

Found by the LLM-card campaign `cards-pilot1-c20` (seed 8009, card
`predi-pilot1-0022`) and 35 spellings of the round-2 card run. The correlation
sits in a query whose FROM target is a subquery that an index serves:

```sql
-- m1.f1 indexed (NOTUNIQUE)
SELECT id, $c.size() AS v FROM m1
  LET $c = (SELECT k FROM (SELECT id AS k FROM m1 WHERE f1 = 0) WHERE k = $parent.$current.id);
-- HTTP 500: java.lang.NullPointerException; answers without the index, or uncorrelated
```

- The correlation may be any comparison (`=`, `<`, reversed operands), or only a
  projected `$parent.$current.x` with no WHERE at all.
- The inner condition is anything the index serves: `=`, a range (not through a hash
  index, which cannot serve it), an `IN` list, `BETWEEN`, `IS NULL`, an `OR` with one
  indexed branch, `x IN (SELECT ...)` on an indexed column (the junction's
  `dst_id`), an `id` lookup through the UNIQUE id index. `<>`, reversed operands and
  arithmetic operands (all scans) do not fire.
- It fires two FROM levels down (`FROM (SELECT FROM (SELECT FROM m1 WHERE f1 = 0))`),
  and through a TRAVERSE rooted at such a subquery:
  `(SELECT FROM (TRAVERSE out('e') FROM (SELECT FROM v WHERE id = :k) MAXDEPTH 2) WHERE id = $parent.$current.id)`.

## The correlated-variable face

The correlation may come through a LET variable instead of `$parent.$current`:

```sql
SELECT id, $m.size() AS v FROM v1
  LET $m = (SELECT $d AS x FROM j1
            LET $b = (SELECT id FROM v1 WHERE id = $parent.$current.dst_id),
                $d = (SELECT dst_id FROM j1 WHERE src_id = $b[0].id AND dst_id IN (SELECT id FROM v1 WHERE f > 0)));
-- j1.dst_id indexed: NullPointerException
```

It fires only when `$b` is correlated and defined inside a subquery. The same `$b`
uncorrelated (`WHERE f = 0`) answers, and so does a correlated `$b` defined by the
outermost query.

Guarded for the cards in `fuzzer/cards/known.py` (`_o2_from_sub`, `_correlated_vars`);
the fuzzer's own spellings never emit these shapes.

## The whole rule (probed 3.2.56; `scripts/probe_osql.py`, "O2 (the rule)")

Every face above is one rule. Take a subquery of the outermost query -- a LET
subquery, an `IN (subquery)` operand, a projected subquery. If its text, at any depth,
reads `$parent.$current` (in a WHERE, in a projection, in a nested LET, in an
expression LET such as `LET $r = $parent.$current.id`) AND, at any depth, holds a
query level with a condition an index serves (its own WHERE, its FROM target, a
TRAVERSE root, an `IN (subquery)` operand, a sibling nested LET), the statement is a
`NullPointerException`:

```sql
-- n1.f1 indexed; f3 not
SELECT id, $m.size() AS v FROM n1 LET $m = (
  SELECT id FROM n1 WHERE f3 = $parent.$current.f3 AND f3 IN (SELECT f3 FROM n1 WHERE f1 = 0));   -- NPE
SELECT id, $m.size() AS v FROM n1 LET $m = (
  SELECT id FROM n1 LET $n = (SELECT id FROM n1 WHERE f1 = 0) WHERE f3 = $parent.$current.f3 AND $n.size() > 0);  -- NPE
SELECT id, $m.size() AS v FROM n1 LET $m = (
  SELECT id FROM n1 LET $x = (SELECT dst_id FROM j1 WHERE src_id = $parent.$current.id)
  WHERE f3 IN (SELECT f3 FROM n1 WHERE f1 = 0));                                                   -- NPE
```

- Not index-served, so no exception: `<>`, `IS NOT NULL`, reversed (`0 = f1`) and
  arithmetic operands, a comparison with `$parent.$current.x` or with a variable.
- A subquery inside a BRACKET filter is run per element the same way, and throws with
  no `$parent.$current` at all once an index serves it:
  `SELECT id, out('e')[id IN (SELECT id FROM v WHERE id = :k)].size() FROM v`. The same
  list bound by a LET first (`out('e')[id IN $n.id]`) answers.
- The outermost query's own FROM-target chain is not a subquery in this sense: an
  index-served level there over a correlated LET answers nothing instead (O1).
- The SCRIPT door is unaffected: the classic shape, the FROM face, the `IN` face and
  the bracket face all answer, correctly, inside
  `BEGIN; LET $r = (...); ROLLBACK; RETURN $r` (`/command/<db>/script`).

Guarded for the cards in `fuzzer/cards/known.py` (`_o2_tree`), which replaces the
per-face checks.

