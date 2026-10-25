# O41 -- `SELECT DISTINCT` answers a row twice once the dedup set's hash bin treeifies

**Version:** OrientDB 3.2.56. Found by the cov400 campaign (seeds 5007, 5025, 5082, 5135, 5154): `node_distinct_values`, the select IR's `distinct` kind and `node_agg`'s `distinct_count` (a `count(*)` over a DISTINCT subquery) answered one or more values twice. Every database answered alike except where the rows arrived in another order (5154: the graph and rel sides doubled `2^31`, the doc side did not).

```sql
CREATE CLASS t CLUSTERS 1; CREATE PROPERTY t.v LONG;
-- eleven distinct multiples of 2^32 + 1 (every one hashes to 0), then 0 once more
INSERT INTO t (v) VALUES (0), (4294967297), (8589934594), (12884901891), (17179869188), (21474836485),
                         (25769803782), (30064771079), (34359738376), (38654705673), (42949672970), (0);
SELECT count(*) FROM (SELECT DISTINCT v FROM t);   -- 12: 0 twice
SELECT count(*) FROM (SELECT v FROM t GROUP BY v); -- 11
SELECT set(v).size() FROM t;                       -- 11
```

This is the smallest form: the bin treeifies at its tenth entry, ten distinct values never duplicate, and the repeat has to arrive after the eleventh. `CLUSTERS 1` makes the scan order the insert order -- a default class has a cluster per core, the rows are spread round-robin and the scan interleaves them, so the earlier 40-row repro (`64j` / `-64j-1` twice) answered 24 rows on an 8-core machine and 20 with one cluster: which rows double depends on the order they arrive in.


**Cause (3.2.56 source).** `DistinctExecutionStep` dedups projected rows through a `HashSet<OResult>`. `OResultInternal` implements `Comparable<OResultInternal>`, and its `compareTo` returns `1` whenever either row is not a record (`x.compareTo(x) == 1`), which breaks the `Comparable` contract. `HashMap` uses `compareTo` to search a TREE bin: a bin is treeified once it holds 9 entries in a table of at least 64. Among entries of equal hash, the search follows `compareTo`, always to the right, and misses an equal row stored on the left. The row is then added and emitted again. `equals` / `hashCode` are consistent, so a chained bin and GROUP BY (keyed by value lists) are right.

- **Affected:** any DISTINCT over projected rows (a record-valued row dedups by RID) where two distinct rows share a full hash and their bin fills. The hashes collide in structured ways: `Long.hashCode` is `v ^ v >>> 32`, so `v` and `-v-1` collide for small `v`; `null`, `0` and `-1` all hash 0; `2^31`, `2^63-1` and `Long.MIN_VALUE` all hash `MIN_VALUE`. Multiples of 64 share the low bits that pick the bin, and so do dyadic floats.
- **Not affected:** `GROUP BY`, `set()`, `count(distinct())`'s own error aside (O33).

**Fuzzer handling:** `fuzzer/java_hash.py` models the JDK hashes of the projected rows (Long, Integer, Double, Float, BigDecimal from `valueOf(double)`, String, Boolean, Date, lists and maps of Longs). `distinct_collides` flags a DISTINCT whose rows hold two distinct rows of equal hash in a bin of at least 9 rows, at any table size that holds at least half the rows. `guards.o41_values` withholds every side of `node_distinct_values`, the DISTINCT sides of `node_agg distinct_count` (`:set` stays) and the select IR's `distinct` kind and `distinct_count` base form. Facts: `scripts/probe_osql.py`, "the cov400 triage". Upstream draft: `upstream/O41-distinct-returns-duplicate-rows.md`.

## Report Status
Reported as 10896. Confirmed.