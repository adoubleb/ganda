"""Which rules each probe fact (`scripts/probe.py`) checks and which defects it
reproduces: a fact is listed against a rule only where its check asserts that rule's
answer (the `L` limit facts and the `T` timeout fact assert the `limits` slot and the
error classes, which are no rules), against a defect only where its expected answer is
the defect's."""
from __future__ import annotations

from ganda.core.rules.model import Rule
from ganda.core.rules.vocabulary import (
    ABSENT_IS_NULL,
    FILTER_LOGIC,
    LIKE_CASE_SENSITIVE,
    MAX_EMPTY,
    MEMBERSHIP_ABSENT_LIST,
    OWN_UNCOMMITTED_VISIBLE,
    PARALLEL_EDGE_ROWS,
    STRING_ORDER,
    SUM_EMPTY,
    SUM_EXACT_BELOW,
    UNDIRECTED_SELF_LOOP_ROWS,
    UNGROUPED_EMPTY_ROWS,
    VARIABLE_LENGTH_PATH_MODE,
    WRITE_ROW_COUNT,
    EmptyAggregate,
    EmptyInputRows,
    Logic,
    PathMode,
    StringOrder,
    Truth,
)
from ganda.arangodb.rules.catalog import (
    ABSENT_SURVIVES_PROJECTION,
    ACCEPTED,
    AQL,
    AQL_INDEX,
    ARITHMETIC_EXACT_BELOW,
    COLLECT_SUM_EMPTY,
    COUNT_DISTINCT_COUNTS_NULL,
    DEFECTS,
    DELETE_VERTEX_CASCADES,
    DOCUMENT_API,
    IN_NULL_MATCHES_NULL,
    INTEGER_VS_DOUBLE_EXACT,
    INTEGRAL_DOUBLE_AS_INTEGER,
    LIKE_CROSSES_NEWLINE,
    LIKE_LINE_TERMINATORS,
    LIKE_NULL_AS_EMPTY,
    MAX_MIN_SKIP_NULL,
    NEGATIVE_ZERO_EQUALS_ZERO,
    PATH,
    REFUSED,
    SHORTEST_PATH_SAME_RECORD,
    SHORTEST_PATH_UNREACHABLE,
    STRING_EQUALITY,
    TRANSACTION,
    TRAVERSAL,
    TYPE_ORDER_TOTAL,
    UPDATE_KEEPS_NULL,
    UPDATE_MERGES_MAP_VALUE,
    accepts,
    equivalent,
)

_DEFECT = {d.id: d for d in DEFECTS}

CHECKS = {
    # --- V: the value semantics
    "V1": (Rule(TYPE_ORDER_TOTAL, AQL, True), Rule(FILTER_LOGIC, AQL, Logic.TWO_VALUED)),
    "V2": (Rule(INTEGER_VS_DOUBLE_EXACT, AQL, True), Rule(NEGATIVE_ZERO_EQUALS_ZERO, AQL, True)),
    "V3": (Rule(STRING_EQUALITY, AQL, StringOrder.CODE_POINT), Rule(STRING_ORDER, AQL, StringOrder.COLLATION)),
    "V4": (Rule(IN_NULL_MATCHES_NULL, AQL, True), Rule(MEMBERSHIP_ABSENT_LIST, AQL, Truth.FALSE)),
    "V5": (Rule(LIKE_CASE_SENSITIVE, AQL, True), Rule(LIKE_CROSSES_NEWLINE, AQL, True), Rule(LIKE_LINE_TERMINATORS, AQL, "U+0085 U+2028 U+2029"), Rule(LIKE_NULL_AS_EMPTY, AQL, True)),
    "V6": (
        Rule(SUM_EMPTY, AQL, EmptyAggregate.ZERO),
        Rule(COLLECT_SUM_EMPTY, AQL, EmptyAggregate.NULL),
        Rule(MAX_EMPTY, AQL, EmptyAggregate.NULL),
        Rule(MAX_MIN_SKIP_NULL, AQL, True),
        Rule(COUNT_DISTINCT_COUNTS_NULL, AQL, True),
        Rule(UNGROUPED_EMPTY_ROWS, AQL, EmptyInputRows.ONE_ROW),
    ),
    "V7": (Rule(INTEGRAL_DOUBLE_AS_INTEGER, AQL, True), Rule(ARITHMETIC_EXACT_BELOW, AQL, 2**53), Rule(SUM_EXACT_BELOW, AQL, 2**53)),
    "V8": (Rule(ABSENT_IS_NULL, AQL, True), Rule(ABSENT_SURVIVES_PROJECTION, AQL, False), Rule(accepts("has.non_object_argument"), AQL, ACCEPTED), Rule(accepts("values.non_object_argument"), AQL, REFUSED)),
    # --- S: the statement forms
    "S1": (Rule(PARALLEL_EDGE_ROWS, AQL, 2), Rule(PARALLEL_EDGE_ROWS, TRAVERSAL, 2), Rule(equivalent("edge_scan"), TRAVERSAL, True), Rule(equivalent("link_array_forms"), AQL, True), Rule(equivalent("junction_forms"), AQL, True)),
    "S2": (Rule(UNDIRECTED_SELF_LOOP_ROWS, AQL, 2), Rule(UNDIRECTED_SELF_LOOP_ROWS, TRAVERSAL, 2), Rule(equivalent("both_as_union"), TRAVERSAL, True)),
    "S3": (Rule(VARIABLE_LENGTH_PATH_MODE, TRAVERSAL, PathMode.TRAIL), Rule(equivalent("deep_traversal"), TRAVERSAL, True), Rule(equivalent("reach_bfs_global"), TRAVERSAL, True)),
    "S4": (Rule(equivalent("flat_collect"), AQL, True), Rule(MAX_MIN_SKIP_NULL, AQL, True)),
    "S5": (Rule(equivalent("noopt"), AQL, True), Rule(equivalent("subquery_target"), AQL, True), Rule(equivalent("collect_forms"), AQL, True), Rule(equivalent("disable_index"), AQL_INDEX, True), Rule(equivalent("index_hint"), AQL_INDEX, True)),
    "S6": (Rule(equivalent("polymorphic_forms"), AQL, True), Rule(accepts("variable.redeclared"), AQL, REFUSED)),
    "S7": (Rule(SHORTEST_PATH_UNREACHABLE, PATH, 0), Rule(SHORTEST_PATH_SAME_RECORD, PATH, 1), Rule(equivalent("path_forms"), PATH, True)),
    "S8": (Rule(accepts("bind_variable.undeclared"), AQL, REFUSED),),
    "S9": (Rule(accepts("index_hint.unservable"), AQL_INDEX, REFUSED),),
    "S10": (Rule(equivalent("pull_up"), AQL, True),),
    # --- W: the write doors
    "W1": (Rule(WRITE_ROW_COUNT, AQL, False), Rule(UPDATE_KEEPS_NULL, AQL, True)),
    "W2": (Rule(OWN_UNCOMMITTED_VISIBLE, TRANSACTION, True), Rule(equivalent("transaction_door"), TRANSACTION, True), Rule(equivalent("write_doors"), AQL, True), Rule(equivalent("write_doors"), DOCUMENT_API, True)),
    "W3": (Rule(DELETE_VERTEX_CASCADES, AQL, False),),
    "W4": (Rule(UPDATE_MERGES_MAP_VALUE, AQL, True), Rule(UPDATE_MERGES_MAP_VALUE, DOCUMENT_API, True), Rule(UPDATE_KEEPS_NULL, DOCUMENT_API, True)),
    "W5": (Rule(accepts("transaction.collection_absent_from_database"), TRANSACTION, REFUSED),),
    # --- A: the defects, each reproduced by the fact of its id
    "A1": (_DEFECT["A1"],),
    "A2": (_DEFECT["A2"],),
    "A25": (_DEFECT["A25"],),
    "A40": (_DEFECT["A40"],),
    "A41": (_DEFECT["A41"],),
    "A42": (_DEFECT["A42"],),
}
