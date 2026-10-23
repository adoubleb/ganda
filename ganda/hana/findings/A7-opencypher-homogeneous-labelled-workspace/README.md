# A7 — openCypher: a vertex label in MATCH fails over a workspace with one vertex table

**Bucket:** A (candidate engine defect — an internal column of the engine's rewrite leaks into
the SQL it generates; the query is legal and documented).
**Version:** HANA Cloud 4.00.000.00.1788859455. **Repro:** `python3 -I repro.py`
**Found by:** the Ganda SAP HANA probe (fact G27), 2026-10-11, while probing the graph data
model's workspace forms; the matrix below run 2026-10-12. A campaign's workspace always has
two or more edge tables, so the fuzzer itself never meets the shape.

## Claim
Over a graph workspace of ONE vertex table and ONE edge table (homogeneous), a MATCH pattern
that names the vertex label -- `(a:v)`, whether the label was declared by a `LABEL` clause or
is the default table name, over tables or over views -- fails with a SQL error about a column
no user wrote:

```
[260] invalid column name: a.CYPHER$TABLE$LABEL: line 3 col 8 (at pos 84)
```

Everything around it answers: the edge label `[x:e]`, `LABEL(a)` in RETURN (which prints the
very label the pattern cannot name), the unlabelled pattern, and the same labelled pattern
over any heterogeneous workspace -- a second vertex table OR merely a second edge table.
The vertex label is a documented MATCH form (`(a:vertex_label)`, Property Graph Engine
Reference QRC 2/2026, MATCH clause) and `LABEL` is documented for every graph table ("If no
label is specified, the label defaults to the corresponding vertex or edge table name");
nothing restricts either to heterogeneous workspaces.

## Steps to reproduce
Plain SQL, in a scratch schema.

1. One vertex table and one edge table, with the edge `m -> n`:
   ```sql
   CREATE SCHEMA A7_REPRO;
   SET SCHEMA A7_REPRO;
   CREATE COLUMN TABLE v (id NVARCHAR(256) PRIMARY KEY, name NVARCHAR(100));
   CREATE COLUMN TABLE e (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL);
   INSERT INTO v VALUES ('m', 'M');
   INSERT INTO v VALUES ('n', 'N');
   INSERT INTO e VALUES ('f0', 'm', 'n');
   ```
2. A homogeneous workspace over them:
   ```sql
   CREATE GRAPH WORKSPACE w1
     VERTEX TABLE v KEY COLUMN id LABEL 'v'
     EDGE TABLE e KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id LABEL 'e';
   ```
3. Match by the vertex label -> **`[260] invalid column name: a.CYPHER$TABLE$LABEL`** (bug;
   expected `m`, `n`):
   ```sql
   SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE w1 QUERY 'MATCH (a:v) RETURN a.ID AS ID');
   ```
4. Controls over the same workspace: `'MATCH (a) RETURN a.ID AS ID, LABEL(a) AS L'` -> `(m, v), (n, v)`
   (the label the pattern cannot name); `'MATCH (a)-[x:e]->(b) RETURN a.ID AS ID, b.ID AS V'` -> `(m, n)`.
5. Control: add a second edge table and the vertex label answers:
   ```sql
   CREATE COLUMN TABLE e2 (eid NVARCHAR(256) PRIMARY KEY, src_id NVARCHAR(256) NOT NULL, dst_id NVARCHAR(256) NOT NULL);
   INSERT INTO e2 VALUES ('f1', 'n', 'm');
   CREATE GRAPH WORKSPACE w6
     VERTEX TABLE v KEY COLUMN id LABEL 'v'
     EDGE TABLE e KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id LABEL 'e'
     EDGE TABLE e2 KEY COLUMN eid SOURCE COLUMN src_id TARGET COLUMN dst_id LABEL 'e2';
   SELECT * FROM OPENCYPHER_TABLE(GRAPH WORKSPACE w6 QUERY 'MATCH (a:v) RETURN a.ID AS ID');   -- m, n
   ```
6. Clean up: `DROP SCHEMA A7_REPRO CASCADE;`

Reproduced on 4.00.000.00.1788856605 (2026-10-23).

## Actual (`repro.py`, 2026-10-12)
Scratch schema; `v(id PK, name)`, `v2`, `e(eid PK, src_id, dst_id)` with `m -> n`, `e2` with
`n -> m`; views `gv_v`, `gv_e`. Seven workspaces, five queries each.

```
w1 tables, LABEL clauses
     vertex label   : [260] invalid column name: a.CYPHER$TABLE$LABEL: line 3 col 8 (at pos 84)
     edge label     : OK [('m', 'n')]
     both labels    : [260] invalid column name: a.CYPHER$TABLE$LABEL: line 3 col 67 (at pos 220)
     no label       : OK [('m', 'n')]
     LABEL(a)       : OK [('m', 'v'), ('n', 'v')]
w2 tables, no LABEL clauses            (labels V / E, the table names)
     vertex label   : [260] invalid column name: a.CYPHER$TABLE$LABEL: line 3 col 8 (at pos 84)
     edge label     : OK [('m', 'n')]
     both labels    : [260] invalid column name: a.CYPHER$TABLE$LABEL: line 3 col 67 (at pos 220)
     no label       : OK [('m', 'n')]
     LABEL(a)       : OK [('m', 'V'), ('n', 'V')]
w3 views, LABEL clauses
     vertex label   : [260] invalid column name: a.CYPHER$TABLE$LABEL: line 3 col 8 (at pos 87)
     edge label     : OK [('m', 'n')]
     both labels    : [260] invalid column name: a.CYPHER$TABLE$LABEL: line 3 col 67 (at pos 229)
     no label       : OK [('m', 'n')]
     LABEL(a)       : OK [('m', 'v'), ('n', 'v')]
w4 tables, vertex LABEL only           (same as w1: fails)
w5 tables, edge LABEL only             (same as w2: fails)
w6 two edge tables, LABELs
     vertex label   : OK [('m',), ('n',)]
     edge label     : OK [('m', 'n')]
     both labels    : OK [('m', 'n')]
     no label       : OK [('m', 'n'), ('n', 'm')]
     LABEL(a)       : OK [('m', 'v'), ('n', 'v')]
w7 two vertex tables, LABELs
     vertex label   : OK [('m',), ('n',)]
     edge label     : OK [('m', 'n')]
     both labels    : OK [('m', 'n')]
     no label       : OK [('m', 'n')]
     LABEL(a)       : OK [('m', 'v'), ('n', 'v'), ('p', 'v2')]
```
(w4 and w5 print the same five lines as w1 and w2; `TRANSCRIPT.txt` has the full run.) The
probe (G27, 2026-10-11) met the same error over a view of a hash-partitioned table and over a
`NO CACHE` workspace: neither is the trigger.

## How to judge
- The query is legal and the label exists: `LABEL(a)` returns it in the same workspace. Error
  260 names `CYPHER$TABLE$LABEL`, the label column the engine synthesises for a heterogeneous
  workspace (w6 / w7 show the path that works); for one vertex table the vertex-label filter
  references it without materialising it. A missing code path, not a user mistake and not a
  documented limitation.
- Not a clean "feature not supported" (code 7): the generated SQL fails to compile.
- Nothing is silently wrong: the statement fails. Impact: the simplest workspace -- the
  documentation's own `FLIGHTS` shape, one `NODES` and one `EDGES` table -- cannot be queried
  by vertex label at all. Workarounds: leave the vertex label out of the pattern (one table:
  it filters nothing), or add a second table.

## What the provider does
Nothing: a campaign's workspace has one view per node class and per edge type (`mapping/graph.py`),
two or more edge tables always (`GeneratorConfig.min_edge_types = 2`), so it is heterogeneous.
Recorded in `RULES.md`, "The graph data model".
