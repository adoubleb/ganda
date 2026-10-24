"""OrientDB's vocabulary for the abstract model (`ganda.core.data.model`): the declared type of
each `FieldType`, the index kinds an `IndexSpec` / `Schema.id_index` names, the write
forms a `WriteForm` names, the key charset, and the engine facts the model's types meet
here (live-probed 3.2.56). `ganda.core.data.model` imports nothing from this module.

- **Records are addressed by a logical `id` property, never by `@rid`.** A RID
  (`#cluster:position`) is assigned per database, so the three data models of one instance
  hold different RIDs for the same node; every query projects and compares the `id`
  string (`n1/r0`), and every setup step that needs a RID carries a `Ref` placeholder
  the connection layer resolves against the RIDs it captured when it inserted that record.
- **Every property is declared (schemaful).** OrientDB coerces a value to the declared
  type on write (`5.5` into a LONG is `5`, a bool is null -- probed), and a schemaless
  class types each value by its JSON (`5` INTEGER, `2^40` LONG, `"#1:0"` a LINK), so
  the generator only ever writes a value of the column's declared type.
- Values travel as JSON: int (LONG / INTEGER), float (DOUBLE / FLOAT / DECIMAL), str,
  bool, list of ints (EMBEDDEDLIST LONG). The narrow types' value domains are what the
  JSON wire carries exactly: an INTEGER holds int32 values, a FLOAT float32 values whose
  shortest decimal is their exact value, a DECIMAL values with at most three decimals,
  stored through `BigDecimal.valueOf(double)` and read back as the same double (probed).
"""
from __future__ import annotations

import re

from ganda.core.data.model import FieldType
from ganda.orientdb.rules import assumes, works_around
from ganda.orientdb.rules.catalog import SQL

# The declared property type of each FieldType (`CREATE PROPERTY t.f <type>`).
#
# The narrow numeric types (the `narrow_type_probability` axis): an INTEGER holds
# Integer boxes an index converts a Long key to by WRAPPING (O16), sums in 32-bit
# arithmetic (O19) and stores an out-of-range write wrapped (O24); a FLOAT holds Float
# boxes an index converts a double key to by ROUNDING (O16) and an integral pivot is
# compared with through float; a DECIMAL holds BigDecimals a scan compares with a double
# through the double's binary expansion where an index or BETWEEN uses its decimal
# string (O20).
#
# A DATETIME / DATE travels as the database's `yyyy-MM-dd HH:mm:ss` string (UTC; a DATE
# reads back at midnight), the model's canonical form. A pivot may be the date alone
# (midnight), an epoch-millisecond integer, or a partially matching string that the
# engine silently compares as the date's midnight (O27).
#
# An EMBEDDEDMAP LONG supports `CONTAINSKEY`, the dotted `m.k op :p` (null rules for a
# missing key), the parameter-keyed `m[:k]`, `m.values() CONTAINS`; `CONTAINSVALUE` with
# an Integer-boxed pivot misses the Long values (an O7 face); indexed BY KEY / VALUE.
#
# A BINARY (the `binary_probability` axis) takes a base64 string on write -- through a
# bind parameter, a CONTENT document or a MERGE literal -- and answers the same standard
# base64 text (padded, no line breaks) on read, so the model's value IS what travels
# (probed 3.2.57). Two engine facts the probe records: an EMPTY byte string is stored as
# null (the model's pool holds none), and `b = :p` with a base64 pivot matches NOTHING on
# a scan while an index on the column matches it (O46, `issues/O46-binary-equality-scan-
# vs-index.md`: the scan decodes the text and then compares the arrays by identity, the
# index key is compared by content) -- a scan-vs-index divergence the model does not
# ask about, since a BINARY column takes no comparison predicate and no index.

# a BINARY column is never compared or indexed (O46)
works_around("O46")
# every write door stores a base64 BINARY as the same text
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


# Order: LONG / INTEGER are Java integer order; DOUBLE / FLOAT are `Double.compare`
# (which puts -0.0 strictly below 0.0, probed; a FLOAT value is its exact widening);
# DECIMAL is numeric order (no -0.0: a BigDecimal has none, probed); STRING is UTF-16
# code-unit order (`String.compareTo`), which `semantics.orient_key` reproduces by
# comparing the UTF-16 encoding.
#
# The type a value of each kind is COMPARED in when a pivot of the other kind meets it
# on a scan (probed 3.2.56, `castComparableNumber`): the wider of the two Java boxes,
# Integer < Long < Float < Double. An INTEGER against a Long compares as longs, a FLOAT
# against an Integer or Long as FLOATS (`16777217 = 16777216.0f`), anything against a
# Double as doubles; a DECIMAL against an integer exactly and against a double through
# `new BigDecimal(double)` (O20).
#
# `FieldSpec.case_insensitive` is `ALTER PROPERTY t.f COLLATE ci` (the
# `collate_probability` axis, STRING fields): `=`, `<>`, `<`..`>=` and a value join
# compare case-insensitively on a scan and through an index; `IN` and `BETWEEN` do not
# on a scan (O21); a bracket filter and a dotted link field ignore it (O25); GROUP BY /
# DISTINCT / max / min do not merge case variants; an index stores the lower-cased key
# (a UNIQUE index rejects `a` next to `A`).

# Index kinds (`IndexSpec.kind`). NOTUNIQUE/UNIQUE are the SB-tree indexes
# (range-capable); the *_HASH_INDEX kinds answer equality only and a range predicate on
# them falls back to a scan (probed); DICTIONARY keeps ONE record per key (by
# replacement), so the kind is only drawn for a column whose values are distinct on every
# data model (3.2.56's planner answered an equality through it with one record, O4,
# fixed in 3.2.57). `IndexSpec.ignore_nulls` is the
# METADATA `ignoreNullValues`, always rendered out (the engine's default is false: a null
# or ABSENT field is the key `null`, which a unique index holds once -- probed);
# the index option `by` (`physical_design.draw_index_options`) is a map index's `BY KEY`
# (serves CONTAINSKEY) or `BY VALUE` (serves CONTAINSVALUE). An edge index sits on the graph data model's E subclass and the relational
# data model's junction class; the doc data model has nothing to index (an embedded list's
# sub-field is not indexable, probed). The planner uses a composite index for a
# predicate on its first field only, and a base class's index for a query on any
# subclass; a query on the base class uses its subclasses' indexes in parallel (probed).
INDEX_KINDS = ("NOTUNIQUE", "UNIQUE", "NOTUNIQUE_HASH_INDEX", "UNIQUE_HASH_INDEX", "DICTIONARY")
UNIQUE_KINDS = ("UNIQUE", "UNIQUE_HASH_INDEX")
HASH_KINDS = ("NOTUNIQUE_HASH_INDEX", "UNIQUE_HASH_INDEX")
RANGE_KINDS = ("NOTUNIQUE", "UNIQUE", "DICTIONARY")

# How each data model looks a node up by its logical `id` (`Schema.id_index`): the kind of
# index on the `id` property. "none" means a class scan serves `WHERE id = :s`, which is
# what makes the `id` index an axis rather than a constant -- the shape of
# issue-drafts/O1 depends on whether the outer query of a correlated subquery is
# index-served.
ID_INDEX_KINDS = ("UNIQUE", "UNIQUE_HASH_INDEX", "NOTUNIQUE", "none")
# the id index when the id index axis drew none for a data model
DEFAULT_ID_INDEX = "UNIQUE"

# The core's predicate operators that OrientDB SQL spells differently; every other
# operator (`=`, `IN`, `LIKE`, `IS NULL`, ...) is written as the core names it. `VALUES HAS`
# has its own form (`m.values() CONTAINS :p`, `concrete_syntax.predicate`).
OPERATORS = {
    "HAS": "CONTAINS",
    "HAS ANY": "CONTAINSANY",
    "HAS ALL": "CONTAINSALL",
    "HAS KEY": "CONTAINSKEY",
    "HAS VALUE": "CONTAINSVALUE",
    "IS PRESENT": "IS DEFINED",
    "IS ABSENT": "IS NOT DEFINED",
}


def sql_op(op: str) -> str:
    """A core predicate operator as OrientDB SQL writes it."""
    return OPERATORS.get(op, op)

# the storage option `clusters` is `CREATE CLASS n1 ... CLUSTERS n` (the
# `clusters_probability` axis): records are spread round-robin over n clusters, so a
# class scan is a multi-cluster fetch and an index covers several clusters (probed:
# queries answer alike).

# Logical keys: kept to a charset every data model stores as a plain STRING. A `#` is
# excluded on purpose -- a value shaped like a RID (`#1:0`) is auto-converted to a LINK
# by the SQL layer inside an embedded document (probed), and the key travels inside
# link entries on the doc data model.
_KEY_RE = re.compile(r"^[^#\x00]{1,200}$")


def is_valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key))


# --- write forms (`WriteForm`): the setup script is itself a cross-model pair of write
# sequences ---
#
# Every component reaches a different engine write path while the final state stays
# the same (live-probed 3.2.56):
#   insert_via: "set"     INSERT INTO t SET a = :a, ...  /  CREATE VERTEX t SET ...
#               "content" INSERT INTO t CONTENT :doc      /  CREATE VERTEX t CONTENT ...
#               "values"  INSERT INTO t (a, b) VALUES (:a, :b)   (graph: CREATE VERTEX SET)
#   update_form: "set"     UPDATE t SET a = :a WHERE id = :id (+ REMOVE for dropped fields)
#                "merge"   UPDATE t MERGE <json literal> WHERE id = :id  (a MERGE takes no
#                          bind parameter, probed; the literal is JSON-encoded, never
#                          SQL-interpolated)
#                "content" UPDATE t CONTENT :doc WHERE id = :id (whole document; a
#                          vertex keeps its edge bags through CONTENT, probed, and the
#                          doc data model's link lists are carried in the document)
#                "upsert"  UPDATE t SET ... UPSERT WHERE id = :id
#                "arith"   UPDATE t SET f = f + :d, g = :g WHERE id = :id -- an integer
#                          field present before and after is written as an ARITHMETIC
#                          expression over its stored value (`INCREMENT` is refused by
#                          the new executor, probed); the delta keeps every sum a long
#   edge_update_form: how an EdgeUpdate reaches the graph E class / the junction:
#                "set" / "merge" / "content" (`UPDATE EDGE e CONTENT {...}` keeps the
#                endpoints, probed); the doc data model always rewrites the link list
#   edge_from:  "ref"      CREATE EDGE e FROM :a TO :b with RID parameters
#               "subquery" CREATE EDGE e FROM (SELECT FROM t WHERE id = :a) TO (...)
#   transaction: the data steps run in one script `BEGIN; ...; COMMIT;` instead of
#                auto-commit commands (a batch with `transaction: true` is the third door)
#   cancelled_junk: a trailing `BEGIN; ...; ROLLBACK;` script writes a junk record,
#                updates and removes a real one -- none of it may survive.
#   rebuild_indexes: a trailing `REBUILD INDEX *`, so every index is rebuilt from the
#                records instead of maintained incrementally through the churn.
# An EdgeUpdate is `UPDATE EDGE e1 ...` on the graph data model (whose CONTENT form keeps
# `out` / `in`, probed), a plain UPDATE of the junction row, and a rewrite of the
# source's link list on the doc data model; a NodeDelete is `DELETE VERTEX` (cascading
# natively) on the graph data model.
INSERT_FORMS = ("set", "content", "values")
UPDATE_FORMS = ("set", "merge", "content", "upsert", "arith")
EDGE_UPDATE_FORMS = ("set", "merge", "content")
EDGE_FROM_FORMS = ("ref", "subquery")
TRANSACTION_FORMS = ("none", "script", "batch")
