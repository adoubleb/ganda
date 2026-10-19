"""What SAP HANA Cloud can hold (`ganda.core.data.limits.StorageLimits`), and where each number
comes from: the SQL reference (documented), or a bound the provider chooses below a
documented one so that all three data models stay inside it. None is probed yet
(`ganda hana probe` checks the ones a campaign walks into: the string and list bounds).
"""
from __future__ import annotations

from ganda.core.data.limits import StorageLimits
from ganda.hana.dialect import NVARCHAR_MAX

LIMITS = StorageLimits(
    # documented: NVARCHAR holds at most 5000 characters; a longer value is an NCLOB,
    # which takes no comparison and no index
    max_string_chars=NVARCHAR_MAX,
    # no binary type in the model yet (`config.UNSUPPORTED_AXES`)
    max_binary_bytes=0,
    # a list's canonical JSON text is an NVARCHAR(5000) on `rel`: an element travels
    # as up to 21 characters
    max_list_items=200,
    # documented: a column table may have 1000 columns; the model's records are small
    max_fields_per_record=64,
    # chosen: a document of the doc data model embeds its relationships, and the rdf
    # data model's setup sends its triples in requests under `max_request_bytes`
    max_record_bytes=1024 * 1024,
    # documented: 2 billion rows per partition of a column table
    max_rows_per_table=2_000_000_000,
    # chosen: a source document carries every relationship it has, so its fan-out
    # bounds the document's size
    max_edges_per_node=2000,
    # chosen below the NVARCHAR bound: an indexed string field stays a few kilobytes
    max_index_key_bytes=4096,
    # chosen: one SPARQL update or one SQL statement's text
    max_request_bytes=1024 * 1024,
    # chosen: rows per batch insert (`executemany`), triples per SPARQL update
    max_statements_per_request=500,
)
