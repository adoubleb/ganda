# A change to a map or list inside a loaded EMBEDDED document is never saved

**OrientDB version:** 3.2.57 (official Docker image `orientdb:latest`, built 2026-10-05; also
the image's own `orientdb-core-3.2.57.jar` in embedded mode). The code involved (`OTrackedMap`,
last changed 2021-08-02, `OTrackedList`, and the binary record serializer V1) is the same on
the `3.2.x` branch head and on `develop`, so the fix is needed on both.
**Java version:** OpenJDK 1.8.0_504 (Temurin, the image's)
**Client:** REST, `POST /command/<db>/sql` and `/script` (the new SQL executor), and the Java
document API (`ODocument`, `ODatabaseSession.save`).

## Steps to reproduce

```sql
CREATE CLASS t;
CREATE PROPERTY t.id STRING;
CREATE PROPERTY t.emb EMBEDDED;
INSERT INTO t SET id = 'u', emb = {'w': 2, 'n': {'x': 2}, 'l': [1, 2]};

UPDATE t SET emb.n.x = 9 WHERE id = 'u';                        -- [{"count": 1}]
SELECT emb.n.x AS v FROM t WHERE id = 'u';                      -- 2   (Wrong: expected 9)

UPDATE t SET emb.n.x = 9 RETURN AFTER emb.n.x WHERE id = 'u';   -- [{"emb.n.x": 9}]
SELECT emb.n.x AS v FROM t WHERE id = 'u';                      -- 2   (the answer said 9)

UPDATE t SET emb.w = 9 WHERE id = 'u';                          -- 9   (a field of the embedded document itself: right)
UPDATE t SET emb.n.x = 9, z = 1 WHERE id = 'u';                 -- emb.n.x = 9, z = 1  (any other change to the record carries it)
```

From the same record each time, these also answer `[{"count": 1}]` and store nothing:

```sql
UPDATE t SET emb.n.x = :v WHERE id = 'u';            -- {"v": 9}
UPDATE t SET emb['n'].x = 9 WHERE id = 'u';          -- likewise emb.n['x'], a new key emb.n.z, three levels emb.n.o.y
UPDATE t SET emb.l[0] = 9 WHERE id = 'u';            -- emb.l stays [1, 2]
UPDATE t REMOVE emb.n.x WHERE id = 'u';              -- emb.n keeps x
BEGIN; UPDATE t SET emb.n.x = 9 WHERE id = 'u'; COMMIT;   -- through /script, the same
```

and so does a map inside an embedded document held by a list or a map
(`e2 = [{'@type': 'd', 'w': 1, 'n': {'x': 1}}]` in an `EMBEDDEDLIST`: `SET e2[0].n.x = 9` stores
nothing while `SET e2[0].w = 9` does; `md = {'a': {'@type': 'd', 'n': {'x': 1}}}` in an
`EMBEDDEDMAP`: `SET md.a.n.x = 9` stores nothing).

The same through the Java API, on a record loaded from storage (`db.getLocalCache().clear();
ODocument d = db.load(rid); ODocument emb = d.field("emb");`):

```java
((Map) emb.field("n")).put("x", 9);  d.isDirty();  // false
db.save(d);                                         // reloaded: n = {x=2}, also inside begin()/commit()
((List) emb.field("l")).set(0, 9);  db.save(d);     // reloaded: l = [1, 2]; add(3) likewise
emb.field("w", 9);  d.isDirty();                    // true; reloaded: w = 9
```

## Expected behavior

A change to a value inside an embedded document is a change to the record that holds it:
the record must become dirty and the save must write it, as it does for a field of the
embedded document itself (`emb.w`), for a map inside a map (`SET m.n.x = 9` on an
`EMBEDDEDMAP`, or an undeclared map of maps) and for an embedded document inside an embedded
document (`SET embd.n.x = 9` where `n` is `{'@type': 'd', ...}`), all of which persist. An
`UPDATE` that answers `count: 1`, or `RETURN AFTER` with the new value, must leave that value
stored.

## Actual behavior

The new value is in the loaded record (`RETURN AFTER` shows it), but the record is not marked
dirty, so the save adds no operation and the storage keeps the old value. Nothing is raised;
the statement reports the record as updated. The change is written only if something else
in the same statement or save marks the record dirty (another `SET` item, even `emb.w = 2` to
its current value).

## Analysis

An embedded document read from storage starts dirty and stays so. The binary serializer
builds it with `new ODocumentEmbedded()` (`ORecordSerializerBinaryV1.deserializeEmbeddedAsDocument`,
`.../serialization/serializer/record/binary/ORecordSerializerBinaryV1.java:1130`), whose
`dirty` flag has `ORecordAbstract`'s default `true` (`ORecordAbstract.java:61`), sets its owner
to the enclosing record, and never clears the flag. Probed on the loaded record: the record
is `dirty=false`, its `emb` is an `ODocumentEmbedded` with `dirty=true` and owner the record,
and `emb.n` is an `OTrackedMap` whose source record is `emb`.

A tracked collection reports a change to its source record only when that record is NOT
already dirty (`OTrackedMap.java:147`; `OTrackedList.setDirty` is the same):

```java
public OTrackedMap<T> setDirty() {
  if (sourceRecord != null) {
    if (!(sourceRecord instanceof ORecordAbstract)
        || !((ORecordAbstract) sourceRecord).isDirty()) {   // emb is "dirty" already ...
      sourceRecord.setDirty();                              // ... so this never runs
    }
  }
  this.dirty = true;
  ...
```

`put` reaches it through `updateEvent` / `addEvent` (directly, or through
`OSimpleMultiValueTracker.onAfterRecordChanged`, which calls the map's `setDirty`). Had it called
`emb.setDirty()`, `ODocument.setDirty` would have propagated to the owner ("PROPAGATES TO THE
OWNER", `ODocument.java:2025`) and the record would be dirty. The skip stops the propagation at
the stale flag, the record stays clean, and `OTransactionOptimistic.saveRecord` adds an
operation only for a record in the dirty manager or with `iRecord.isDirty()` true
(`OTransactionOptimistic.java:457`), so nothing is written. `emb.w = 9` works because
setting a field of the embedded document calls the document's own `setDirty()`, which
propagates to the owner whatever the flag holds. A map inside a map works because the inner map's source record is the outer map
(or the record), which is not a stale document. An embedded document inside an embedded document
works for the same reason as `emb.w`.

The fix: clear the flag of an embedded document after it is deserialized
(`ORecordInternal.unsetDirty` in `deserializeEmbeddedAsDocument` and wherever else embedded
documents are materialized from bytes), so the short-circuit in the tracked collections means
what it assumes. Alternatively, drop the short-circuit when the source record is an embedded
document, since `ODocument.setDirty` must reach the owner each time a nested value changes.
The second is the smaller change; the first also fixes any other code that reads an embedded
document's dirty flag (`ODocumentEntry.isTrackedModified` returns it for an embedded value).

## Scope

- **Affected:** any change to an `OTrackedMap` or `OTrackedList` (put, remove, set, add) held
  by an embedded document that was LOADED, at any depth below it, when nothing else marks the
  record dirty. Through SQL: `UPDATE ... SET a.b.c = v`, `a.b[i] = v`, `REMOVE a.b.c`, where `a`
  is an `EMBEDDED` property (or an embedded document inside an `EMBEDDEDLIST` / `EMBEDDEDMAP`)
  and `b` a map or list; literal or parameter; `/command`, `/script` and a transaction alike.
  Through the Java API: the same changes followed by `save`, with or without a transaction.
  The statement answers `count: 1` and `RETURN AFTER` the new value.
- **Not affected:** a field of the embedded document itself (`emb.w`); a map or list inside a
  map or list with no embedded document between them (`EMBEDDEDMAP` `m.n.x`, `e1[1].n.x` when
  the elements are maps, an undeclared map of maps); an embedded document inside an embedded
  document (`embd.n.x`); a change made in the same statement as any change that dirties the
  record; `MERGE` and a whole-value `SET emb.n = {...}` or `SET emb = {...}`.
- **Silent loss beyond SQL:** the Java API loses the change the same way, so an application
  that edits a nested map of an embedded document and saves sees it until the record is
  reloaded.
- **Workaround:** re-set the nested value whole (`SET emb.n = :n`), or add any change that marks
  the record dirty to the same statement or save.

## How Ganda met it

During the triage of O49 (2026-10-24), `SET emb.n.x += 5` showed no change at all instead of
the `5` the other O49 cases stored; `SET emb.n.x = 9` with a plain `=` did the same. The cases
above were then run against the same server, and the dirty flags read in embedded mode with
the image's own 3.2.57 jars. Ganda never writes inside an embedded value: its doc data model
holds relationships as `EMBEDDEDLIST`s of link documents and writes an edge attribute by
re-setting the whole list (O48), and node fields are scalars, lists and maps written whole.
So no campaign meets it and no rule or guard reads it. Were a write to reach into an
embedded document on one data model and set a row field on the others, the state check would
see the first off the consensus.
