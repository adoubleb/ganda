"""Engine rules as minimal records: what the fuzzer acts on so that it asks and compares
only what an engine's languages answer alike.

A `Rule` is one subject's (a language, optionally on a data model) answer to one
`Question`; the question's `Kind` says what the fuzzer does with the answers. A
`Defect` names an engine bug the known-bug guards withhold. An `ErrorClass` is one entry
of the ordered error classification. A probe fact CHECKS rules and error classes, and
reproduces defects (`Catalog.checks`): a rule no fact checks is unchecked, and the rendering says so; a
fact reproducing a defect that fails says the defect is gone."""
from __future__ import annotations

import dataclasses
import enum
import re
from typing import Any

IDENTIFIER = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")


class Kind(enum.StrEnum):
    COMPARE = "compare"  # subjects answering differently are not compared where the question is exercised
    ASK = "ask"  # a construct a subject refuses is not rendered
    EQUIVALENT = "equivalent"  # twin forms answer alike: a mismatch between them is a finding
    EXACT = "exact"  # where a value compares exactly: a bound, or whether at all


class Verdict(enum.StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"


class ErrorKind(enum.StrEnum):
    CONNECTION_LOST = "connection-lost"
    TIMEOUT = "timeout"
    INTERNAL = "internal"
    REJECTION = "rejection"


class Core(enum.StrEnum):
    IR = "ir"


IR = Core.IR


@dataclasses.dataclass(frozen=True)
class Question:
    name: str
    domain: Any  # an Enum class, bool, int, float or str
    kind: Kind


@dataclasses.dataclass(frozen=True)
class Rule:
    question: Question
    subject: enum.Enum
    answer: Any


@dataclasses.dataclass(frozen=True)
class Defect:
    id: str
    subject: enum.Enum


@dataclasses.dataclass(frozen=True)
class ErrorClass:
    """The first entry whose codes and marks match an error gives its kind."""

    kind: ErrorKind
    codes: "frozenset[int]" = frozenset()  # empty: any code
    marks: "tuple[str, ...]" = ()  # fragments of the engine's message; empty: any message
    all_marks: bool = False  # every mark must occur (else one)
    ignore_case: bool = False

    def matches(self, code: int, message: str) -> bool:
        if self.codes and code not in self.codes:
            return False
        if not self.marks:
            return True
        text = message.lower() if self.ignore_case else message
        hits = [(m.lower() if self.ignore_case else m) in text for m in self.marks]
        return all(hits) if self.all_marks else any(hits)


@dataclasses.dataclass(frozen=True)
class Catalog:
    engine: str
    subjects: type
    rules: "tuple[Rule, ...]"
    defects: "tuple[Defect, ...]" = ()
    errors: "tuple[ErrorClass, ...]" = ()
    checks: "dict" = dataclasses.field(default_factory=dict)  # {probe fact id: (Rule | Defect | ErrorClass, ...)}

    def answer(self, question: Question, subject):
        for r in self.rules:
            if r.question == question and r.subject == subject:
                return r.answer
        return None

    def questions(self) -> "list[Question]":
        seen: dict = {}
        for r in self.rules:
            seen.setdefault(r.question.name, r.question)
        return list(seen.values())

    def divergences(self) -> "list[tuple[Question, list[Rule]]]":
        """The compare questions two subjects answer differently."""
        out = []
        for q in self.questions():
            rules = [r for r in self.rules if r.question == q]
            if q.kind is Kind.COMPARE and len({r.answer for r in rules}) > 1:
                out.append((q, rules))
        return out

    def checked_by(self, item: "Rule | Defect | ErrorClass") -> "list[str]":
        return [fid for fid, items in self.checks.items() if item in items]

    def unchecked(self) -> "list[Rule]":
        checked = {r for items in self.checks.values() for r in items}
        return [r for r in self.rules if r.subject is not IR and r not in checked]

    def unreproduced(self) -> "list[Defect]":
        checked = {d for items in self.checks.values() for d in items}
        return [d for d in self.defects if d not in checked]

    def classify(self, code: int, message: str) -> "ErrorKind | None":
        for e in self.errors:
            if e.matches(code, message):
                return e.kind
        return None


def in_domain(value, domain) -> bool:
    if isinstance(domain, type) and issubclass(domain, enum.Enum):
        return isinstance(value, domain)
    if domain is bool:
        return isinstance(value, bool)
    if domain is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if domain is float:
        return isinstance(value, float)
    if domain is str:
        return isinstance(value, str) and value != ""
    return False


def validate(catalog: Catalog, fact_ids: "set | None" = None) -> "list[str]":
    """Every way the catalog breaks its own schema; empty when it is well formed."""
    bad = []
    seen = set()
    for r in catalog.rules:
        q = r.question
        where = f"{q.name} / {r.subject}"
        if not IDENTIFIER.match(q.name):
            bad.append(f"{where}: the question's name is no dotted identifier")
        if (q.name, r.subject) in seen:
            bad.append(f"{where}: answered twice")
        seen.add((q.name, r.subject))
        if r.subject is not IR and not isinstance(r.subject, catalog.subjects):
            bad.append(f"{where}: {r.subject!r} is no subject of {catalog.engine}")
        if not in_domain(r.answer, q.domain):
            bad.append(f"{where}: {r.answer!r} is outside the domain {q.domain!r}")
        if q.kind is Kind.ASK and q.domain is not Verdict:
            bad.append(f"{where}: an ask question is answered accepted or refused")
        if q.kind is Kind.EQUIVALENT and q.domain is not bool:
            bad.append(f"{where}: an equivalent question is answered true or false")
        if q.kind is Kind.EXACT and q.domain not in (bool, int):
            bad.append(f"{where}: an exact question is answered by a bound or true / false")
        if r.subject is IR and q.kind is not Kind.COMPARE:
            bad.append(f"{where}: the IR answers compare questions only")
    names: dict = {}
    for r in catalog.rules:
        if names.setdefault(r.question.name, r.question) != r.question:
            bad.append(f"{r.question.name}: two questions share the name")
    rules, defects = set(catalog.rules), set(catalog.defects)
    for fid, checked in catalog.checks.items():
        if fact_ids is not None and fid not in fact_ids:
            bad.append(f"check {fid}: no such probe fact")
        for r in checked:
            if isinstance(r, Defect):
                if r not in defects:
                    bad.append(f"check {fid}: {r.id} / {r.subject} is no defect of the catalog")
                continue
            if isinstance(r, ErrorClass):
                if r not in catalog.errors:
                    bad.append(f"check {fid}: the {r.kind} error class is not the catalog's")
                continue
            if r not in rules:
                bad.append(f"check {fid}: {r.question.name} / {r.subject} is no rule of the catalog")
            if r.subject is IR:
                bad.append(f"check {fid}: the IR's answers are definitions, not probed")
    ids = [d.id for d in catalog.defects]
    if len(ids) != len(set(ids)):
        bad.append("a defect id is used twice")
    for d in catalog.defects:
        if not isinstance(d.subject, catalog.subjects):
            bad.append(f"defect {d.id}: {d.subject!r} is no subject of {catalog.engine}")
    return bad
