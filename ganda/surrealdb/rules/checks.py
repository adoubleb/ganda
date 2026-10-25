"""Which rules (and error classes) each probe check (`rules/probe.py`, ids `C<n>`,
never reused) checks, and which defects it reproduces -- every check is listed: a check
is listed against a rule only where it asserts that rule's answer, against a defect
only where its expected answer is the defect's."""
from __future__ import annotations

from ganda.core.rules.model import Rule
from ganda.core.rules.vocabulary import ABSENT_IS_NULL, MAX_EMPTY, PARALLEL_EDGE_ROWS, STRING_ORDER, SUM_EMPTY, SUM_EXACT_BELOW, EmptyAggregate, StringOrder
from ganda.surrealdb.rules.catalog import (
    ACCEPTED,
    CANCELLED_READ_SURVIVES_IN_VARIABLE,
    CANCELLED_STATEMENTS_REPORT_CANCELLED,
    DATETIME_ISO,
    DECIMAL_TEXT,
    DEFECTS,
    ERRORS,
    GRAPH,
    INDEX_KEY_MAX_BYTES,
    INT_ADD_OVERFLOW_REFUSED,
    INT_FLOAT_EQUAL,
    INT_SUM_WRAPS,
    LINK_TO_DELETED_READS_NONE,
    NEGATIVE_ZERO_EQUALS_ZERO,
    NONE_BELOW_EVERY_VALUE,
    NONE_NOT_NULL,
    PARENT_IN_BRACKET_FILTER,
    PARENT_IN_CLOSURE,
    RECURSION,
    RELATE_DELETE_CASCADES,
    REQUEST_MAX_BYTES,
    SHORTEST_EXCLUDES_START,
    SHORTEST_SAME_RECORD_NONE,
    SHORTEST_UNREACHABLE_NONE,
    STATEMENTS_PER_REQUEST,
    SUBQUERY,
    SURQL,
    SURQL_INDEX,
    TRANSACTION,
    UNDECLARED_TABLE_REFUSED,
    accepts,
    exact,
)

_RULE_CHECKS = {
    "C1": (Rule(STRING_ORDER, SURQL, StringOrder.CODE_POINT),),
    "C2": (Rule(NONE_BELOW_EVERY_VALUE, SURQL, True),),
    "C3": (Rule(NONE_NOT_NULL, SURQL, False),),
    "C4": (Rule(ABSENT_IS_NULL, SURQL, False),),
    "C5": (Rule(INT_FLOAT_EQUAL, SURQL, True),),
    "C6": (Rule(NEGATIVE_ZERO_EQUALS_ZERO, SURQL, True),),
    "C7": (Rule(DECIMAL_TEXT, SURQL, True),),
    "C8": (Rule(DATETIME_ISO, SURQL, True),),
    "C9": (Rule(INT_ADD_OVERFLOW_REFUSED, SURQL, True),),
    "C10": (Rule(INT_SUM_WRAPS, SURQL, True),),
    "C11": (Rule(SUM_EMPTY, SURQL, EmptyAggregate.ZERO),),
    "C12": (Rule(MAX_EMPTY, SURQL, EmptyAggregate.NULL),),
    "C13": (Rule(PARALLEL_EDGE_ROWS, GRAPH, 2),),
    "C14": (Rule(UNDECLARED_TABLE_REFUSED, SURQL, True),),
    "C15": (Rule(RELATE_DELETE_CASCADES, GRAPH, True),),
    "C16": (Rule(LINK_TO_DELETED_READS_NONE, SURQL, True),),
    "C17": (Rule(CANCELLED_READ_SURVIVES_IN_VARIABLE, TRANSACTION, True),),
    "C18": (Rule(CANCELLED_STATEMENTS_REPORT_CANCELLED, TRANSACTION, True),),
    "C19": (Rule(PARENT_IN_CLOSURE, SUBQUERY, True),),
    "C20": (Rule(PARENT_IN_BRACKET_FILTER, SUBQUERY, False),),
    "C21": (Rule(SHORTEST_EXCLUDES_START, RECURSION, True),),
    "C22": (Rule(SHORTEST_UNREACHABLE_NONE, RECURSION, True),),
    "C23": (Rule(SHORTEST_SAME_RECORD_NONE, RECURSION, True),),
    "C24": (Rule(accepts("define_field.option_or_null"), SURQL, ACCEPTED),),
    "C25": (Rule(accepts("relate.explicit_edge_id"), GRAPH, ACCEPTED),),
    "C26": (Rule(accepts("select.several_tables"), SURQL, ACCEPTED),),
    "C27": (Rule(SUM_EXACT_BELOW, SURQL, 2**63),),
    "C28": (Rule(exact("sum.decimal"), SURQL, True),),
    "C29": (Rule(REQUEST_MAX_BYTES, SURQL, 64 * 1024 * 1024),),
    "C30": (Rule(STATEMENTS_PER_REQUEST, SURQL, 500),),
    "C31": (Rule(INDEX_KEY_MAX_BYTES, SURQL_INDEX, 16 * 1024),),
    "C43": (ERRORS[0],),
}

REPRODUCED_BY = {"F28": ("C40",), "F6": ("C41",), "F21": ("C42",)}

_DEFECT = {d.id: d for d in DEFECTS}
CHECKS: dict = {fid: tuple(items) for fid, items in _RULE_CHECKS.items()}
for _defect, _fids in REPRODUCED_BY.items():
    for _fid in _fids:
        CHECKS[_fid] = CHECKS.get(_fid, ()) + (_DEFECT[_defect],)
