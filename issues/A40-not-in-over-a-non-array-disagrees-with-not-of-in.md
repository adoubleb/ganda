# A40: `NOT (x IN y)` over a non-array attribute is false, while the same expression over a constant is true

**Version.** ArangoDB 3.12.11 (Docker `arangodb:3.12.11`, single server, default options).
Found by Ganda's ArangoDB port on 2026-10-09, the first smoke campaign (seed 1, an
`ir_select` case over an edge type's list attribute); re-checked by `ganda arangodb probe
A40`.

## Steps to reproduce

```aql
RETURN [1 IN null, NOT (1 IN null), 1 NOT IN null]
```

answers `[false, true, false]`: `IN` is false for a non-array right operand, `NOT` of it
is true, and `NOT IN` is false (its documented behaviour: the value is not found in an
array, and there is no array).

```aql
FOR d IN [{}, {x: null}, {x: 7}, {x: [1]}, {x: [2]}]
  RETURN [d.x, 1 IN d.x, NOT (1 IN d.x), 1 NOT IN d.x, NOT (NOT (1 IN d.x))]
```

answers, for the first three documents (an absent attribute, a null, a scalar):

```
[null, false, false, false, false]
[null, false, false, false, false]
[7,    false, false, false, false]
```

## Expected

`NOT (1 IN d.x)` is the negation of `1 IN d.x`. Over the same non-array value the
constant form answers true; over the attribute it answers false, so a filter and its
logically equivalent rewrite disagree:

```aql
FOR d IN [{}, {x: null}, {x: [1]}, {x: [2]}, {x: 7}]
  FILTER (2 IN d.x OR NOT (1 IN d.x)) RETURN d
-- [{x: [2]}]
FOR d IN [{}, {x: null}, {x: [1]}, {x: [2]}, {x: 7}]
  FILTER NOT ((NOT (2 IN d.x) AND NOT (NOT (1 IN d.x)))) RETURN d
-- [{}, {x: null}, {x: [2]}, {x: 7}]
```

The second filter is De Morgan's form of the first.

## Actual / analysis

Over a non-constant operand the optimizer normalizes `NOT (a IN b)` into `a NOT IN b`
and `NOT (NOT e)` into `e`. `NOT IN` is not the negation of `IN` when `b` is not an
array -- both are false -- so the rewrite changes the answer exactly on the records
whose attribute is null, absent or a scalar. Constant folding evaluates `NOT (1 IN
null)` as the negation (true), which is how the constant and attribute forms come to
disagree. The array comparison operators are not affected: `NOT (d.x ANY == 1)`, `NOT
(d.x ANY IN [1])` and `NOT ([1] ALL IN d.x)` answer true over the same three documents,
as the negation should.

## Scope

- Affected: every `NOT (x IN attr)` where `attr` may be null, absent or a scalar on
  some document, directly or under a wider boolean tree the optimizer pushes the NOT
  through; the double negation `NOT (NOT (x IN attr))` is folded away and is right.
- Not affected: `IN` without a negation; `NOT IN` written as such (false, as
  documented); the `ANY` / `ALL` / `NONE` array comparison operators; a constant right
  operand.
- A workaround: write `NOT (attr != null AND x IN attr)`... is not one either (the
  conjunction is pushed through); `x NOT IN (attr || [])` states the intended reading
  explicitly.

## How Ganda met it

The oracle compares a filter `p` with its rewrite `NOT (NOT a AND NOT b)` for `a OR b`;
with `b` a list membership over a column holding nulls the two disagreed on every data
model alike (a same-database pair). `known_bugs.A40` withholds a variant whose predicate
has a list membership under a NOT where the column holds a null or absent value; the
probe pins the current answers so a fix shows up as a changed fact.
