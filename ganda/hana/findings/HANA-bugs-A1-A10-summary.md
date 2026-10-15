# SAP HANA: candidate engine bugs A1–A10

SAP HANA Cloud **4.00.000.00.1788859455** (2026-10-09 to 2026-10-13). Every repro below was re-run as written on 2026-10-13.

| id | data model | kind | one-line |
|----|------------|------|----------|
| A1 | RDF (SPARQL) | wrong value | `xsd:double` results are serialised to about 6 significant figures |
| A2 | Document Store | **crash** | A correlated scalar subquery over an `UNNEST` view raises an internal assertion |
| A3 | RDF (SPARQL) | wrong answer | `SUM`/`AVG` become unbound depending on unrelated (or deleted) triples |
| A4 | Document Store | error on valid query | `SUM` over a join that matches no row raises numeric overflow |
| A5 | Document Store | error on valid query | `BETWEEN ? AND ?` types string parameters as BIGINT; the equivalent `>= AND <=` works |
| A6 | RDF (SPARQL) | conformance | `FILTER`/`BIND` over a variable that no pattern binds is rejected |
| A7 | Graph (openCypher) | error on valid query | A vertex label in `MATCH` fails on a workspace with one vertex table and one edge table |
| A8 | Graph (openCypher) | wrong answer (silent) | DOUBLE literals are parsed to about 15 significant digits |
| A9 | Graph (openCypher) | wrong answer (silent) | A second `OPENCYPHER_TABLE` call in one statement reads the first call's parameter values |
| A10 | RDF (SPARQL) | wrong answer (silent) | A `FILTER` over `COALESCE` of an `OPTIONAL` variable drops or keeps the wrong solutions |


**Running the repros.** SQL runs as-is. SPARQL goes through
`CALL SYS.SPARQL_EXECUTE('<query>', 'Accept: application/sparql-results+json', ?, ?)`,
which takes an empty second argument for updates. Statements that bind values (`?`) need a client
such as `hdbcli`.

---

## A1: SPARQL serialises `xsd:double` to about 6 significant figures

**Summary.** HANA stores an `xsd:double` exactly, but every SPARQL result format rounds the numeric
term to about 6 significant figures. `STR(?d)` on the same binding returns all 17
digits, so the loss happens in the result serialiser. Impact: Any client that
reads doubles through SPARQL silently loses precision.

**Minimal repro (SPARQL)**
```sparql
PREFIX p: <urn:ex:p:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
INSERT DATA { GRAPH <urn:ex:a1> { <urn:ex:a> p:d "3.141592653589793"^^xsd:double } }

SELECT ?d                 WHERE { GRAPH <urn:ex:a1> { ?n p:d ?d } }   # -> "3.14159"
SELECT (STR(?d) AS ?s)    WHERE { GRAPH <urn:ex:a1> { ?n p:d ?d } }   # -> "3.1415926535897931"
```
**Expected:** `?d` round-trips to the same double (`3.141592653589793`).
**Actual:** `3.14159`. Likewise `0.30000000000000004` becomes `0.3` and `1.7976931348623157e+308` becomes `1.79769e+308`.

---

## A2: Document Store crashes on a correlated scalar subquery over an `UNNEST` view

**Summary.** A scalar subquery in `SELECT` that is correlated to the outer row, and whose `FROM`
reads a collection through `UNNEST`, aborts with an internal assertion (error code 3).

**Minimal repro (SQL)**
```sql
CREATE COLLECTION a2_pc;
INSERT INTO a2_pc VALUES('{"id": "a", "links": [{"to": "b"}]}');

WITH node AS (SELECT $."id" AS id  FROM a2_pc),
     link AS (SELECT $."id" AS src FROM a2_pc UNNEST $."links" AS "l")
SELECT s.id, (SELECT COUNT(*) FROM link p WHERE p.src = s.id) AS v FROM node s;
-- [3] fatal error: Execution flow must not reach here
```
**Controls:** with the subquery uncorrelated (`(SELECT COUNT(*) FROM link)`), the query returns `[('a', 1)]`. Correlated over a view without `UNNEST`, it also returns `[('a', 1)]`.

---

## A3: SPARQL `SUM`/`AVG` turn unbound once a predicate has held a second datatype

**Summary.** Take a group in which the aggregated variable is unbound for some rows (via
`OPTIONAL`). `SUM` and `AVG` skip the unbound value as long as the predicate has only ever held one
datatype. Once the same predicate IRI holds a second datatype **anywhere in the store**, they return
UNBOUND instead. That includes a resource the query never matches, another graph, and even a graph
that has since been dropped.

**Minimal repro (SPARQL)** (use fresh IRIs)
```sparql
PREFIX p: <urn:ex3:p:> PREFIX c: <urn:ex3:c:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
INSERT DATA { GRAPH <urn:ex3:g> {
  <urn:ex3:a> a c:n ; p:i "3"^^xsd:long .
  <urn:ex3:b> a c:n ; p:i "4"^^xsd:long .
  <urn:ex3:c> a c:n . } }

SELECT (SUM(?w) AS ?sum) (AVG(?w) AS ?avg) (MAX(?w) AS ?max) (COUNT(?w) AS ?count)
WHERE { GRAPH <urn:ex3:g> { ?n a c:n . OPTIONAL { ?n p:i ?w } } }
# -> sum 7, avg 3.5, max 4, count 2   (correct)

# add one unrelated triple: a different class, which the query never matches
INSERT DATA { GRAPH <urn:ex3:g> { <urn:ex3:x> a c:other ; p:i "text" . } }

# same SELECT again
# -> max 4, count 2; sum and avg UNBOUND
```
**Expected:** sum 7 and avg 3.5 both times. `SUM(COALESCE(?w, 0))` still returns 7.

---

## A4: Document Store SQL: `SUM` over a join that matches no row overflows

**Summary.** A `SUM` over a join of collection views fails with `[314] numeric overflow`, even
though the join returns no rows and the correct answer is `NULL`. The error appears only when the link view reads
the whole link (id, far end and value). If the view reads only the value, the query answers `NULL`.
The same query over column tables also answers `NULL`.

**Minimal repro (SQL)**
```sql
CREATE COLLECTION st;  CREATE COLLECTION mid;
INSERT INTO st  VALUES('{"id": "s1", "e1": []}');
INSERT INTO mid VALUES('{"id": "m1", "e2": [{"eid": "x1", "to": "t1", "w": 9223372036854775807},
                                            {"eid": "x2", "to": "t1", "w": 1}]}');

WITH v_mid    AS (SELECT $."id" AS id FROM mid),
     v_e1     AS (SELECT $."id" AS src_id, "l"."to" AS dst_id FROM st UNNEST $."e1" AS "l"),
     raw_v_e2 AS (SELECT "l"."eid" AS eid, $."id" AS src_id, "l"."to" AS dst_id, "l"."w" AS w
                  FROM mid UNNEST $."e2" AS "l"),
     v_e2     AS (SELECT eid, src_id, dst_id, TO_BIGINT(w) AS w FROM raw_v_e2)
SELECT SUM(p.v) FROM (
  SELECT j1.src_id AS strt, j2.w AS v
  FROM v_e1 j1 JOIN v_mid t1 ON t1.id = j1.dst_id JOIN v_e2 j2 ON j2.src_id = t1.id) p;
-- [314] numeric overflow       (the inner join alone returns [])
```
**Expected:** `NULL`.

---

## A5: Document Store field: `BETWEEN ? AND ?` types its parameters as BIGINT

**Summary.** On a collection field, `f BETWEEN ? AND ?` with string
parameters fails because HANA types the parameters as BIGINT. The field holds only strings.
`f >= ? AND f <= ?`, which is how SQL defines `BETWEEN`, works on the same field with the same
parameters, and so do `BETWEEN` with literals and `BETWEEN` on a column table. The failure does not
depend on the data.

To run repro, you need a client that binds parameters. 

**Minimal repro (SQL with bound parameters `['A', 'i']`)**
```sql
CREATE COLLECTION c;
INSERT INTO c VALUES('{"f": "abc"}');
INSERT INTO c VALUES('{"f": "xyz"}');

WITH raw AS (SELECT $."f" AS f FROM c) SELECT f FROM raw WHERE f BETWEEN ? AND ?;
-- [-10427] Conversion of parameter/column (1) from data type UCS2 (LE) to BIGINT failed

WITH raw AS (SELECT $."f" AS f FROM c) SELECT f FROM raw WHERE f >= ? AND f <= ?;
-- [('abc',)]
```
**Expected:** both forms return `abc`.

---

## A6: SPARQL rejects `FILTER`/`BIND` over a variable that no pattern binds

**Summary.** A query whose `FILTER` or `BIND` uses a variable that no triple pattern binds is
rejected. Under SPARQL 1.1, an unbound variable is a per-solution type error (§17.2, §18.2.1),
not a reason to reject the query. Wrapping the variable as `COALESCE(?x, 1)` makes HANA accept
the query.

**Minimal repro (SPARQL)**
```sparql
PREFIX p: <urn:ex6:p:>
INSERT DATA { GRAPH <urn:ex6:g> { <urn:ex6:a> p:id "a" . <urn:ex6:b> p:id "b" . } }

SELECT ?id WHERE { GRAPH <urn:ex6:g> { ?n p:id ?id . FILTER(!BOUND(?x)) } }
# [129] FILTER(s) contains unbound variables          (expected: a, b)

SELECT ?id WHERE { GRAPH <urn:ex6:g> { ?n p:id ?id . BIND(?x AS ?y) } }
# [129] Named variable not in contained WHERE clause  (expected: a, b)

SELECT ?id WHERE { GRAPH <urn:ex6:g> { ?n p:id ?id . FILTER(COALESCE(?x, 1) = 1) } }
# a, b   (accepted)
```

---

## A7: openCypher: a vertex label in `MATCH` fails on a one-vertex-table workspace

**Summary.** Take a graph workspace with exactly one vertex table and one edge table. A `MATCH`
pattern that names the vertex label, such as `(a:v)`, fails with a SQL error about an internal
column of the engine's rewrite (`CYPHER$TABLE$LABEL`). The edge label, the unlabelled
pattern and `LABEL(a)` (which even prints the label) all work. Adding a second vertex or edge table
makes the labelled pattern work too.

**Minimal repro (SQL)**
```sql
CREATE COLUMN TABLE v (id NVARCHAR(256) PRIMARY KEY, name NVARCHAR(100));
CREATE COLUMN TABLE e (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL);
INSERT INTO v VALUES ('m', 'M');  INSERT INTO v VALUES ('n', 'N');
INSERT INTO e VALUES ('f0', 'm', 'n');
CREATE GRAPH WORKSPACE w1
  VERTEX TABLE v KEY COLUMN id LABEL 'v'
  EDGE TABLE e KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id LABEL 'e';

SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE w1 QUERY 'MATCH (a:v) RETURN a.ID AS ID');
-- [260] invalid column name: a.CYPHER$TABLE$LABEL

SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE w1 QUERY 'MATCH (a)-[x:e]->(b) RETURN a.ID AS ID, b.ID AS V');
-- [('m', 'n')]
SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE w1 QUERY 'MATCH (a) RETURN a.ID AS ID, LABEL(a) AS L');
-- [('m', 'v'), ('n', 'v')]
```
**Expected:** `MATCH (a:v)` returns `m`, `n`. It does on a workspace with a second edge or vertex table.

---

## A8: openCypher parses DOUBLE literals to about 15 significant digits

**Summary.** A DOUBLE literal written into openCypher query text loses precision: a constant one or
two ulps away from a stored double compares `=` to it. Binding the same value as a parameter, or
writing it as `TOFLOAT('<digits>')`, compares exactly, matching HANA SQL. 

**Minimal repro (SQL)**
```sql
CREATE COLUMN TABLE rv  (id NVARCHAR(256) PRIMARY KEY, d DOUBLE);
CREATE COLUMN TABLE re1 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL);
CREATE COLUMN TABLE re2 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL);
INSERT INTO rv VALUES ('h', 0.5);
CREATE VIEW gv_rv  AS SELECT id, d AS rv_d FROM rv;
CREATE VIEW gv_re1 AS SELECT eid, src_id, dst_id FROM re1;
CREATE VIEW gv_re2 AS SELECT eid, src_id, dst_id FROM re2;
CREATE GRAPH WORKSPACE rw
  VERTEX TABLE gv_rv KEY COLUMN id LABEL 'rv'
  EDGE TABLE gv_re1 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_rv TARGET COLUMN dst_id REFERENCES gv_rv LABEL 're1'
  EDGE TABLE gv_re2 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_rv TARGET COLUMN dst_id REFERENCES gv_rv LABEL 're2';
-- (two edge tables avoid A7)

SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE rw QUERY
  'MATCH (a:rv) WHERE a.RV_D = 0.5000000000000002 RETURN a.ID AS ID');            -- ['h']  (wrong: 2 ulps away)
SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE rw QUERY
  'MATCH (a:rv) WHERE a.RV_D = TOFLOAT(''0.5000000000000002'') RETURN a.ID AS ID'); -- []     (correct)
SELECT id FROM rv WHERE d = 0.5000000000000002;                                -- []     (correct)
```
**Expected:** no row for the literal form, the same as the parameter, `TOFLOAT` and SQL forms.

---

## A9: openCypher: a second `OPENCYPHER_TABLE` call reads the first call's parameter values

**Summary.** When one SQL statement holds two parameterised `OPENCYPHER_TABLE` calls, the second
call uses the value bound to the first call's `?` instead of its own. Each call alone uses its own
value. With a SQL `?` before a call, the call uses that SQL parameter's value.

**Minimal repro (SQL with bound parameters)**
```sql
CREATE COLUMN TABLE v (id NVARCHAR(10) PRIMARY KEY, i BIGINT);
CREATE COLUMN TABLE e (eid NVARCHAR(10) PRIMARY KEY, src_id NVARCHAR(10) NOT NULL, dst_id NVARCHAR(10) NOT NULL);
INSERT INTO v VALUES ('a', 1);  INSERT INTO v VALUES ('b', 2);
CREATE GRAPH WORKSPACE w
  VERTEX TABLE v KEY COLUMN id
  EDGE TABLE e KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id;

-- one call alone: binds (1) -> [('a',)];  binds (2) -> [('b',)]
SELECT ID FROM OPENCYPHER_TABLE(GRAPH WORKSPACE w QUERY
  'MATCH (n) WHERE n.I = $p RETURN n.ID AS ID' PARAMETERS ('p' = ?));

-- two calls in one statement: binds (1, 2)
SELECT ID FROM OPENCYPHER_TABLE(GRAPH WORKSPACE w QUERY
  'MATCH (n) WHERE n.I = $p RETURN n.ID AS ID' PARAMETERS ('p' = ?))
UNION ALL
SELECT ID FROM OPENCYPHER_TABLE(GRAPH WORKSPACE w QUERY
  'MATCH (n) WHERE n.I = $q RETURN n.ID AS ID' PARAMETERS ('q' = ?));
-- [('a',), ('a',)]          (expected [('a',), ('b',)])

-- a plain SQL parameter before the call: binds ('z', 2)
SELECT TO_NVARCHAR(?) AS ID FROM DUMMY
UNION ALL
SELECT ID FROM OPENCYPHER_TABLE(GRAPH WORKSPACE w QUERY
  'MATCH (n) WHERE n.I = $p RETURN n.ID AS ID' PARAMETERS ('p' = ?));
-- [339] invalid number: not a valid number string 'z'   (expected [('z',), ('b',)])
```
**Expected:** each call reads its own `?`. In the second statement, the second call reads `1`
instead of `2`. In the third, the call reads `'z'`, the SQL parameter before it.

---

## A10: SPARQL `FILTER` evaluates `COALESCE` over an `OPTIONAL` variable wrongly

**Summary.** Take a `FILTER` that applies `COALESCE` to a variable bound only by an `OPTIONAL`.
HANA keeps or drops the wrong solutions, both where the variable is unbound and where it is bound.
`FILTER (COALESCE(?i, 0) = 0)` drops the solution where `?i` is unbound, and
`FILTER (COALESCE(?i > 1, true))` keeps a solution where `?i = 1`. The expression itself evaluates
correctly, when selected as a value it gives the expected answer.

**Minimal repro (SPARQL)**
```sparql
PREFIX p: <urn:ex10:p:> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
INSERT DATA { GRAPH <urn:ex10:g> { <urn:ex10:x> p:id "x" ; p:i "1"^^xsd:long . <urn:ex10:y> p:id "y" . } }

SELECT ?id (COALESCE(?i, 0) AS ?a) (COALESCE(?i > 1, true) AS ?b)
WHERE { GRAPH <urn:ex10:g> { ?n p:id ?id . OPTIONAL { ?n p:i ?i } } }
# x 1 false, y 0 true   (correct)

SELECT ?id WHERE { GRAPH <urn:ex10:g> { ?n p:id ?id . OPTIONAL { ?n p:i ?i } FILTER (COALESCE(?i, 0) = 0) } }
# []          (expected y)
SELECT ?id WHERE { GRAPH <urn:ex10:g> { ?n p:id ?id . OPTIONAL { ?n p:i ?i } FILTER (COALESCE(?i > 1, true)) } }
# x, y        (expected y)
```
**Expected:** `y` for both filters, matching the values the first query selects.
