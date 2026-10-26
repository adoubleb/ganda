"""The SurrealDB campaign configuration: the core's axes plus this engine's own."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ganda.core.data.generator import GeneratorConfig


@dataclass
class SurrealConfig(GeneratorConfig):
    HELP: ClassVar[dict] = {
        "schemafull_probability": "chance a class is SCHEMAFULL (every field declared with its type) on a data model instead of SCHEMALESS; drawn per class and data model",
    }

    # SurrealDB has no case-insensitive collation: the axis is off
    collate_probability: float = 0.0
    # a class declared SCHEMAFULL on a data model, each field `option<type | null>`
    schemafull_probability: float = 0.25
