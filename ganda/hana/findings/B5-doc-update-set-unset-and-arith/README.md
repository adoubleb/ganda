# B5 — doc UPDATE: SET and UNSET combine with a comma; SET takes values only

**Bucket:** B — not a bug (verified 2026-10-10). **Repro:** `python3 -I repro.py`

## Claim (corrected 2026-10-10)
1. `UPDATE c SET $."f" = 1, UNSET $."g"` (and `UNSET ..., SET ...`) works; WITHOUT the comma it is a
   syntax error (`257`). The first write-up tested only the comma-less form and concluded that SET
   and UNSET cannot combine.
2. A SET value must be a value, not an expression over a field: `$."f" = $."f" + c` is refused
   (`[7] only constant expressions allowed`).

## Actual (from repro.py)
```
SET ... UNSET ... (no comma)  -> [257] incorrect syntax near "UNSET"
SET ..., UNSET ... (comma)    -> OK 1        UNSET ..., SET ... (comma) -> OK 1
SET f = f + c (field arith)   -> [7] only constant expressions allowed
```

## Verdict
**Not a bug.** The Document Store Guide's UPDATE page says SET and UNSET may share one statement, and
its example writes them with a comma; its grammar line (`<set_fields> [ <unset_fields> ]`) omits the
comma, a doc slip. A SET `<value>` is documented as a scalar, object, array, null, `CAST(? AS ...)`
or `PARSE_JSON(?)` -- no field reference (https://help.sap.com/docs/hana-cloud-database/sap-hana-cloud-sap-hana-database-json-document-store-guide/update-statement).

## What the provider does
Emits SET and UNSET as two statements (`mapping/document.py`), and computes an arithmetic write's
new value per row and binds it (`concrete_syntax/write.py`). The comma form would make a mixed
SET / UNSET one atomic statement.
