"""Engine rules as typed records.

A rule is an `Answer`: one engine's answer to one `Question` in one query language (and,
where it matters, one data model), with its basis and its evidence. A `Defect` is an
engine bug: its trigger, its effect and how the provider handles it. An `ErrorClass`
reads the engine's error codes. A provider's `Catalog` holds all three; every field is
data, checked by `validate`."""
from __future__ import annotations

import dataclasses
import enum
import re
from typing import Any

IDENTIFIER = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")


class Basis(enum.StrEnum):
    DEFINITION = "definition"  # the core IR's own semantics (subject `IR`)
    VENDOR_DOC = "vendor-doc"
    ASSUMED = "assumed"
    OBSERVED = "observed"


class Verdict(enum.StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"


class Effect(enum.StrEnum):
    WRONG_ANSWER = "wrong-answer"
    LOST_PRECISION = "lost-precision"
    REFUSAL = "refusal"
    INTERNAL_ERROR = "internal-error"
    OVERFLOW = "overflow"


class Handling(enum.StrEnum):
    WORKED_AROUND = "worked-around"
    WITHHELD = "withheld"
    NOT_MET = "not-met"


class ErrorKind(enum.StrEnum):
    CONNECTION_LOST = "connection-lost"
    TIMEOUT = "timeout"
    INTERNAL = "internal"
    REJECTION = "rejection"


class Subject(enum.StrEnum):
    IR = "ir"


IR = Subject.IR


# --- evidence ----------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Standard:
    document: str
    section: str


@dataclasses.dataclass(frozen=True)
class Tck:
    feature: str
    scenario: int


@dataclasses.dataclass(frozen=True)
class VendorDoc:
    document: str
    edition: str
    topic: "str | None" = None


@dataclasses.dataclass(frozen=True)
class Finding:
    id: str


@dataclasses.dataclass(frozen=True)
class Campaign:
    tag: str


Ref = Standard | Tck | VendorDoc | Finding | Campaign


# --- rules -------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Question:
    name: str
    domain: Any  # an Enum class, bool, int or tuple


@dataclasses.dataclass(frozen=True)
class Answer:
    question: Question
    language: enum.Enum
    value: Any
    basis: Basis
    facts: "tuple[str, ...]" = ()
    refs: "tuple[Ref, ...]" = ()
    data_model: "str | None" = None
    error: "int | None" = None  # the engine's error code of a REFUSED verdict
    documented: Any = None  # the vendor's documented answer, where the engine departs from it
    standard: Any = None  # the standard's answer, where the engine departs from it

    @property
    def subject(self) -> tuple:
        return (self.question.name, self.language, self.data_model)


@dataclasses.dataclass(frozen=True)
class Defect:
    id: str
    language: enum.Enum
    trigger: str
    effect: Effect
    handling: Handling
    data_model: "str | None" = None
    error: "int | None" = None
    facts: "tuple[str, ...]" = ()
    refs: "tuple[Ref, ...]" = ()
    aliases: "tuple[str, ...]" = ()


@dataclasses.dataclass(frozen=True)
class ErrorClass:
    """One entry of an ordered classification: the first entry whose codes and marks
    match an error's code and message gives its kind."""

    kind: ErrorKind
    codes: "frozenset[int]" = frozenset()  # empty: any code
    marks: "tuple[str, ...]" = ()  # fragments of the engine's message; empty: any message
    all_marks: bool = False  # every mark must occur (else one)
    ignore_case: bool = False
    facts: "tuple[str, ...]" = ()
    refs: "tuple[Ref, ...]" = ()

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
    languages: type
    answers: "tuple[Answer, ...]"
    defects: "tuple[Defect, ...]" = ()
    errors: "tuple[ErrorClass, ...]" = ()

    def answer(self, question: Question, language, data_model: "str | None" = None) -> "Answer | None":
        for a in self.answers:
            if a.question == question and a.language == language and a.data_model == data_model:
                return a
        return None

    def questions(self) -> "list[Question]":
        seen: dict = {}
        for a in self.answers:
            seen.setdefault(a.question.name, a.question)
        return list(seen.values())

    def divergences(self) -> "list[tuple[Question, list[Answer]]]":
        """The questions two subjects answer differently, with every answer to them."""
        out = []
        for q in self.questions():
            answers = [a for a in self.answers if a.question == q]
            if len({_hashable(a.value) for a in answers}) > 1:
                out.append((q, answers))
        return out

    def classify(self, code: int, message: str) -> "ErrorKind | None":
        for e in self.errors:
            if e.matches(code, message):
                return e.kind
        return None

    def fact_ids(self) -> "set[str]":
        return {f for a in self.answers for f in a.facts} | {f for d in self.defects for f in d.facts} | {f for e in self.errors for f in e.facts}

    def refs(self) -> "set":
        return {r for a in self.answers for r in a.refs} | {r for d in self.defects for r in d.refs} | {r for e in self.errors for r in e.refs}


def _hashable(v):
    return tuple(v) if isinstance(v, list) else v


# --- validation --------------------------------------------------------------------------------


def in_domain(value, domain) -> bool:
    if isinstance(domain, type) and issubclass(domain, enum.Enum):
        return isinstance(value, domain)
    if domain is bool:
        return isinstance(value, bool)
    if domain is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if domain is tuple:
        return isinstance(value, tuple)
    return False


def validate(catalog: Catalog) -> "list[str]":
    """Every way the catalog breaks its own schema; empty when it is well formed."""
    bad = []
    seen: dict = {}
    for a in catalog.answers:
        where = f"{a.question.name} / {a.language} / {a.data_model}"
        if not IDENTIFIER.match(a.question.name):
            bad.append(f"{where}: the question's name is no dotted identifier")
        if a.subject in seen:
            bad.append(f"{where}: answered twice")
        seen[a.subject] = a
        if a.language is not IR and not isinstance(a.language, catalog.languages):
            bad.append(f"{where}: {a.language!r} is no language of {catalog.engine}")
        if not in_domain(a.value, a.question.domain):
            bad.append(f"{where}: {a.value!r} is outside the domain {a.question.domain!r}")
        for name in ("documented", "standard"):
            other = getattr(a, name)
            if other is not None and (not in_domain(other, a.question.domain) or other == a.value):
                bad.append(f"{where}: `{name}` must be another value of the domain")
        if (a.basis is Basis.DEFINITION) != (a.language is IR):
            bad.append(f"{where}: the IR's answers, and only they, are definitions")
        if a.basis is Basis.OBSERVED and not a.facts and not any(isinstance(r, (Campaign, Finding)) for r in a.refs):
            bad.append(f"{where}: an observed answer names the probe facts, campaign or finding that observed it")
        if a.basis is Basis.VENDOR_DOC and not any(isinstance(r, VendorDoc) for r in a.refs):
            bad.append(f"{where}: a documented answer cites the vendor's document")
        if a.error is not None and a.value is not Verdict.REFUSED:
            bad.append(f"{where}: only a refusal carries an error code")
    for d in catalog.defects:
        if not IDENTIFIER.match(d.trigger):
            bad.append(f"defect {d.id}: the trigger is no dotted identifier")
        if not isinstance(d.language, catalog.languages):
            bad.append(f"defect {d.id}: {d.language!r} is no language of {catalog.engine}")
        if not any(isinstance(r, Finding) for r in d.refs):
            bad.append(f"defect {d.id}: a defect cites its finding")
    ids = [d.id for d in catalog.defects] + [a for d in catalog.defects for a in d.aliases]
    if len(ids) != len(set(ids)):
        bad.append("a defect id or alias is used twice")
    return bad
