"""The transaction door's text (`SelectQuery.tx`): a question read inside a stream
transaction that first writes one record of the class and is then aborted. A door text
is a SCRIPT: `TX_MARK`, the transaction's collections as JSON, then the statements
separated by `SEPARATOR`, the question last. The session (`connection.ArangoSession`)
begins the transaction, runs the statements and the question inside it, and aborts
it whatever happened."""
from __future__ import annotations

import json

TX_MARK = "/* ganda:tx */"
SEPARATOR = "\n;;\n"


def script(collections: dict, statements: list, question: str) -> str:
    """`collections`: `{"write": [...], "read": [...]}` as `/_api/transaction/begin` takes them."""
    return TX_MARK + " " + json.dumps(collections, sort_keys=True) + SEPARATOR + SEPARATOR.join(statements + [question])


def is_script(text: str) -> bool:
    return text.startswith(TX_MARK)


def parts(text: str) -> "tuple[dict, list, str]":
    """(collections, statements, question) of a door text."""
    head, *rest = text[len(TX_MARK):].strip().split(SEPARATOR)
    return json.loads(head), rest[:-1], rest[-1]
