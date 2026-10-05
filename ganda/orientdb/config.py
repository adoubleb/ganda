"""The OrientDB campaign configuration: the core's axes plus this engine's own."""
from __future__ import annotations

from dataclasses import dataclass

from ganda.core.data_generator import GeneratorConfig


@dataclass
class OrientConfig(GeneratorConfig):
    # the index on `id`, per data model: one of `dialect.ID_INDEX_KINDS` instead of UNIQUE
    id_index_probability: float = 0.5
    # a class spread over several physical clusters
    clusters_probability: float = 0.0
