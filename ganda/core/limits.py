"""What one engine can hold: the bounds the data generator keeps inside when it is
asked for large data.

Every number here is the ENGINE's (documented, or probed live on the version the
provider runs), never the fuzzer's appetite: a scale profile (`ganda.core.scale`)
decides how big an instance gets, and these say where it must stop so that a rejected
write is an engine finding rather than a limit the generator walked into. A provider
fills one `StorageLimits` in its `limits` slot and says, per field, where the number
comes from; a new engine plugs its own in and every large-data axis respects it.

The core consults the limits in exactly these places:

- `max_string_chars`, `max_binary_bytes`, `max_list_items`: the longest value the
  large-row axis grows a field to (`max_binary_bytes == 0`: the engine has no binary
  type, so `binary_probability` draws nothing);
- `max_index_key_bytes`: the longest value an INDEXED field may hold (an index key);
- `max_record_bytes`: the budget one record's grown values share;
- `max_fields_per_record`, `max_rows_per_table`, `max_edges_per_node`: caps on the
  schema shape, the class size and a hub's fan-out;
- `max_request_bytes`, `max_statements_per_request`: what the provider's own mapping
  and connection layer send in one request (the transport's bounds; a provider may
  configure its server to them).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass


@dataclass(frozen=True)
class StorageLimits:
    # the longest STRING value, in code points
    max_string_chars: int
    # the longest BINARY value, in bytes (0: no binary type)
    max_binary_bytes: int
    # the most elements in a LIST value
    max_list_items: int
    # the most declared fields one class may carry
    max_fields_per_record: int
    # the largest one record may be, every value together (its JSON)
    max_record_bytes: int
    # the most records one class may hold
    max_rows_per_table: int
    # the largest fan-out one record may hold (an embedded link list, an edge bag)
    max_edges_per_node: int
    # the largest value an indexed field may hold, in bytes of the value's UTF-8 form
    max_index_key_bytes: int
    # the largest payload one request may carry
    max_request_bytes: int
    # the most statements one script or batch may carry
    max_statements_per_request: int

    @classmethod
    def conservative(cls) -> "StorageLimits":
        """Bounds every reasonable engine meets: what a provider declares until it
        has probed its own (a skeleton provider's)."""
        return cls(
            max_string_chars=64 * 1024,
            max_binary_bytes=0,
            max_list_items=4096,
            max_fields_per_record=64,
            max_record_bytes=1024 * 1024,
            max_rows_per_table=1_000_000,
            max_edges_per_node=1000,
            max_index_key_bytes=1024,
            max_request_bytes=512 * 1024,
            max_statements_per_request=100,
        )

    def value_cap(self, ftype) -> int:
        """The longest value of `ftype` (a `FieldType`) one record may hold on its own:
        the type's bound, under the record and request budgets (half of each, so a
        grown value never fills a request by itself). In code points for a string, in
        bytes for a binary, in elements for a list; 0 when the type does not grow."""
        from ganda.core.model import FieldType

        budget = min(self.max_record_bytes, self.max_request_bytes) // 2
        if ftype is FieldType.STRING:
            return min(self.max_string_chars, budget)
        if ftype is FieldType.BINARY:
            # base64 carries 4 characters per 3 bytes
            return min(self.max_binary_bytes, budget * 3 // 4)
        if ftype is FieldType.LIST:
            # an element travels as up to 21 characters (a 64-bit integer, its sign
            # and the separator)
            return min(self.max_list_items, budget // 21)
        return 0

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)
