"""SAP HANA's vocabulary for the abstract model (`ganda.core.data.model`): the declared type
of each `FieldType` in a column table, how a value is stored in a JSON collection and as
RDF terms, the IRIs of the triple store, the index kinds a schema may name, and the
write forms a setup may take. `ganda.core.data.model` imports nothing from this module.

Four data models of one HANA Cloud database (`RULES.md` holds the facts each rests on):

- `doc`: the JSON Document Store. One COLLECTION per node class; a document holds `id`,
  its fields and, per edge type leaving its class, an array of link objects
  `{"eid": ..., "to": <target id>, <attributes>}` -- the relationships embedded in the
  source, as a document model keeps them.
- `rel`: column tables. One table per node class (`id` and a column per field) and
  one JUNCTION table per edge type (`eid`, `src_id`, `dst_id`, a column per attribute).
- `graph`: the Property Graph Engine. The rel data model's tables again, read through
  one view each (the columns under the workspace's attribute names, `graph_attr`) by
  ONE GRAPH WORKSPACE (`GRAPH_WORKSPACE`): a vertex table per node class, an edge table
  per edge type, labelled by the logical names. Asked in openCypher through SQL's
  `OPENCYPHER_TABLE` (`opencypher_table`); written in SQL, as the graph has no write of
  its own.
- `rdf`: the Knowledge Graph Engine. One named graph per database. A node is an IRI
  with its class, its `id` literal and a literal per present field; a relationship is a
  resource of its own (`src`, `dst`, `eid` and its attributes: RDF has no multigraph,
  so a parallel edge needs a node of its own) plus one DIRECT link triple `src l:e dst`
  for the property paths, where set semantics is what the question wants.

A LIST of integers has no native column type that every data model compares alike, so
every data model holds two things: the list's CANONICAL JSON text (`[1,2,3]`, no
spaces) for whole-list equality and projection, and its elements one per row/triple for
membership (`CONTAINS`): a child table `<table>__<field>(owner, pos, v)` on `rel`, an
`i:<field>` triple per element on `rdf`; the JSON array itself on `doc`.

An absent field and an explicit null are ONE thing on every data model here (a SQL
NULL, a missing key, a missing triple): the null tests are `IS NULL` / `IS NOT NULL`
only, and the state check reads both as null.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from urllib.parse import quote

from ganda.core.data.model import FieldType

# --- column tables ---------------------------------------------------------------------------

# NVARCHAR holds at most 5000 characters (documented); a longer string is an NCLOB,
# which takes no comparison and no index, so the provider's strings stay under it
NVARCHAR_MAX = 5000
ID_TYPE = "NVARCHAR(256)"

SQL_TYPES = {
    FieldType.INT: "BIGINT",
    FieldType.FLOAT: "DOUBLE",
    FieldType.STRING: f"NVARCHAR({NVARCHAR_MAX})",
    FieldType.BOOL: "BOOLEAN",
    # the canonical JSON text; the elements live in the child table
    FieldType.LIST: f"NVARCHAR({NVARCHAR_MAX})",
}
SUPPORTED_TYPES = tuple(SQL_TYPES)


def child_table(table: str, field_name: str) -> str:
    """The child table holding a LIST column's elements, one row per element."""
    return f"{table}__{field_name}"


def list_text(v) -> "str | None":
    """A list's canonical JSON text (the form `=` compares and a projection reads)."""
    return None if v is None else json.dumps(v, separators=(",", ":"))


# --- index kinds ----------------------------------------------------------------------------

# `CREATE [UNIQUE] INDEX` on a column table (an INVERTED VALUE index, the default);
# `UNIQUE` rejects a second row with the key (several NULLs are allowed, documented)
INDEX_KINDS = ("INDEX", "UNIQUE")
UNIQUE_KINDS = ("UNIQUE",)
# the kind of the index on `id` per data model (`Schema.id_index`): a missing data
# model means "UNIQUE", rendered as the table's PRIMARY KEY
ID_INDEX_KINDS = ("UNIQUE_INDEX", "INDEX", "none")
DEFAULT_ID_INDEX = "UNIQUE"

# --- setup write forms (`WriteForm`, `physical_design.draw_write_form`) ---------------------

# how a row is inserted: one INSERT ... VALUES per row, an `INSERT ... SELECT ... FROM
# DUMMY`, or the driver's batch (`executemany`) of one prepared INSERT
INSERT_FORMS = ("values", "select", "batch")
# how a NodeUpdate's final values are written: UPDATE, UPSERT by key, or MERGE INTO
UPDATE_FORMS = ("set", "upsert", "merge")
# auto-commit per statement, or every data step in one transaction committed at the end
TRANSACTION_FORMS = ("auto", "tx")
EDGE_UPDATE_FORMS = ("set", "delete_insert")

# --- the triple store -------------------------------------------------------------------------

NS = "urn:ganda:"
PREFIXES = {
    "c": NS + "c:",  # a class
    "p": NS + "p:",  # a field / attribute / the logical id / an edge's endpoints
    "i": NS + "i:",  # a LIST field's element
    "l": NS + "l:",  # the direct link of an edge type
}
PREFIX_TEXT = " ".join(f"PREFIX {k}: <{v}>" for k, v in PREFIXES.items()) + " PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>"
# the graph a rendered text names; the session rebinds it to its database's graph
GRAPH_TOKEN = "<urn:ganda:graph>"
# the session's schema name, where a text must spell it (GraphScript's `Graph("SCHEMA",
# "GWS")`): replaced by `connection.HanaSession._bind_graph` with the database's
SCHEMA_TOKEN = "<urn:ganda:schema>"
XSD = "http://www.w3.org/2001/XMLSchema#"


def graph_iri(database: str) -> str:
    return f"<urn:ganda:g:{database}>"


def record_iri(record_id: str) -> str:
    """A node's or a relationship's IRI: its logical id, percent-encoded (a custom key
    may hold any character)."""
    return f"<{NS}r:{quote(record_id, safe='/')}>"


def sparql_string(s: str) -> str:
    """A SPARQL string literal: the ECHAR escapes for the quote, the backslash and the
    line breaks, a UCHAR for any other control character (never for the quote: a
    codepoint escape is decoded before the text is parsed)."""
    out = []
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def sparql_literal(v) -> str:
    """A Python value as the RDF literal the triple store holds for it: an integer as
    `xsd:long` (the integer type the engine documents; `xsd:integer` is not among
    them), a float as `xsd:double` (its shortest repr), a string as `xsd:string`, a
    bool as `xsd:boolean`, a list as its canonical JSON text."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f'"{v}"^^xsd:long'
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError(f"no RDF literal for {v!r}")
        return f'"{v!r}"^^xsd:double'
    if isinstance(v, str):
        return sparql_string(v)
    if isinstance(v, list):
        return sparql_string(list_text(v))
    raise TypeError(f"no RDF literal for {v!r}")


def from_rdf_term(term: dict):
    """A SPARQL JSON result term as a Python value (`None` for an unbound variable)."""
    if term is None:
        return None
    kind = term.get("type")
    value = term.get("value")
    if kind == "uri":
        return value
    dt = term.get("datatype", XSD + "string")
    if dt in (XSD + "integer", XSD + "long", XSD + "int", XSD + "short", XSD + "byte", XSD + "nonNegativeInteger", XSD + "unsignedLong", XSD + "unsignedInt"):
        return int(value)
    if dt in (XSD + "double", XSD + "float"):
        return float(value)
    if dt == XSD + "decimal":
        return number(Decimal(value))
    if dt == XSD + "boolean":
        return value in ("true", "1")
    return value


def number(d: Decimal):
    """A DECIMAL answer (an aggregate's) as the model's number: an integral value as an
    int, any other as the nearest double."""
    if d == d.to_integral_value():
        return int(d)
    return float(d)


# --- the property graph engine ---------------------------------------------------------------

# the GRAPH WORKSPACE of a graph database: one per campaign schema, unqualified (it
# resolves in the session's current schema, as the tables do)
GRAPH_WORKSPACE = "gw"
GRAPH_VIEW_PREFIX = "gv_"
# the workspace's caching strategy (`physical_design.graph_cache`): the default dynamic
# cache (no clause) or NO CACHE
GRAPH_CACHE_FORMS = ("dynamic", "none")
# where `Schema.id_index` keeps the drawn cache form: a namespaced key, never a data model
GRAPH_CACHE_KEY = "graph:cache"
GRAPH_KEYS = ("id", "eid", "src_id", "dst_id")


def graph_view(table: str) -> str:
    """The view of a node or junction table the workspace reads: the table's columns
    under the workspace's attribute names."""
    return GRAPH_VIEW_PREFIX + table


def graph_label(table: str) -> str:
    """A vertex or edge table's label in the workspace: the logical name (`LABEL 'n1'`,
    matched by `(a:n1)`; a label is a case-sensitive string, not an identifier)."""
    return table


def graph_attr(table: str, field: str, shared: bool = False) -> str:
    """A column's attribute name in the workspace, as a cypher text spells it. The
    workspace wants ONE type per attribute name across its vertex tables (and across its
    edge tables), and a class's own `f1` is drawn per class, so an own field is qualified
    `<table>_<field>`; the keys and a base-class field (one type everywhere) keep their
    names. Uppercase: the DDL is unquoted, so the catalog uppercases the names, and an
    openCypher attribute name is case-sensitive."""
    if shared or field in GRAPH_KEYS:
        return field.upper()
    return f"{table}_{field}".upper()


def graph_columns(schema, table: str) -> list:
    """[(physical column, attribute name)] of a node table (`id`, then its query fields)
    or a junction table (`eid`, `src_id`, `dst_id`, then the attributes), in the order
    the view and every cypher projection of the table name them."""
    if any(et.name == table for et in schema.edge_types):
        keys, fields, shared = ["eid", "src_id", "dst_id"], schema.edge_type(table).fields, set()
    else:
        keys, fields = ["id"], schema.query_fields(table)
        shared = {f.name for f in schema.base.fields} if schema.base is not None else set()
    return [(k, k.upper()) for k in keys] + [(f.name, graph_attr(table, f.name, f.name in shared)) for f in fields]


def param_expr(name: str, sql_type: "str | None" = None) -> str:
    """The SQL expression a `PARAMETERS ('name' = ...)` entry binds a cypher `$name`
    from: the bare `:name` marker (typed by the engine), or a CAST to `sql_type`."""
    return f":{name}" if sql_type is None else f"CAST(:{name} AS {sql_type})"


def opencypher_table(query: str, params: "dict | None" = None) -> str:
    """An openCypher query as SQL's `OPENCYPHER_TABLE` table function over the database's
    workspace. The query is a SQL string LITERAL (its quotes doubled; the function takes
    no expression there); `params` maps each `$name` the query uses to the SQL expression
    binding it (`param_expr`), written in the order given. `connection.positional` leaves
    the literal alone and binds the `:name` markers of the PARAMETERS clause."""
    clause = " PARAMETERS (" + ", ".join(f"'{k}' = {v}" for k, v in params.items()) + ")" if params else ""
    return f"OPENCYPHER_TABLE(GRAPH WORKSPACE {GRAPH_WORKSPACE} QUERY '{query.replace(chr(39), chr(39) * 2)}'{clause})"


# --- keys -----------------------------------------------------------------------------------


def is_valid_key(key: str) -> bool:
    """Every key the generator draws is stored as a string column value, a JSON string
    and a percent-encoded IRI: any key under the id column's length travels."""
    return 0 < len(key) <= 200
