# O27 -- a DATETIME compared with a string the date format only partially matches (an ISO `T` separator, no seconds) is compared as the date's midnight; the same string is rejected on write

**Version:** OrientDB 3.2.56. Found while probing the DATETIME / DATE column types for the fuzzer's `date_map_probability` axis.

```sql
CREATE CLASS dt; CREATE PROPERTY dt.id STRING; CREATE PROPERTY dt.t DATETIME;
INSERT INTO dt SET id = 'a', t = '2024-01-01 00:00:00';
INSERT INTO dt SET id = 'b', t = '2024-01-01 12:34:56';

SELECT id FROM dt WHERE t = '2024-01-01 12:34:56';        -- [b]
SELECT id FROM dt WHERE t = '2024-01-01T12:34:56';        -- [a]        expected [b] (or an error)
SELECT id FROM dt WHERE t > '2024-01-01T12:34:56';        -- [b]        expected []
SELECT id FROM dt WHERE t = '2024-01-01 12:34';           -- [a]        expected [] (or an error)
SELECT id FROM dt WHERE t = '2024-01-01';                 -- [a]        (a date alone is that day's midnight: documented)
SELECT id FROM dt WHERE t = '2024-01-01 12:34:56.789';    -- [b]        (millis dropped, as on write)
SELECT id FROM dt WHERE t = '2024-13-01';                 -- []
INSERT INTO dt SET id = 'w', t = '2024-01-01T12:34:56';   -- rejected: Error on conversion of date ... using the format: yyyy-MM-dd HH:mm:ss
INSERT INTO dt SET id = 'w', t = '2024-01-01 12:34';      -- rejected
```

The comparison's conversion falls back to the `yyyy-MM-dd` format when the full pattern fails, keeping the date and dropping whatever followed it, while the write path applies the full pattern and rejects the string; an index on the column converts the same way, so the wrong answer is consistent across plans. The fuzzer's reference expects what the string says and withholds every side for such a pivot (`guards.o27_predicate`); its date-only and epoch-millisecond pivots are exact.
