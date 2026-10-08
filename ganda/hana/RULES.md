# SAP HANA: the engine rules the oracle rests on

Every rule below is something the provider's renderings or its reference semantics
(`semantics.py`) assume. **None has been checked against a live HANA yet.** The status
column says where each comes from:

- **doc**: SAP's documentation, QRC 3/2026 (SQL Reference, JSON Document Store Guide,
  SPARQL Reference, Knowledge Graph Guide);
- **assumed**: the SQL standard / SPARQL 1.1, not stated by SAP for HANA;
- **probe Xn**: `ganda hana probe` checks it (`scripts/probe.py`).

When a probe fact fails, change the provider, not the probe: see `HANDOFF.md`, "When a
probe fact fails".

## The three data models

| Rule | Status |
|---|---|
| A campaign database is a SCHEMA (tables, collections) plus a NAMED GRAPH `<urn:ganda:g:<schema>>`. Graphs belong to the user, not to a schema. | doc |
| A query with no FROM / GRAPH reads every graph (HANA differs from SPARQL 1.1 here): every rdf text names its graph, `GRAPH <g> {...}` or `FROM <g>`. | doc |
| `CREATE GRAPH` / `DROP GRAPH` need auto-commit on; an `INSERT DATA` into a missing graph creates it. | doc |
| SPARQL updates join the SQL transaction (COMMIT / ROLLBACK cover both). | doc, probe K15 |
| `SYS.SPARQL_EXECUTE(text, headers, ?, ?)` through `callproc`; the response is OUT parameter 3; `Accept: application/sparql-results+json` gives SPARQL JSON results; an update answers nothing. | doc + langchain-hana source, probe K0/K1 |
| `SPARQL_TABLE('<one SELECT>')` answers one SQL column per variable, typed after the triple store's datatype (NVARCHAR when a variable mixes types; a string over 5000 characters becomes NULL). | doc, probe K12/K13 |

## Values

| Rule | Status |
|---|---|
| BIGINT keeps the 64-bit range; DOUBLE keeps DBL_MAX; `-0.0` is stored as `+0.0` (the pools hold no `-0.0`). | doc, probe R1 |
| NVARCHAR holds at most 5000 characters; it compares in binary order (UTF-16 code units, CESU-8 storage), without padding (`'a' <> 'a '`); `''` is not NULL. | doc (length), assumed (order), probe R3/R4 |
| SPARQL compares strings by code point; on the Basic Multilingual Plane that is UTF-16 order, so the pools hold no astral code point. | assumed (SPARQL 1.1) |
| An integer is stored in the triple store as `xsd:long` (`xsd:integer` is not among the documented types), a double as `xsd:double`, a boolean as `xsd:boolean`. | doc, probe K1/K2 |
| The document store keeps a JSON integer as BIGINT and a number with a fraction or an exponent as DECIMAL (collections created since QRC 3/2024); it compares integer and number as one type; it never casts implicitly (a string `'10'` does not equal the number `10`). | doc, probe D1/D4/D5 |
| A field projected from an NVARCHAR collection reaches SQL as text: a number as its digits, a string unquoted, a boolean as `true` / `false`, an array as its JSON text, a JSON null as SQL NULL. | doc (text), assumed (the boolean's and the number's exact text), probe D2 |
| A parameter in a collection filter must be cast (`CAST(? AS BIGINT|NVARCHAR|DOUBLE|DECIMAL|BOOLEAN)`); a bare `= ?` is refused. A boolean is compared with `TO_JSON_BOOLEAN(TRUE)` in an NVARCHAR collection. | doc, probe D4/D5/D6 |
| A LIST is held as its canonical JSON text (`[1,2]`) plus its elements (a child table on rel, `i:` triples on rdf, the JSON array on doc). | the provider's design |

## Nulls and logic

| Rule | Status |
|---|---|
| An absent field and an explicit null are one null on every data model (`IS NULL` holds for both on doc). | doc (doc), assumed (rel: both NULL; rdf: no triple), probe D7 |
| SQL is three-valued; a record is kept only where the filter is TRUE. | assumed (SQL) |
| The rdf base renderings spell three-valued logic out with `BOUND` guards; the `native` variants rely on SPARQL's error propagation (`error && false` is false). | assumed (SPARQL 1.1), probe K5/K6 |
| `FOR ANY ... SATISFIES ... END` is false (not unknown) over an absent array, so its negation holds; the doc filter never negates one anyway (`document.doc_pred` raises and the question is asked through the views). | assumed, probe D8 |
| SQL `IN (SELECT ...)` is rendered with `g IS NOT NULL` inside, so a semi-join is unknown only for a null field. | the provider's design |

## Query forms

| Rule | Status |
|---|---|
| `ORDER BY ... NULLS FIRST / NULLS LAST`, `LIMIT n OFFSET m` (an OFFSET only after a LIMIT: `LIMIT 2147483647 OFFSET m`). NULL sorts first ascending when no NULLS clause is given. | doc (syntax), assumed (default), probe R5/R6 |
| The document store orders `null < number < string < object < boolean < array`, so a collection ORDER BY puts nulls (and absent keys) first ascending, last descending, without a NULLS clause. | doc, probe D12 |
| AVG of BIGINT is the exact quotient; SUM of BIGINT is exact below 2^63; MEDIAN is an aggregate in SQL and in HANA's SPARQL. | assumed (AVG, SUM), doc (MEDIAN), probe R7/R8/R9/K16 |
| SPARQL aggregates skip an unbound value; `SUM` / `AVG` of nothing are 0 in SPARQL (the renderings guard them with `IF(COUNT(?w) > 0, ...)`); `COUNT` over nothing is 0. | assumed (SPARQL 1.1), probe K7/K8 |
| A grouped SPARQL query over no solution answers no row; the renderings add `HAVING (COUNT(*) > 0)` (a no-op by the standard) because rdflib answers one empty group. | assumed, probe K9 |
| SPARQL property paths `?`, `/`, `^`, `|` (no `{n,m}`); MINUS; FILTER [NOT] EXISTS; subqueries; IF; STRDT. | doc, probe K10/K11/K14 |
| An `EXISTS` inside `GRAPH` in an update's WHERE reads the named graph (the rdf write marks its records with one). rdflib does not, so the offline stand-in selects the marks with a query. | assumed, probe K19 |
| The collection SELECT filter: a path compared with a cast value, `IS [NOT] NULL`, `LIKE`, `FOR ANY`, AND / OR, parentheses. `IN` is rendered as ORs, `BETWEEN` as two comparisons, `NOT` pushed down to the leaves. | doc (grammar), assumed (parentheses, NOT LIKE) |
| The collection UPDATE / DELETE filter is narrower: comparisons, BETWEEN, IS NULL, AND / OR. A doc write whose filter is outside it chooses its records with a SELECT first and writes them by id (`concrete_syntax/write.py`, `_doc`). UPDATE takes no arithmetic: an arithmetic write reads the value, shifts it and writes it back. | doc |
| `UPDATE c SET $."f" = PARSE_JSON(?) [, ...] UNSET $."g" WHERE ...`; `SET c.$ = PARSE_JSON(?)` replaces a document; `UPDATE` and `DELETE` answer their row count. | doc (forms), assumed (PARSE_JSON of a parameter), probe D13/D14/D15 |
| `UNNEST $."arr" AS "x"` (chained: `UNNEST "l"."arr" AS "y"`) skips an absent array; an element's field is `"l"."f"`; a WITH view over collections joins with SQL. | doc, probe D9/D10/D16 |
| Column tables: `UPSERT t (cols) VALUES (...) WHERE id = ?`; `MERGE INTO t USING (...) s ON ... WHEN MATCHED THEN UPDATE SET ...`; `MERGE DELTA OF t`; `CREATE [UNIQUE] INDEX` (an INVERTED VALUE index; INVERTED HASH / INDIVIDUAL exist for composite UNIQUE constraints only, so the provider draws neither); a UNIQUE index admits several NULLs; `PARTITION BY HASH (id) PARTITIONS n`. | doc, probe R12/R13/R14 |

## Error classes (`connection.py`)

| Code | Read as |
|---|---|
| 613 (execution aborted by timeout) | a timeout (`EngineError.timeout`) |
| 2, 3, 129, 2048 (general, fatal, rolled back by an internal error, column store error) | an INTERNAL error: a finding on its own. To refine: a 2048 that wraps an ordinary rejection would be noise |
| -10709, -10807, -10108, -10821, -10806 (client: connection lost) | a crash (`EngineCrash`) |
| everything else (257 syntax, 301 unique, 339 invalid number, ...) | a rejection, compared across variants |
