# `UPDATE ... SET list[<condition>].field = v` answers its count and changes nothing

**OrientDB version:** 3.2.57 (official Docker image `orientdb:latest`, built 2026-10-05). The
code involved is the same on the `3.2.x` branch head (`OModifier`, last changed 2025-01-28;
`OSuffixIdentifier`, 2024-06-27; `OUpdateItem`, 2024-09-27) and on `develop`, so the fix is
needed on both.
**Java version:** OpenJDK 1.8.0_504 (Temurin, the image's)
**Client:** REST, `POST /command/<db>/sql`, the new SQL executor; a literal or a named parameter.

## Steps to reproduce

```sql
CREATE CLASS t;
CREATE PROPERTY t.id STRING;
CREATE PROPERTY t.e1 EMBEDDEDLIST;
INSERT INTO t SET id = 'u', e1 = [{'w': 1, 't': 'a', 'n': {'x': 1}}, {'w': 2, 't': 'b', 'n': {'x': 2}}];

SELECT e1[t = 'b'].w AS v FROM t WHERE id = 'u';             -- [2]: the filter selects the element

UPDATE t SET e1[t = 'b'].w = 7 WHERE id = 'u';               -- [{"count": 1}]
SELECT e1.w AS v FROM t WHERE id = 'u';                      -- [1, 2]   (Wrong: expected [1, 7])

UPDATE t SET e1[t = 'b'].w = 7 RETURN AFTER e1.w WHERE id = 'u';   -- [{"e1.w": [1, 2]}]

UPDATE t SET e1[1].w = 8 WHERE id = 'u';                     -- [{"count": 1}]
SELECT e1.w AS v FROM t WHERE id = 'u';                      -- [1, 8]   (by position it works)

UPDATE t SET e1[t = 'b'] = {'w': 7, 't': 'b'} WHERE id = 'u';
-- HTTP 501: java.lang.UnsupportedOperationException: SET value on conditional filtering will be supported soon
```

Each of these also answers `[{"count": 1}]` and leaves `e1` as it was (every case from the
INSERT above):

```sql
UPDATE t SET e1[t = :k].w = :v WHERE id = 'u';      -- {"k": "b", "v": 7}: a parameter in the filter and the value
UPDATE t SET e1[t = 'b'].n.x = 9 WHERE id = 'u';    -- a nested field below the filtered element
UPDATE t SET e1[n.x = 2].w = 7 WHERE id = 'u';      -- a filter on a nested field
UPDATE t SET e1[w > 0].w = 9 WHERE id = 'u';        -- a filter matching every element
UPDATE t SET e1[t = 'b'].w = 7, id = 'v' WHERE id = 'u';   -- id becomes 'v'; e1 is unchanged
BEGIN; UPDATE t SET e1[t = 'b'].w = 7 WHERE id = 'u'; COMMIT;   -- through /script, the same
```

The same holds on a class without the property declared (a schemaless list), on an
`EMBEDDEDSET` and on a `LINKLIST` (`SET ll[t = 'b'].w = 7` leaves the linked record alone,
while `SET ll[1].w = 7` writes it).

## Expected behavior

The documentation of `UPDATE` (`src/sql/SQL-Update.md` on the 3.2.x docs branch) describes
`SET <field-name> = <field-value>` and shows a bracket filter only on the right side of
`REMOVE` (`REMOVE addresses = addresses[city = 'Rome']`). The grammar, though, accepts any
modifier after the field on the left of `SET` (`UpdateItem: Identifier [ Modifier ] (= | += |
...) Expression` in `OrientSQL.jjt`), the executor implements the positional form
(`e1[1].w = 8`) and a filter over a single `EMBEDDED` document (`emb[t = 'b'].w = 7` writes
it), and it refuses the filter it cannot apply when nothing follows it (`e1[t = 'b'] = ...`,
HTTP 501). So one of two answers is right for `SET e1[t = 'b'].w = 7`: set `w` on every
element the filter selects (the elements `SELECT e1[t = 'b'].w` reads), or refuse the statement like
its undotted sibling. A statement the engine accepts must not report the record as updated
and then drop the assignment.

## Actual behavior

The statement is accepted, the record is saved and counted (`[{"count": 1}]`; the other
`SET` items of the same statement are applied), and the filtered assignment is dropped
without an error or a warning. `RETURN AFTER` shows the unchanged list. A client cannot tell
this statement from one that worked.

## Analysis

`OUpdateItem.applyUpdate` (`core/.../sql/parser/OUpdateItem.java:130`) hands an item with a
left modifier to the modifier, with the property's current value as the target:

```java
} else {
  Object val = doc.getProperty(left.getStringValue());
  ...
  leftModifier.setValue(doc, val, rightValue, ctx);      // OUpdateItem.java:142
}
```

For `e1[t = 'b'].w` the modifier is the filter `[t = 'b']` (`condition`) with `.w` as its
`next`. `OModifier.setValue` (`OModifier.java:310`) resolves every step but the last with
`calculateLocal`, whose `condition` branch copies the matching elements of a collection into
a NEW list:

```java
} else if (OMultiValue.isMultiValue(target)) {
  List<Object> result = new ArrayList<>();            // OModifier.java:357
  for (Object o : OMultiValue.getMultiValueIterable(target)) {
    if (condition.evaluate(o, ctx)) {
      result.add(o);
    }
  }
  return result;
```

and then calls the last step, `.w`, on that list: `doSetValue` reaches
`suffix.setValue(target, value, ctx)` (`OModifier.java:327`). `OSuffixIdentifier.setValue`
(`OSuffixIdentifier.java:340`) only knows three kinds of target, and has no `else`:

```java
public void setValue(Object target, Object value, OCommandContext ctx) {
  if (target instanceof OResult) {
    setValue((OResult) target, value, ctx);
  } else if (target instanceof OIdentifiable) {
    setValue((OIdentifiable) target, value, ctx);
  } else if (target instanceof Map) {
    setValue((Map) target, value, ctx);
  }
  // an ArrayList falls through: nothing is set, nothing is thrown
}
```

So the assignment is lost on the `ArrayList`, and the update plan saves and counts the
record as usual. The list holds the stored elements themselves (`result.add(o)`; an element
written as `{'w': 1, 't': 'a'}` is an `OTrackedMap`), so a setter that walked it would update
them in place, exactly as the positional form does: `OArraySingleValuesSelector.execute`
returns the one selected element itself (`result.get(0)` when one index is given), and
`setValue(Map ...)` writes it. (Were the elements embedded documents, a write below one of
their fields would meet O50.)

The explicit refusal of the undotted form is the `condition` branch of `doSetValue`
(`OModifier.java:331-333`, `// TODO ... will be supported soon`), reached only when the filter
is the LAST step; the dotted form never gets there. Its `REMOVE` counterpart (`Remove on
conditional filtering will be supported soon`, orientechnologies/orientdb#10385) was
implemented in commit e70baf7 (2025-01-28); the `SET` side was left as it was.

The fix is in `OSuffixIdentifier.setValue(Object, ...)`: when the target is a multi-value
(`OMultiValue.isMultiValue`), set the field on each element; and throw an
`OCommandExecutionException` for any other target it cannot set, rather than returning. With
that, `e1[t = 'b'].w`, `e1[0..1].w`, `e1[0, 1].w` and `e1.w` would set every selected element.
If the project prefers not to support it, the same `else` must refuse the statement. On
`develop` `OSuffixIdentifier.setValue(Object, ...)` wraps an `OElement` in an
`OUpdatableResult` but still has no collection branch and no `else`, and `OModifier.doSetValue`
still carries the TODO.

## Scope

- **Affected:** an `UPDATE ... SET` whose left side selects
  SEVERAL values of a list and then sets a field on them: a bracket filter
  (`e1[t = 'b'].w`, `e1[n.x = 2].w`, a parameter in the filter or the value, a nested field
  `e1[t = 'b'].n.x`), a range (`e1[0..2].w`, `e1[0-1].w`), several indexes (`e1[0, 1].w`) and
  a field of the bare list (`e1.w`). It answers `count: 1` and changes nothing, on
  `EMBEDDEDLIST`, `EMBEDDEDSET`, `LINKLIST` and an undeclared list, through `/command`,
  `/script` and in a transaction, whether the filter matches one, every or no element.
- **Not affected:** one index (`e1[1].w`, `ll[1].w`); a filter over a single `EMBEDDED`
  document (`emb[t = 'b'].w` is set when the document matches); `SET e1[t = 'b'] = ...`
  with nothing after the filter, which is refused (a raw HTTP 501
  `UnsupportedOperationException`, the O5 family); `REMOVE e1 = e1[t = 'b']`.
- **Nearby, same method:** `OUpdateItem.applyUpdate` never reads `operator` when there is a
  left modifier, so a compound assignment through one stores the right operand:
  `SET e1[1].w += 5` on `w = 2` stores `5`, `SET e1[1].w *= 3` stores `3`, `SET emb.w += 5`
  stores `5` (all `count: 1`). `applyOperation`, which handles `+=` / `-=` / `*=` / `/=`, runs
  only without a modifier, and the modifier branch in `calculateNewValue`
  (`OUpdateItem.java:342`) is never reached. Not covered by the fix above; written up as O49
  (`issues/O49-compound-assignment-through-a-modifier-stores-the-right-operand.md`).
- **Workaround:** re-set the whole list, e.g. `SET e1 = :list` with the edited list built by
  the client, or address the element by position.

## How Ganda met it

The legacy OrientDB port recorded "an element of an embedded list cannot be set in place" as
a probed fact, and its doc data model writes an edge attribute by re-setting each source's
whole list by id. Ganda carried that as an engine rule, `accepts.update.embedded_list_element`
refused, which nothing checked. When the rules were bound to probe checks (2026-10-24), check
C338 in `ganda/orientdb/scripts/probe_osql.py` was written for it on 3.2.57: the filtered
form is not refused, it is accepted with `count: 1` and changes nothing, while the positional
form updates. The rule is now `accepts.update.filtered_list_element` (`SQL`, refused) in
`ganda/orientdb/rules/catalog.py`, read by `assumes(...)` in
`ganda/orientdb/concrete_syntax/query/write.py`, so no campaign emits the form and the doc data
model keeps re-setting whole lists. The triage above (the parameter, nested-field, whole-element,
range, multi-index, collection-kind and transaction forms) was run against the same server
on 2026-10-24. Had the doc data model written edge attributes through the filter, the state
check would have caught it: the graph and rel data models store the new value, so the doc
data model's unchanged list is off the consensus after the write.
