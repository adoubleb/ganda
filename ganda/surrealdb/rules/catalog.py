"""SurrealDB's engine rules (`ganda.core.rules`): what the fuzzer acts on so that it asks
and compares only what the engine's execution paths answer alike. Its three data models
speak one language, SurrealQL, so a subject is an execution path: a table scan, the
same statement served by an index, the graph arrows (`->e->t`), the recursive idioms
(`{..}`), a `$parent` correlated subquery and the transaction (`BEGIN .. COMMIT /
CANCEL`). Only a rule whose other answer could make two data models differ, make a
statement invalid or exceed a limit is recorded. The probe checks that check the rules
are in `checks.py`."""
from __future__ import annotations

import enum

from ganda.core.rules.model import Defect, ErrorClass, ErrorKind, Kind, Question, Rule, Verdict
from ganda.core.rules.vocabulary import (
    ABSENT_IS_NULL,
    MAX_EMPTY,
    PARALLEL_EDGE_ROWS,
    STRING_ORDER,
    SUM_EMPTY,
    SUM_EXACT_BELOW,
    EmptyAggregate,
    StringOrder,
)


class Subject(enum.StrEnum):
    SURQL = "surql"
    SURQL_INDEX = "surql@index"
    GRAPH = "graph"
    RECURSION = "recursion"
    SUBQUERY = "subquery"
    TRANSACTION = "transaction"


SURQL, SURQL_INDEX, GRAPH, RECURSION, SUBQUERY, TRANSACTION = Subject
COMPARE, ASK, EQUIVALENT, EXACT = Kind.COMPARE, Kind.ASK, Kind.EQUIVALENT, Kind.EXACT
REFUSED, ACCEPTED = Verdict.REFUSED, Verdict.ACCEPTED


def accepts(construct: str) -> Question:
    return Question(f"accepts.{construct}", Verdict, ASK)


def equivalent(form: str) -> Question:
    return Question(f"equivalent.{form}", bool, EQUIVALENT)


def exact(value: str, domain=bool) -> Question:
    return Question(f"exact.{value}", domain, EXACT)


# --- the questions ------------------------------------------------------------------------------
# values
NONE_BELOW_EVERY_VALUE = Question("compare.none_and_null.below_every_value", bool, COMPARE)
NONE_NOT_NULL = Question("compare.none.equals_null", bool, COMPARE)
INT_FLOAT_EQUAL = Question("compare.int_float.by_value", bool, COMPARE)
NEGATIVE_ZERO_EQUALS_ZERO = Question("compare.negative_zero.equals_zero", bool, COMPARE)
DECIMAL_TEXT = Question("value.decimal.wire_form_is_text", bool, COMPARE)
DATETIME_ISO = Question("value.datetime.wire_form_is_iso", bool, COMPARE)
INT_ADD_OVERFLOW_REFUSED = Question("arithmetic.int.overflow_refused", bool, COMPARE)
INT_SUM_WRAPS = Question("aggregate.sum.int.wraps", bool, COMPARE)
# statement forms
UNDECLARED_TABLE_REFUSED = Question("table.undeclared.select_refused", bool, COMPARE)
RELATE_DELETE_CASCADES = Question("delete.record.cascades_relations", bool, COMPARE)
LINK_TO_DELETED_READS_NONE = Question("link.deleted_target.field_reads_none", bool, COMPARE)
CANCELLED_READ_SURVIVES_IN_VARIABLE = Question("transaction.cancelled.let_value_survives", bool, COMPARE)
CANCELLED_STATEMENTS_REPORT_CANCELLED = Question("transaction.cancelled.statements_report_cancelled", bool, COMPARE)
PARENT_IN_CLOSURE = Question("subquery.parent.bound_in_closure", bool, COMPARE)
PARENT_IN_BRACKET_FILTER = Question("subquery.parent.bound_in_bracket_filter", bool, COMPARE)
SHORTEST_EXCLUDES_START = Question("recursion.shortest.excludes_start", bool, COMPARE)
SHORTEST_UNREACHABLE_NONE = Question("recursion.shortest.unreachable_is_none", bool, COMPARE)
SHORTEST_SAME_RECORD_NONE = Question("recursion.shortest.same_record_is_none", bool, COMPARE)
# limits
REQUEST_MAX_BYTES = exact("request.bytes.max", int)
STATEMENTS_PER_REQUEST = exact("statements.per_request.max", int)
INDEX_KEY_MAX_BYTES = exact("index_key.bytes.max", int)

# --- compare: paths answering differently are not compared where the question is exercised ----
COMPARE_RULES = (
    Rule(STRING_ORDER, SURQL, StringOrder.CODE_POINT),
    Rule(NONE_BELOW_EVERY_VALUE, SURQL, True),
    Rule(NONE_NOT_NULL, SURQL, False),
    Rule(ABSENT_IS_NULL, SURQL, False),
    Rule(INT_FLOAT_EQUAL, SURQL, True),
    Rule(NEGATIVE_ZERO_EQUALS_ZERO, SURQL, True),
    Rule(DECIMAL_TEXT, SURQL, True),
    Rule(DATETIME_ISO, SURQL, True),
    Rule(INT_ADD_OVERFLOW_REFUSED, SURQL, True),
    Rule(INT_SUM_WRAPS, SURQL, True),
    Rule(SUM_EMPTY, SURQL, EmptyAggregate.ZERO),
    Rule(MAX_EMPTY, SURQL, EmptyAggregate.NULL),
    Rule(PARALLEL_EDGE_ROWS, GRAPH, 2),
    Rule(UNDECLARED_TABLE_REFUSED, SURQL, True),
    Rule(RELATE_DELETE_CASCADES, GRAPH, True),
    Rule(LINK_TO_DELETED_READS_NONE, SURQL, True),
    Rule(CANCELLED_READ_SURVIVES_IN_VARIABLE, TRANSACTION, True),
    Rule(CANCELLED_STATEMENTS_REPORT_CANCELLED, TRANSACTION, True),
    Rule(PARENT_IN_CLOSURE, SUBQUERY, True),
    Rule(PARENT_IN_BRACKET_FILTER, SUBQUERY, False),
    Rule(SHORTEST_EXCLUDES_START, RECURSION, True),
    Rule(SHORTEST_UNREACHABLE_NONE, RECURSION, True),
    Rule(SHORTEST_SAME_RECORD_NONE, RECURSION, True),
)

# --- ask: a construct a path refuses is not rendered --------------------------------------------
ASK_RULES = (
    Rule(accepts("define_field.option_or_null"), SURQL, ACCEPTED),
    Rule(accepts("relate.explicit_edge_id"), GRAPH, ACCEPTED),
    Rule(accepts("select.several_tables"), SURQL, ACCEPTED),
)

# --- exact: where a value compares exactly ------------------------------------------------------
EXACT_RULES = (
    Rule(SUM_EXACT_BELOW, SURQL, 2**63),
    Rule(exact("sum.decimal"), SURQL, True),
    Rule(REQUEST_MAX_BYTES, SURQL, 64 * 1024 * 1024),
    Rule(STATEMENTS_PER_REQUEST, SURQL, 500),
    Rule(INDEX_KEY_MAX_BYTES, SURQL_INDEX, 16 * 1024),
)

RULES = COMPARE_RULES + ASK_RULES + EXACT_RULES

# the legacy port's defects (`known_bugs/issue-drafts/`) that can make two data models
# differ or a setup fail, re-probed on this build
DEFECTS = (
    Defect("F28", SURQL_INDEX),  # an index over an array field answers a whole-array equality from element keys: nothing
    Defect("F6", RECURSION),  # a bounded `{..k+shortest=x}` below the distance answers nested paths, not NONE
    Defect("F21", TRANSACTION),  # an index defined in a transaction after its data stays empty
)

ERRORS = (
    ErrorClass(ErrorKind.TIMEOUT, marks=("exceeded the timeout",)),
    ErrorClass(ErrorKind.INTERNAL, marks=("internal error", "panicked", "unreachable"), ignore_case=True),
)
