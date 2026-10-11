# A7 — openCypher: a homogeneous workspace with LABEL clauses fails every labelled query

**Bucket:** A (candidate engine defect — an internal column of the engine's rewrite leaks into
the SQL it generates; the query is legal and documented).
**Version:** HANA Cloud 4.00.000.00.1788859455. **Repro:** `python3 -I repro.py`
**Found by:** the Ganda SAP HANA probe (fact G27), 2026-10-11, while probing the graph data
model's workspace forms. A campaign's workspace always has two or more edge tables, so the
fuzzer itself never meets the shape.

## Claim
A graph workspace of ONE vertex table and ONE edge table (homogeneous), created with `LABEL`
clauses, refuses any openCypher query that names a label -- `(a:pv3)`, `[e:pe3]` -- with a SQL
error about a column no user wrote:

```
[260] invalid column name: a.CYPHER$TABLE$LABEL: line 3 col 67 (at pos 238)
```

The same workspace answers the unlabelled form of the query, a workspace without `LABEL`
clauses answers, and a heterogeneous workspace (a second edge table) answers the labelled
query. `LABEL` is documented for every graph table ("For each graph table, an optional label can
be specified ... The label can be used later in both GraphScript and openCypher", Property Graph
Engine Reference, QRC 2/2026, CREATE GRAPH WORKSPACE); nothing restricts it to heterogeneous
workspaces.

## Actual (the probe's run, 2026-10-11; `ganda hana probe G27`)
Scratch schema; `pv3(id NVARCHAR(256) NOT NULL, f1 BIGINT)` hash-partitioned, `pe3(eid PK,
src_id NOT NULL, dst_id NOT NULL)`; views `gv_pv3(id, f1 AS pv3_f1)`, `gv_pe3(eid, src_id,
dst_id)`; one edge `m -> n`.

```
CREATE GRAPH WORKSPACE gw4 VERTEX TABLE gv_pv3 KEY COLUMN id LABEL 'pv3'
    EDGE TABLE gv_pe3 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_pv3 TARGET COLUMN dst_id REFERENCES gv_pv3 LABEL 'pe3'
MATCH (a:pv3)-[e:pe3]->(b:pv3) RETURN a.ID AS ID, b.ID AS V
    : [260] invalid column name: a.CYPHER$TABLE$LABEL: line 3 col 67 (at pos 238)
MATCH (a)-[e]->(b) RETURN a.ID AS ID, b.ID AS V
    : answers

CREATE GRAPH WORKSPACE gw5 VERTEX TABLE gv_pv3 KEY COLUMN id
    EDGE TABLE gv_pe3 KEY COLUMN eid SOURCE COLUMN src_id REFERENCES gv_pv3 TARGET COLUMN dst_id REFERENCES gv_pv3
MATCH (a)-[e]->(b) RETURN a.ID AS ID, b.ID AS V
    : answers

(G17, control) the same with a second edge table gv_pe4 LABEL 'pe4':
MATCH (a:pv3)-[e:pe3]->(b:pv3) RETURN a.ID AS ID, b.ID AS V
    : [('m', 'n')]
```

The first probe run met the same error twice more: a homogeneous labelled workspace created
`NO CACHE` over the campaign's own views (`gv_pv1` / `gv_pe2`), and the hash-partitioned case
above -- so neither the cache option nor the partitioning is the trigger; the labels on a
homogeneous workspace are.

`repro.py` lays the shape out as a matrix -- tables vs views; `LABEL` on both tables, on the
vertex table only, on the edge table only, on neither; a vertex label, an edge label, both,
none, `LABEL(a)` in RETURN; heterogeneous controls by a second edge table and by a second
vertex table. **Its output is not captured yet** (the machine holding the tunnel was
unreachable when the script was written); the claim above rests on the probe's three
observations and the G17 control only.

## How to judge
- The query is legal: `(a:label)` and `[e:label]` are the documented MATCH forms, and the labels
  exist (the workspace declared them). Error 260 names `CYPHER$TABLE$LABEL`, a column of the
  engine's internal rewrite (the label column it synthesises for a heterogeneous workspace), so
  this is a code path missing for the one-table case, not a user mistake and not a documented
  limitation.
- It is not a clean "feature not supported" (code 7): the generated SQL fails to compile.
- Nothing is lost silently: the statement fails. Impact is a workspace of one vertex and one edge
  table -- the tutorial shape (`FLIGHTS` with `NODES` / `EDGES`) -- that cannot be queried by
  label once labelled; the workaround is to drop the `LABEL` clauses (and the labels from the
  queries) or to add a second table.

## What the provider does
Nothing: a campaign's workspace has one view per node class and per edge type (`mapping/graph.py`),
two or more edge tables always (`GeneratorConfig.min_edge_types = 2`), so it is heterogeneous.
Recorded in `RULES.md`, "The graph data model".
