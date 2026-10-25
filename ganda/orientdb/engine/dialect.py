"""OrientDB's dialect for the abstract model (`ganda.core.data.model`): the declared type
of each `FieldType`, the index kinds and the key charset (probed 3.2.56). The operator
spellings are `concrete_syntax/query/predicate.py`'s, the write forms
`provider/physical_design.py`'s.

- Records are addressed by the logical `id` property, never by `@rid` (a RID differs
  per database); a setup step needing a RID carries a `Ref` the connection resolves.
- Every property is declared: OrientDB coerces a value to the declared type on write,
  so the generator only writes values of the column's type.
- Values travel as JSON; each narrow type holds only what JSON carries exactly
  (INTEGER int32, FLOAT float32-exact, DECIMAL at most three places).
"""
from __future__ import annotations

import re

from ganda.core.data.model import FieldType
from ganda.orientdb.rules import assumes, works_around
from ganda.orientdb.rules.catalog import SQL

# The declared type of each FieldType (`CREATE PROPERTY t.f <type>`). Engine faces of
# the narrow types: INTEGER keys wrap through an index (O16), sums wrap at 32 bits (O19),
# writes wrap (O24); FLOAT keys round through an index (O16); DECIMAL against a double
# compares the binary expansion on a scan (O20). Dates are `yyyy-MM-dd HH:mm:ss` (UTC);
# a partial date string compares as midnight (O27). An EMBEDDEDMAP LONG's CONTAINSVALUE
# misses an Integer-boxed constant (O7). A BINARY is base64 text, never compared or
# indexed: a scan's `b = :p` matches nothing where an index matches (O46).
works_around("O46")
assumes("value.binary.base64_round_trip", SQL, True)

ORIENT_TYPES = {
    FieldType.INT: "LONG",
    FieldType.FLOAT: "DOUBLE",
    FieldType.STRING: "STRING",
    FieldType.BOOL: "BOOLEAN",
    FieldType.LIST: "EMBEDDEDLIST LONG",
    FieldType.INT32: "INTEGER",
    FieldType.FLOAT32: "FLOAT",
    FieldType.DECIMAL: "DECIMAL",
    FieldType.DATETIME: "DATETIME",
    FieldType.DATE: "DATE",
    FieldType.MAP: "EMBEDDEDMAP LONG",
    FieldType.BINARY: "BINARY",
}


# Index kinds (`IndexSpec.kind`): NOTUNIQUE / UNIQUE are range-capable SB-trees, the
# hash kinds serve equality only, DICTIONARY keeps one record per key (drawn only for
# distinct columns). A null or absent field is the key `null` (`ignoreNullValues` false).
UNIQUE_KINDS = ("UNIQUE", "UNIQUE_HASH_INDEX")
HASH_KINDS = ("NOTUNIQUE_HASH_INDEX", "UNIQUE_HASH_INDEX")
RANGE_KINDS = ("NOTUNIQUE", "UNIQUE", "DICTIONARY")

# The index on `id` per data model (`Schema.id_index`); "none" makes `WHERE id = :s` a scan.
ID_INDEX_KINDS = ("UNIQUE", "UNIQUE_HASH_INDEX", "NOTUNIQUE", "none")
DEFAULT_ID_INDEX = "UNIQUE"


# Logical keys: no `#` (a RID-shaped string becomes a LINK inside an embedded document).
_KEY_RE = re.compile(r"^[^#\x00]{1,200}$")


def is_valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key))
