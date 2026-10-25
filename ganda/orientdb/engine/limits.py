"""What OrientDB 3.2 can hold (`ganda.core.data.limits.StorageLimits`), with where each
number comes from: the engine's documentation (`internals/Limits.md`), a live probe on
3.2.57 (`rules/probe.py`, the "large data" section), or the server's own
configuration, which the connection layer starts the container with.
"""
from __future__ import annotations

from ganda.core.data.limits import StorageLimits
from ganda.orientdb.rules import answer
from ganda.orientdb.rules.catalog import SCRIPT, SQL, SQL_INDEX

# `network.http.maxLength`, the largest request body the REST server reads (its default
# is 1 MB); `connection.OrientServer` starts the container with this value, and a server
# attached through ORIENT_URL must be started with it for large data
HTTP_MAX_LENGTH = answer("exact.request.bytes.max", SQL)

LIMITS = StorageLimits(
    # a Java String; the record bound below is what binds a value in practice
    max_string_chars=2**31 - 1,
    # a byte[] (probed: a 3 MB binary and a 40 MB string round-trip through REST)
    max_binary_bytes=2**31 - 1,
    # an embedded list (probed: 200,000 LONGs round-trip; an index over such a list
    # keys every element and takes 0.6 s)
    max_list_items=2**31 - 1,
    # documented: 2 billion schema-full properties per database; "users have reported
    # no problems working with documents made of 15,000 properties"
    max_fields_per_record=15000,
    # documented: a record may be 2 GB, but "we suggest avoiding the creation of records
    # larger than 10 MB"; a quarter of the request bound keeps a record and its request
    # under that advice
    max_record_bytes=16 * 1024 * 1024,
    # documented: 2^63 - 1 records per cluster, 2^78 - 1 per database
    max_rows_per_table=2**63 - 1,
    # probed: a 20,000-entry embedded link list and a vertex with 5,000 incoming edges
    # write in well under a second; the doc data model folds a source's relationships
    # into ONE statement, so this also keeps that statement under the request bound
    max_edges_per_node=20000,
    # probed: an SB-tree index key may be at most 10240 bytes
    # (`OTooBigIndexKeyException`), the key being the value's UTF-8 form plus 16 bytes
    # of overhead: 10224 ASCII characters, 3408 three-byte ones, 2556 four-byte ones; a
    # hash index has no key size bound
    max_index_key_bytes=answer("exact.index_key.bytes.max", SQL_INDEX),
    # configured: `network.http.maxLength` above
    max_request_bytes=HTTP_MAX_LENGTH,
    # probed: scripts and batches of 500 statements run in 0.1 s; no documented bound,
    # a script's variables live in one context
    max_statements_per_request=answer("exact.statements.per_request.max", SCRIPT),
)
