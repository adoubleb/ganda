# O24 -- a value beyond the range of an INTEGER, SHORT or BYTE property is stored WRAPPED (2147483648 becomes -2147483648) instead of being rejected

**Version:** OrientDB 3.2.56. Found while probing the narrow integer types for the fuzzer's `narrow_type_probability` axis (the generator only ever writes in-range values, so the fuzzer never exercises it).

```sql
CREATE CLASS i1; CREATE PROPERTY i1.id STRING; CREATE PROPERTY i1.f INTEGER;
INSERT INTO i1 SET id = 'w', f = 2147483648;                    -- stored f = -2147483648
INSERT INTO i1 SET id = 'w2', f = :v;    -- :v = 2147483648       stored f = -2147483648
INSERT INTO i1 CONTENT {"id": "w3", "f": 2147483648};           -- stored f = -2147483648
UPDATE i1 SET f = 2147483648 WHERE id = 'w';                    -- stored f = -2147483648
CREATE PROPERTY i1.s SHORT;  INSERT INTO i1 SET s = 40000;      -- stored s = -25536
CREATE PROPERTY i1.b BYTE;   INSERT INTO i1 SET b = 200;        -- stored b = -56
CREATE PROPERTY i1.l LONG;   INSERT INTO i1 SET l = 1e300;      -- stored l = 9223372036854775807 (saturated, not wrapped)
```

The declared-type coercion on write takes `Number.intValue()` / `shortValue()` / `byteValue()`, so the record silently holds a different number from the one written, through every write door (SET, a bind parameter, CONTENT, UPDATE). A LONG saturates a too-large double instead, and a JSON integer beyond a long is rejected at parsing -- three behaviours for one situation.
