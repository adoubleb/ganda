"""The OrientDB campaign configuration: the core's axes plus this engine's own."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ganda.core.data.generator import GeneratorConfig


@dataclass
class OrientConfig(GeneratorConfig):
    HELP: ClassVar[dict] = {
        "id_index_probability": "chance the id index is a non-UNIQUE kind drawn from the dialect's id index kinds",
        "clusters_probability": "chance a class is spread over several physical clusters",
    }

    # the index on `id`, per data model: one of `dialect.ID_INDEX_KINDS` instead of UNIQUE
    id_index_probability: float = 0.5
    # a class spread over several physical clusters
    clusters_probability: float = 0.3
