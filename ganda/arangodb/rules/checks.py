"""Which rules each probe fact (`probe.py`) checks and which defects it
reproduces: a fact is listed against a rule only where its check asserts that rule's
answer (the `L` limit facts and the `T` timeout fact assert the `limits` slot and the
error classes, which are no rules), against a defect only where its expected answer is
the defect's."""
from __future__ import annotations

from ganda.core.rules.model import Rule
from ganda.core.rules.vocabulary import (
    MAX_EMPTY,
    OWN_UNCOMMITTED_VISIBLE,
    PARALLEL_EDGE_ROWS,
    STRING_ORDER,
    SUM_EXACT_BELOW,
    UNDIRECTED_SELF_LOOP_ROWS,
    VARIABLE_LENGTH_PATH_MODE,
    EmptyAggregate,
    PathMode,
    StringOrder,
)
from ganda.arangodb.rules.catalog import (
    ACCEPTED,
    AQL,
    ARITHMETIC_EXACT_BELOW,
    DEFECTS,
    DELETE_VERTEX_CASCADES,
    INLINE_ARRAY_REBUILDS,
    DOCUMENT_API,
    MAX_MIN_SKIP_NULL,
    PATH,
    REFUSED,
    SHORTEST_PATH_SAME_RECORD,
    SHORTEST_PATH_UNREACHABLE,
    STRING_EQUALITY,
    TRANSACTION,
    TRAVERSAL,
    UPDATE_KEEPS_NULL,
    UPDATE_MERGES_MAP_VALUE,
    accepts,
    equivalent,
)

_DEFECT = {d.id: d for d in DEFECTS}

CHECKS = {
    # --- V: the value semantics
    "V3": (Rule(STRING_EQUALITY, AQL, StringOrder.CODE_POINT), Rule(STRING_ORDER, AQL, StringOrder.COLLATION)),
    "V6": (
        Rule(MAX_EMPTY, AQL, EmptyAggregate.NULL),
        Rule(MAX_MIN_SKIP_NULL, AQL, True),
    ),
    "V7": (Rule(ARITHMETIC_EXACT_BELOW, AQL, 2**53), Rule(SUM_EXACT_BELOW, AQL, 2**53)),
    "V8": (Rule(accepts("has.non_object_argument"), AQL, ACCEPTED), Rule(accepts("values.non_object_argument"), AQL, REFUSED)),
    # --- S: the statement forms
    "S1": (Rule(PARALLEL_EDGE_ROWS, AQL, 2), Rule(PARALLEL_EDGE_ROWS, TRAVERSAL, 2), Rule(equivalent("edge_scan"), TRAVERSAL, True), Rule(equivalent("link_array_forms"), AQL, True), Rule(equivalent("junction_forms"), AQL, True)),
    "S2": (Rule(UNDIRECTED_SELF_LOOP_ROWS, AQL, 2), Rule(UNDIRECTED_SELF_LOOP_ROWS, TRAVERSAL, 2), Rule(equivalent("both_as_union"), TRAVERSAL, True)),
    "S3": (Rule(VARIABLE_LENGTH_PATH_MODE, TRAVERSAL, PathMode.TRAIL), Rule(equivalent("deep_traversal"), TRAVERSAL, True), Rule(equivalent("reach_bfs_global"), TRAVERSAL, True)),
    "S4": (Rule(equivalent("flat_collect"), AQL, True), Rule(MAX_MIN_SKIP_NULL, AQL, True)),
    "S6": (Rule(accepts("variable.redeclared"), AQL, REFUSED),),
    "S7": (Rule(SHORTEST_PATH_UNREACHABLE, PATH, 0), Rule(SHORTEST_PATH_SAME_RECORD, PATH, 1), Rule(equivalent("path_forms"), PATH, True)),
    "S8": (Rule(accepts("bind_variable.undeclared"), AQL, REFUSED),),
    # --- W: the write doors
    "W1": (Rule(UPDATE_KEEPS_NULL, AQL, True), Rule(equivalent("update_replace"), AQL, True)),
    "W2": (Rule(OWN_UNCOMMITTED_VISIBLE, TRANSACTION, True), Rule(equivalent("write_doors"), AQL, True), Rule(equivalent("write_doors"), DOCUMENT_API, True)),
    "W3": (Rule(DELETE_VERTEX_CASCADES, AQL, False),),
    "W4": (Rule(UPDATE_MERGES_MAP_VALUE, AQL, True), Rule(UPDATE_MERGES_MAP_VALUE, DOCUMENT_API, True), Rule(UPDATE_KEEPS_NULL, DOCUMENT_API, True)),
    "W5": (Rule(accepts("transaction.collection_absent_from_database"), TRANSACTION, REFUSED),),
    "W6": (Rule(INLINE_ARRAY_REBUILDS, AQL, True),),
    # --- A: the defects, each reproduced by the fact of its id
    "A25": (_DEFECT["A25"],),
    "A42": (_DEFECT["A42"],),
    "A43": (_DEFECT["A43"],),
}
