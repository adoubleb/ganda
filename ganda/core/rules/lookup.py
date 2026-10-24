"""How provider code reads its catalog, so that every read is a call a static scan finds
(`reads`):

    answer(question, subject)              the answer, where the code branches on it
    refused(construct, subject)            `accepts.<construct>` answers refused
    alike(question, a, b)                  two subjects answer the question alike
    assumes(question, subject, expected)   the code has no branch for another answer: a
                                           catalog that says otherwise fails the import
    works_around(defect)                   the code avoids this defect's shape

The question is its dotted name, the subject a member of the catalog's subjects (or
`IR`)."""
from __future__ import annotations

import ast
import dataclasses
import pathlib

from ganda.core.rules.model import IR, Catalog, Verdict

READERS = ("answer", "refused", "alike", "assumes", "works_around")


class RuleMismatch(AssertionError):
    """Code assumes an answer the catalog does not give."""


class Lookup:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self._answers = {(r.question.name, r.subject): r.answer for r in catalog.rules}
        self._defects = {d.id for d in catalog.defects}

    def answer(self, question: str, subject):
        try:
            return self._answers[(question, subject)]
        except KeyError:
            raise KeyError(f"{self.catalog.engine} has no rule {question} / {subject}") from None

    def refused(self, construct: str, subject) -> bool:
        return self.answer(f"accepts.{construct}", subject) is Verdict.REFUSED

    def alike(self, question: str, a, b) -> bool:
        return self.answer(question, a) == self.answer(question, b)

    def assumes(self, question: str, subject, expected) -> None:
        got = self.answer(question, subject)
        if got != expected:
            raise RuleMismatch(f"the code assumes {question} / {subject} = {expected!r}; {self.catalog.engine}'s catalog says {got!r}")

    def works_around(self, defect: str) -> None:
        if defect not in self._defects:
            raise KeyError(f"{self.catalog.engine} has no defect {defect}")


# --- the static scan --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Read:
    path: str
    line: int
    reader: str
    question: str  # a question's name, or a defect id for `works_around`
    subjects: tuple


def reads(catalog: Catalog, root: pathlib.Path, sources: "list[str]", exclude: "tuple[str, ...]" = ()) -> "list[Read]":
    """Every lookup call in the sources: the reader, its question (or defect) and the
    subjects it names (a subject written as its member name: `OPENCYPHER`, `Subject.SQL`,
    `IR`)."""
    names = {m.name: m for m in catalog.subjects} | {"IR": IR}
    out = []
    for src in sources:
        base = root / src
        files = sorted(base.rglob("*.py")) if base.is_dir() else [base]
        for path in files:
            rel = path.relative_to(root)
            if any(part in exclude for part in rel.parts):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
                if fn not in READERS or not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                    continue
                first = node.args[0].value
                question = f"accepts.{first}" if fn == "refused" else first
                subjects = tuple(names[n] for a in node.args[1:] if (n := _name(a)) in names)
                out.append(Read(str(rel), node.lineno, fn, question, subjects))
    return out


def _name(node) -> "str | None":
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def read_by(catalog: Catalog, all_reads: "list[Read]") -> dict:
    """{(question name, subject): [Read]} for every rule, and {defect id: [Read]}."""
    rules = {(r.question.name, r.subject): [] for r in catalog.rules}
    defects = {d.id: [] for d in catalog.defects}
    for rd in all_reads:
        if rd.reader == "works_around":
            defects.setdefault(rd.question, []).append(rd)
            continue
        for s in rd.subjects:
            rules.setdefault((rd.question, s), []).append(rd)
    return {"rules": rules, "defects": defects}
