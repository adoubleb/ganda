# `SET a.b += v` (any `+=` / `-=` / `*=` / `/=` past a field) stores `v`

**OrientDB version:** 3.2.57 (official Docker image `orientdb:latest`, built 2026-10-05). The
code involved (`OUpdateItem.applyUpdate`, last changed 2024-09-27) is the same on the `3.2.x`
branch head and on `develop`, so the fix is needed on both.
**Java version:** OpenJDK 1.8.0_504 (Temurin, the image's)
**Client:** REST, `POST /command/<db>/sql`, the new SQL executor; a literal or a named parameter.

## Steps to reproduce

```sql
CREATE CLASS t;
CREATE PROPERTY t.id STRING;
CREATE PROPERTY t.f LONG;
CREATE PROPERTY t.emb EMBEDDED;
INSERT INTO t SET id = 'u', f = 2, emb = {'w': 2};

UPDATE t SET f += 5 WHERE id = 'u';             -- f = 7      (right)
UPDATE t SET emb.w += 5 WHERE id = 'u';         -- [{"count": 1}]
SELECT emb.w AS v FROM t WHERE id = 'u';        -- 5          (Wrong: expected 7)

UPDATE t SET emb.w = emb.w + 5 WHERE id = 'u';  -- 7 from emb.w = 2 (the spelled-out form is right)
```

From `emb.w = 2` each time (and `e1 = [{'w': 1}, {'w': 2}]`, `l = [1, 2]` on an `EMBEDDEDLIST
LONG`, `m = {'k': 2}` on an `EMBEDDEDMAP`, `lk` a `LINK` to a record with `w = 2`), every one
answers `[{"count": 1}]` and stores the right operand:

```sql
UPDATE t SET emb.w -= 5 WHERE id = 'u';          -- 5   (expected -3)
UPDATE t SET emb.w *= 3 WHERE id = 'u';          -- 3   (expected 6)
UPDATE t SET emb.w /= 2 WHERE id = 'u';          -- 2   (expected 1)
UPDATE t SET emb.w += :d WHERE id = 'u';         -- 5   with {"d": 5}  (expected 7)
UPDATE t SET emb.s += 'z' WHERE id = 'u';        -- 'z' from 'a'       (expected 'az')
UPDATE t SET e1[1].w += 5 WHERE id = 'u';        -- e1.w = [1, 5]      (expected [1, 7])
UPDATE t SET l[1] += 5 WHERE id = 'u';           -- l = [1, 5]         (expected [1, 7])
UPDATE t SET m.k += 5 WHERE id = 'u';            -- 5, likewise m['k'] and m[:key]  (expected 7)
UPDATE t SET lk.w += 5 WHERE id = 'u';           -- the LINKED record's w becomes 5 (expected 7)
UPDATE t SET emb.w += 5 RETURN AFTER emb.w WHERE id = 'u';   -- [{"emb.w": 5}]
```

## Expected behavior

The SQL syntax page (`src/sql/SQL-Syntax.md` on the 3.2.x docs branch, "Math + Assign
operators") says these operators "can be used in UPDATE statements" and that `a += 2` "is
just a shortcut for `a = a + 2`"; for a non-number operand `+=` concatenates. The grammar
accepts them after any left modifier (`UpdateItem: Identifier [ Modifier ] (= | += | -= |
*= | /=) Expression` in `OrientSQL.jjt`). So `SET emb.w += 5` must store what `SET emb.w =
emb.w + 5` stores, `7`, and likewise for a list element, a map entry and a linked record's
field. Since `UPDATE ... INCREMENT` is refused by the new executor (`Cannot execute with
UPDATE PUT/ADD/INCREMENT new executor`, plain or dotted), these operators are what takes its
place.

## Actual behavior

With a left modifier (a dotted field, a list index, a map key) the operator is ignored and
the item behaves as `=`: the right operand is stored, the record is counted, and no error is
raised. The two forms the documentation calls equivalent answer differently. Through a
`LINK` the wrong value lands in another record than the one the statement counts.

## Analysis

`OUpdateItem.applyUpdate` (`core/.../sql/parser/OUpdateItem.java:130`) evaluates the right
side and then branches on the modifier only:

```java
public void applyUpdate(OResultInternal doc, OCommandContext ctx) {
  Object rightValue = right.execute(doc, ctx);
  ...
  if (leftModifier == null) {
    applyOperation(doc, left, rightValue, ctx);          // switch (operator): =, +=, -=, *=, /=
  } else {
    Object val = doc.getProperty(left.getStringValue());
    ...
    leftModifier.setValue(doc, val, rightValue, ctx);    // OUpdateItem.java:142, operator never read
  }
}
```

`applyOperation` (`OUpdateItem.java:207`) is the only place that reads `operator`: for `+=`
and the others it stores `calculateNewValue(doc, ctx, <op>)`, which builds the expression
`left <op> (right)` and evaluates it on the record. With a modifier that method is never
called, so `OModifier.setValue` receives the bare right value. `calculateNewValue` already
handles a modifier,

```java
OExpression leftEx = new OExpression(left.copy());
if (leftModifier != null) {                               // OUpdateItem.java:342, unreachable
  ((OBaseExpression) leftEx.mathExpression).modifier = leftModifier.copy();
}
```

so the intended evaluation (`emb.w + (5)`) exists, but `applyUpdate` never routes a modified
item to it.

The fix is in `applyUpdate`'s modifier branch: when `operator != OPERATOR_EQ`, compute the
value with `calculateNewValue` for the operator's `OMathExpression.Operator` and pass that to
`leftModifier.setValue`. On `develop` `applyUpdate` is unchanged.

## Scope

- **Affected:** `+=`, `-=`, `*=` and `/=` in `UPDATE ... SET` whenever the left side has a
  modifier: a field of an `EMBEDDED` document (`emb.w`), an element by index (`l[1]`,
  `e1[1].w`), a map entry (`m.k`, `m['k']`, `m[:key]`) and a field through a `LINK` (`lk.w`,
  which writes the linked record). Literal or parameter on the right; numbers and the string
  concatenation alike; `RETURN AFTER` reports the stored value.
- **Not affected:** the operators on a plain field (`f += 5` is `7`); the spelled-out
  `emb.w = emb.w + 5` and `e1[1].w = e1[1].w + 5` (both `7`).
- **Nearby:** `%=` is documented with the others, and the lexer has the token (`REMASSIGN`),
  but `UpdateItem` does not accept it: `SET f %= 2` is a parse error, with or without a
  modifier. Past a filter, a range or several indexes (`e1[t = 'b'].w += 5`) the assignment is
  dropped altogether (O48). Separately, a change to a map inside a loaded `EMBEDDED` document
  is never saved (O50), which is why `emb.n.x += 5` shows no change at all rather than `5`.
- **Workaround:** spell the arithmetic out (`SET emb.w = emb.w + 5`).

## How Ganda met it

During the triage of O48 (2026-10-24, the same server), the scope pass tried `SET e1[1].w +=
5` as a positional control and stored `5` rather than `7`; the cases above were then run on
every operator, modifier kind, literal and parameter. Ganda never emits these operators:
`UPDATE ... INCREMENT` is refused (`accepts.update.increment`, probe check C210), the
arithmetic write is spelled `f = f <op> :wd` on a top-level field (the write IR's `arith`
door in `ganda/orientdb/concrete_syntax/query/write.py`, and `_arith_set_clause` in
`ganda/orientdb/concrete_syntax/setup/common.py`), and the doc data model writes an edge attribute by re-setting the whole
list (O48). So no campaign asks the question and no rule or guard reads it. Were a write
spelled `a.b += d` on one data model and `a.b = a.b + d` on another, the state check would
see the first off the consensus.
