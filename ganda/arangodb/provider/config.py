"""The ArangoDB campaign configuration; `collate_probability` and the clusters axis are
inert here."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ganda.core.data.generator import GeneratorConfig


@dataclass
class ArangoConfig(GeneratorConfig):
    HELP: ClassVar[dict] = {
        "id_index_probability": "chance the id index is a non-unique kind instead of the unique persistent index",
    }

    # the index on `id`, per data model: one of `dialect.ID_INDEX_KINDS` instead of the
    # unique persistent index
    id_index_probability: float = 0.5
