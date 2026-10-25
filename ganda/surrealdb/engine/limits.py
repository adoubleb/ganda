"""What SurrealDB 3.3 (the memory backend, over `/sql`) can hold
(`ganda.core.data.limits.StorageLimits`), with where each number comes from: the server's
configuration (the connection layer starts the container with it), a live probe on 3.3.4
(`rules/probe.py`), or the absence of a documented bound.
"""
from __future__ import annotations

from ganda.core.data.limits import StorageLimits
from ganda.surrealdb.rules import answer
from ganda.surrealdb.rules.catalog import SURQL, SURQL_INDEX

# `SURREAL_HTTP_MAX_SQL_BODY_SIZE`, the largest `/sql` body the server reads (its default
# is 1 MiB; probed: a 32 MiB body answers in half a second); `connection.SurrealServer`
# starts the container with this value, and a server attached through SURREAL_URL must be
# started with it for large data
HTTP_MAX_SQL_BODY_SIZE = answer("exact.request.bytes.max", SURQL)

LIMITS = StorageLimits(
    # a Rust String; no documented bound, the request bound binds a value in practice
    max_string_chars=4 * 1024 * 1024,
    # a byte array, written as a hex literal (two characters per byte)
    max_binary_bytes=4 * 1024 * 1024,
    # an array; no documented bound
    max_list_items=200_000,
    # no documented bound; a SCHEMAFULL table declares every field
    max_fields_per_record=1000,
    # a record travels in one statement: a quarter of the request bound
    max_record_bytes=16 * 1024 * 1024,
    max_rows_per_table=2**63 - 1,
    # the doc data model folds a source's relationships into ONE statement, which stays
    # under the request bound at this fan-out
    max_edges_per_node=20000,
    # no documented bound on an index key (probed: L facts)
    max_index_key_bytes=answer("exact.index_key.bytes.max", SURQL_INDEX),
    max_request_bytes=HTTP_MAX_SQL_BODY_SIZE,
    # one request runs its statements in order; probed: 500 statements in one body
    max_statements_per_request=answer("exact.statements.per_request.max", SURQL),
)
