"""SAP HANA's vocabulary for the abstract model (`ganda.core.model`): the declared type
of each `FieldType` in a column table, how a value is stored in a JSON collection and as
RDF terms, the IRIs of the triple store, the index kinds a schema may name, and the
write forms a setup may take. `ganda.core.model` imports nothing from this module.

Three data models of one HANA Cloud database (`RULES.md` holds the facts each rests on):

- `doc`: the JSON Document Store. One COLLECTION per node class; a document holds `id`,
  its fields and, per edge type leaving its class, an array of link objects
  `{"eid": ..., "to": <target id>, <attributes>}` -- the relationships embedded in the
  source, as a document model keeps them.
- `rel`: column tables. One table per node class (`id` and a column per field) and
  one JUNCTION table per edge type (`eid`, `src_id`, `dst_id`, a column per attribute).
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

from ganda.core.model import FieldType

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


# --- keys -----------------------------------------------------------------------------------


def is_valid_key(key: str) -> bool:
    """Every key the generator draws is stored as a string column value, a JSON string
    and a percent-encoded IRI: any key under the id column's length travels."""
    return 0 < len(key) <= 200
