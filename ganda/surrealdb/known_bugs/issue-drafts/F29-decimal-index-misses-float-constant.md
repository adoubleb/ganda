# F29: an index over a decimal column misses a float constant the scan matches

**Version.** SurrealDB 3.3.4 (`surrealdb/surrealdb:v3.3.4`, memory backend, over `/sql`).
Found 2026-10-26 by Ganda's campaign c8 (seed 103): a `nexists` traversal whose source
predicate `f2 IN [0.3, -81.319, 999999.999]` over a DECIMAL column answered no record on the
graph data model, the only one with an index on `f2`, where the doc data model and the
pulled-up form (the same filter over a projection, never index-served) answered the record
holding `0.3dec`.

**Reproduction.**

```sql
DEFINE TABLE d SCHEMALESS;
DEFINE INDEX ix_d_f ON TABLE d FIELDS f;
CREATE d:a SET f = 0.3dec;
CREATE d:b SET f = 2.5dec;
SELECT VALUE id FROM d WHERE f = 0.3;            -- []          expected ["d:a"]
SELECT VALUE id FROM d WHERE f IN [0.3, 2.5];    -- ["d:b"]     expected ["d:a", "d:b"]
SELECT VALUE id FROM d WHERE f = 2.5;            -- ["d:b"]     (a dyadic float: right)
SELECT VALUE id FROM d WHERE f = 0.3dec;         -- ["d:a"]     (the typed constant: right)
RETURN 0.3dec = 0.3;                             -- true        (the comparison itself)
```

Without the index, `f = 0.3` answers `d:a`.

**Analysis.** `0.3dec = 0.3` holds: the comparison converts the float to a decimal through
its shortest decimal text (`0.3`). The index lookup converts the constant another way --
through its exact binary expansion (`0.299999999999999988897769753748...`), which equals no
stored key. A dyadic float (`2.5`, `1.0`, `5.0`) has one expansion and is found; a range
bound is compared, not looked up, and stays right. The same engine answers the same
equality differently depending on whether an index serves it.

**Scope.** Every decimal column with an index, under `=` / `IN` with a float constant that
is not a dyadic rational. (The analogue of OrientDB's O20, where a scan compared a
decimal against a double's binary expansion and an index against its text: the roles
reversed.)

**How Ganda met it.** The data model holding the index answered an empty set where the
other two answered the record. Ganda avoids the shape by design (`works_around("F29")` in
`concrete_syntax/query/predicate_sampling/constants.py`: a decimal column's same-type
constants are decimal literals and its cross-type constants integers); pinned by probe
check C44.
