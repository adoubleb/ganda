"""SurrealDB's setup step `Surql`, a script sent as one `/sql` request; records are addressed by record id, so no `Ref`."""
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
