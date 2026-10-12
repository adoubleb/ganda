"""The ArangoDB campaign configuration: the core's axes plus this engine's own.

Two core axes are INERT here rather than refused: `collate_probability` (ArangoDB has
no per-attribute collation, so a `ci` field is an ordinary string field whose values
collide case-wise on purpose and whose comparisons are case-sensitive) and the
clusters axis (`physical_design.draw_storage_options`: a single server has no
partitions to spread a collection over).
"""
from __future__ import annotations

from dataclasses import dataclass

from ganda.core.data_generator import GeneratorConfig


@dataclass
class ArangoConfig(GeneratorConfig):
    # the index on `id`, per data model: one of `dialect.ID_INDEX_KINDS` instead of the
    # unique persistent index
    id_index_probability: float = 0.5
