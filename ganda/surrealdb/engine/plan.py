"""SurrealDB's setup-plan vocabulary: one step, `Surql`, a script of statements sent as
one `/sql` request (`connection.SurrealSession.apply_setup`). Records are addressed by
their record ids, which the mappers spell from the logical ids (`dialect.engine_id`), so
a plan needs no `Ref`: the same text runs on every replay.
"""
from __future__ import annotations

from dataclasses import dataclass

from ganda.core.engine.setup_plan import step as register_step


@register_step("surreal_sql")
@dataclass
class Surql:
    """Statements run in one request, in order; the request fails on the first that
    fails."""

    text: str

    def to_dict(self) -> dict:
        return {"op": "surreal_sql", "text": self.text}

    @classmethod
    def from_dict(cls, d: dict) -> "Surql":
        return cls(d["text"])


SetupPlan = list
