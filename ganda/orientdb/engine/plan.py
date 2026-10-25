"""OrientDB's setup-plan vocabulary: the steps `connection.OrientSession.apply_setup`
executes -- `Sql` (one command with named parameters), `Script` (a multi-statement SQL
script, `BEGIN; ...; COMMIT;` or `ROLLBACK;`) and `Batch` (the `/batch` endpoint,
optionally transactional). Every step is a plain dataclass with a JSON `to_dict()`, so a
bug bundle records the exact plan and `repro.py` replays it.

**RIDs never appear in a plan.** A record is addressed by its logical `id`; where a
statement needs a RID (a LINK inside an embedded document, `CREATE EDGE FROM :a TO :b`)
the parameter holds a `Ref("n1/r0")` placeholder, and the connection substitutes the RID
it captured when the step that inserted that record ran (`Sql.capture` names the id to
record). A plan is therefore the same text on every replay although every database
assigns its own RIDs. The mappers that write plans are `concrete_syntax/setup/`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ganda.core.engine.setup_plan import decode_refs, encode_refs, step as register_step, step_from_dict


@register_step("sql")
@dataclass
class Sql:
    """One SQL command with named parameters. `capture` is the logical id whose RID
    the connection layer records from the command's result (an INSERT / CREATE VERTEX returns
    the record with its `@rid`)."""

    command: str
    params: dict = field(default_factory=dict)
    capture: "str | None" = None

    def to_dict(self) -> dict:
        return {"op": "sql", "command": self.command, "params": encode_refs(self.params), "capture": self.capture}

    @classmethod
    def from_dict(cls, d: dict) -> "Sql":
        return cls(d["command"], decode_refs(d.get("params", {})), d.get("capture"))


@register_step("script")
@dataclass
class Script:
    """A multi-statement script (`POST /command/<db>/script`). `params` are shared by
    every statement in it. `captures` maps a script variable name to the logical id
    whose RID that variable holds (the script ends with `RETURN` of those)."""

    text: str
    params: dict = field(default_factory=dict)
    captures: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"op": "script", "text": self.text, "params": encode_refs(self.params), "captures": dict(self.captures)}

    @classmethod
    def from_dict(cls, d: dict) -> "Script":
        return cls(d["text"], decode_refs(d.get("params", {})), d.get("captures", {}))


@register_step("batch")
@dataclass
class Batch:
    """The `/batch` endpoint: `steps` are `Sql` commands run in one request, in one
    transaction when `transaction` (probed: a failing command rolls the batch back)."""

    steps: list
    transaction: bool = True

    def to_dict(self) -> dict:
        return {"op": "batch", "transaction": self.transaction, "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict) -> "Batch":
        return cls([step_from_dict(s) for s in d["steps"]], d.get("transaction", True))


SetupPlan = list
