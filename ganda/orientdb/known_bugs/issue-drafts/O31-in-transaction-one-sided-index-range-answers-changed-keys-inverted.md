# O31 -- inside a transaction, an index-served one-sided range (`<`, `<=`, `>`, `>=`) misses the records the transaction inserted, and answers the records whose key it changed INVERTED

**Version:** OrientDB 3.2.56. Found while probing the script transaction door (`BEGIN; ...; LET $r = (...); ROLLBACK; RETURN $r`) for the fuzzer's `tx_read` family.

```sql
CREATE CLASS t; CREATE PROPERTY t.id STRING; CREATE PROPERTY t.f LONG;
CREATE INDEX t_id ON t (id) UNIQUE METADATA {ignoreNullValues: true};
CREATE INDEX t_f ON t (f) NOTUNIQUE METADATA {ignoreNullValues: true};
INSERT INTO t SET id = 'a', f = 1; INSERT INTO t SET id = 'b', f = -7; INSERT INTO t SET id = 'c', f = 5; INSERT INTO t SET id = 'd', f = 100;

-- each block is one SQL script (POST /command/<db>/script), rolled back at the end
BEGIN; UPDATE t SET f = -100 WHERE id = 'c'; LET $r = (SELECT id FROM t WHERE f < 0);                    ROLLBACK; RETURN $r;   -- b            (expected b, c)
BEGIN; UPDATE t SET f = -100 WHERE id = 'c'; LET $r = (SELECT id FROM t WHERE f <= -100);                ROLLBACK; RETURN $r;   -- c            (the bound IS the key)
BEGIN; UPDATE t SET f = -100 WHERE id = 'c'; LET $r = (SELECT id FROM t WHERE f > -200 AND f < 0);       ROLLBACK; RETURN $r;   -- b, c         (a two-sided range is right)
BEGIN; UPDATE t SET f = -100 WHERE id = 'c'; LET $r = (SELECT id FROM t WHERE f IN [-100, -7]);          ROLLBACK; RETURN $r;   -- b, c
BEGIN; UPDATE t SET f = -100 WHERE id = 'c'; LET $r = (SELECT id FROM t ORDER BY f);                     ROLLBACK; RETURN $r;   -- c, b, a, d   (an index walk is right)
BEGIN; UPDATE t SET f = -100 WHERE id = 'c'; LET $r = (SELECT id FROM (SELECT FROM t) WHERE f < 0);      ROLLBACK; RETURN $r;   -- b, c         (a scan is right)
BEGIN; UPDATE t SET f = -100 WHERE id = 'c'; LET $r = (SELECT id FROM t WHERE :p > f);                   ROLLBACK; RETURN $r;   -- :p = 0 -> b, c (reversed operands are a scan)
BEGIN; UPDATE t SET f = 1000 WHERE id = 'c'; LET $r = (SELECT id FROM t WHERE f > 99);                   ROLLBACK; RETURN $r;   -- d            (expected c, d)
BEGIN; UPDATE t SET f = 1000 WHERE id = 'c'; LET $r = (SELECT id FROM t WHERE f < 6);                    ROLLBACK; RETURN $r;   -- a, b, c      (c's new key is 1000: expected a, b)
BEGIN; INSERT INTO t SET id = 'z', f = -100; LET $r = (SELECT id FROM t WHERE f < 0);                    ROLLBACK; RETURN $r;   -- b            (expected b, z)
BEGIN; INSERT INTO t SET id = 'z', f = -100; LET $r = (SELECT id FROM t WHERE id > 'c');                 ROLLBACK; RETURN $r;   -- d            (expected d, z; the UNIQUE id index too)
BEGIN; DELETE FROM t WHERE id = 'b';         LET $r = (SELECT id FROM t WHERE f < 0);                    ROLLBACK; RETURN $r;   -- (none)       (a deleted record is filtered: right)
BEGIN; UPDATE t SET f = -100 WHERE id = 'c'; COMMIT; LET $r = (SELECT id FROM t WHERE f < 0); RETURN $r;                        -- b, c         (right once committed)
```

An equality, an `IN`, a two-sided range or a `BETWEEN`, an ORDER BY index walk, the `index:` door and every scan merge the transaction's index changes correctly; a one-sided range through an SB-tree index (any of `<`, `<=`, `>`, `>=`, on a NOTUNIQUE or UNIQUE index, on the lead of a composite one too) sees the committed entries only, except that a changed record's old entry is kept when its NEW key does not satisfy the range and dropped when it does -- so the changed records come back inverted -- and a record inserted in the transaction is never found (unless the bound equals its key). Graph writes (`CREATE EDGE`, `DELETE EDGE`, `DELETE VERTEX`) inside the same kind of script are visible to `out()`, MATCH, TRAVERSE and an edge scan correctly.

The fuzzer withholds the shape (`guards.o31_known`: a one-sided range leaf on a column an SB-tree index of the side serves, after an update of that column or an insert) and re-verifies the live state after every instance's transaction cases.

**Second face (campaign c100d, seed 1046).** A range whose bounds are reversed answers nothing outside a transaction and a raw exception inside one, once the transaction has written the column (an update of it, an insert, a delete; a write to another column: nothing):

```sql
SELECT id FROM t WHERE f BETWEEN 6 AND 4;                                                                 -- (none)
BEGIN; UPDATE t SET f = 9 WHERE id = 'c'; LET $r = (SELECT id FROM t WHERE f BETWEEN 6 AND 4);    ROLLBACK; RETURN $r;   -- java.lang.IllegalArgumentException: fromKey > toKey
BEGIN; UPDATE t SET f = 9 WHERE id = 'c'; LET $r = (SELECT id FROM t WHERE f >= 6 AND f <= 4);    ROLLBACK; RETURN $r;   -- the same
BEGIN; UPDATE t SET s = 'y' WHERE id = 'c'; LET $r = (SELECT id FROM t WHERE f BETWEEN 6 AND 4);  ROLLBACK; RETURN $r;   -- (none)
```

**Third face (campaign cov400, seeds 5192, 5229, 5287, 5338, 5355): the DESC walk.** After the transaction writes a non-null value to the key, or inserts a record, a single-key `ORDER BY f DESC` walked from an SB-tree index keeping nulls answers the written record FIRST, after the nulls, whatever its value. `max` read as `ORDER BY f DESC LIMIT 1` then answers the written value. The ASC walk, an ordering with an id tiebreak (it sorts), a hash index, no index and a write of null are all right. An index ignoring nulls answers nothing on a DESC walk anyway (O18). The earlier "an ORDER BY walk is right" holds for ASC only.

```sql
CREATE INDEX t_f ON t (f) NOTUNIQUE METADATA {ignoreNullValues: false};   -- f: null, 131, 428, 894, 2^63-1, -374
BEGIN; UPDATE t SET f = 500 WHERE id = 'r1'; LET $r = (SELECT f FROM t ORDER BY f DESC); ROLLBACK; RETURN $r;
-- null, 500, 9223372036854775807, 894, 428, -374      (expected null, 2^63-1, 894, 500, 428, -374)
```

**Fuzzer handling:** `guards.o31_desc_walk` in `select_shape_known` (the `keys` kind, and `max`'s `:order` form, on the walk positions). **The semi-join face (seven cov400 bundles):** the `:scan` twin's `f IN (SELECT f FROM t WHERE f >= :p)` still reads the class through its index inside the transaction. `_tx_known` now judges a semi-join's inner query under every position, `scan` included.
