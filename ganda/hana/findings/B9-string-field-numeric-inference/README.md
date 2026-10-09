# B9 — string field with a numeric-looking value inferred as a number

**Bucket:** B/A boundary (you decide) — **context-dependent, not minimally reproducible.**
**Repro:** `python3 -I repro.py` (shows the isolated forms, which do NOT fail on this build).

## Claim
In a multi-view doc traversal join, a collection STRING field whose value reads numeric (the string
`"0"`) was inferred as a number by the Document Store, so a string comparison against it
(`f1 BETWEEN 'A' AND 'i'`) failed: `[-10427] Conversion of parameter/column from data type UCS2 (LE)
to BIGINT failed (invalid number: not a valid number string 'A')`.

## Honesty note
This error was observed **live** in the first round-3 campaign (a 6-view traversal join, shape
`src_hops_3[out:e,out:e,in:t] proj=distinct_ids`, `doc` + `doc:join` variants). It does **not**
minimise: the isolated single/edge-view forms in `repro.py` all succeed on this build. So the
evidence is the campaign bundle, not a one-liner. After the provider cast string fields `TO_NVARCHAR`
in doc views, the round-4 campaign ran **0 findings** on the same seed — confirming the fix resolves
the real failure.

## How to judge
Because it does not minimise, treat it as a provider-side defensive fix for a context-dependent
Document Store type-inference behaviour. Whether the underlying inference (a JSON **string** `"0"`
treated as a number in a comparison) is a quirk or a bug is the open question — but it is not cleanly
demonstrable in isolation, so it is the weakest of the findings. If you want to chase it, replay the
campaign bundle with `repro.py` under `bugs/<...>distinct_ids/` from before the fix (git stash the
`sql_cast` change).

## What the provider does
Casts a STRING field `TO_NVARCHAR(col)` in the doc views (`concrete_syntax/document.py`, `sql_cast`),
so a string field is always a string column regardless of its values.
