"""What ArangoDB can hold (`ganda.core.limits.StorageLimits`), and where each number
comes from: the documentation, a bound the provider chooses so that all three data
models stay inside a documented one, or a probe (`ganda arangodb probe`, the `L`
facts). Every bound here is probed live before a large campaign relies on it.
"""
from __future__ import annotations

from ganda.core.limits import StorageLimits

# the request body the server takes (documented default of `--http.max-request-body-size`
# is far above this; the provider keeps its requests under this bound and the probe
# sends one of exactly this size)
HTTP_MAX_LENGTH = 64 * 1024 * 1024
# the size of one stream transaction's writes (`--transaction.streaming-max-transaction-size`,
# documented default 128 MB): a data model's whole setup may run in one, so the server
# is started with this bound and the setup's data stays under it
STREAMING_TRANSACTION_MAX_BYTES = 1024 * 1024 * 1024

LIMITS = StorageLimits(
    # a VelocyPack string has no documented bound below the document's own; chosen
    # under the record bound
    max_string_chars=16 * 1024 * 1024,
    # no binary type: a byte string travels and is stored as its base64 text (a
    # string), under the same bound
    max_binary_bytes=8 * 1024 * 1024,
    # an array's elements are bounded by the document's size alone
    max_list_items=1_000_000,
    # no documented bound on attributes per document; chosen
    max_fields_per_record=1000,
    # chosen: a document of the doc data model embeds its relationships, and a
    # stream transaction's writes stay under `STREAMING_TRANSACTION_MAX_BYTES`
    max_record_bytes=4 * 1024 * 1024,
    # a collection holds as many documents as the disk takes
    max_rows_per_table=2**63 - 1,
    # chosen: a source document of the doc data model carries every relationship it
    # has as a link object of about a hundred bytes
    max_edges_per_node=20000,
    # chosen below the record bound; the probe checks a value of this size is accepted
    # as a unique persistent index key (RocksDB keys have no documented bound)
    max_index_key_bytes=4096,
    max_request_bytes=HTTP_MAX_LENGTH,
    # documents per bulk insert (the document API takes an array; chosen)
    max_statements_per_request=2000,
)
