"""openCypher over SQLite: the offline stand-in of SAP HANA's `OPENCYPHER_TABLE`.

The graph data model's texts are SQL around `OPENCYPHER_TABLE(GRAPH WORKSPACE gw QUERY
'<cypher>' PARAMETERS (...))`. `rewrite_sql` replaces every such call by an equivalent
SQLite subquery over the workspace's views (the same `gv_` views the live schema has),
so the rest of the text runs as the rel stand-in runs it; `$name` becomes the SQL
expression its PARAMETERS entry binds (`:name`, for `connection.positional`).

The translator covers exactly the subset the provider emits (`concrete_syntax.graph`):
MATCH clauses of vertices `(a:n1)`, directed edges `(a)-[e:e1]->(b)`, undirected ones,
variable-length paths `rp = (a)-[:e1*1..3]->(b)`, comma-separated patterns, WHERE with
comparisons / IS NULL / CONTAINS / STARTS WITH / ENDS WITH / arithmetic / NOT AND OR /
`$params`, and a RETURN of `expr AS alias` items with DISTINCT, COUNT / SUM / MAX / MIN,
ORDER BY, LIMIT and SKIP. Anything else raises `UnsupportedCypher` (`translatable` is
the runner's valve: such a variant is left out offline, as rdf's `native` is).

It mirrors the engine's semantics, each a switch read from the engine rules
(`ganda/hana/rules/catalog.py`): one MATCH clause only (a second one is the engine's refusal, raised
at run time); a relationship binds once within the clause (pairwise `eid <>` between its
edge variables); an undirected edge is both orientations but a self-loop once; a
variable-length path never repeats a relationship; SUM over a group of nulls is NULL;
ORDER BY puts NULL last ascending; an aggregate-only RETURN over no match answers one
row (SQLite's own); the RETURN is grouped by its non-aggregated items.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ganda.core.rules.vocabulary import EdgeBinding, EmptyAggregate, NullPlacement, PathMode
from ganda.hana.rules import answer, refused
from ganda.hana.rules.catalog import OPENCYPHER

# --- the engine's behaviour, read from its rules (`ganda/hana/rules/catalog.py`) ----------------

SELF_LOOP_TWICE = answer("match.undirected.self_loop.rows", OPENCYPHER) == 2
ISOMORPHIC_WITHIN_CLAUSE = answer("match.edge_binding", OPENCYPHER) is EdgeBinding.EDGE_ISOMORPHISM
ONE_CLAUSE_ONLY = refused("match.second_clause", OPENCYPHER)
TRAIL_PATHS = answer("path.variable_length.mode", OPENCYPHER) is PathMode.TRAIL
SUM_EMPTY_ZERO = answer("aggregate.sum.all_null_input", OPENCYPHER) is EmptyAggregate.ZERO
NULLS_FIRST_ASC = answer("order.nulls.asc", OPENCYPHER) is NullPlacement.FIRST


class UnsupportedCypher(ValueError):
    """A construct outside the subset the provider emits."""


# --- the workspace, as the setup step records it -----------------------------------------------


@dataclass
class Workspace:
    name: str
    vertices: dict  # label -> {"table", "key", "columns"}
    edges: dict  # label -> {"table", "key", "source", "source_table", "target", "target_table", "columns"}
    views: dict = field(default_factory=dict)  # view -> vertex label

    @classmethod
    def from_step(cls, step) -> "Workspace":
        ws = cls(step.name, {v["label"]: v for v in step.vertices}, {e["label"]: e for e in step.edges})
        ws.views = {v["table"]: label for label, v in ws.vertices.items()}
        return ws

    def vertex_view(self, label: str) -> str:
        if label not in self.vertices:
            raise UnsupportedCypher(f"no vertex label {label!r}")
        return self.vertices[label]["table"]

    def edge(self, label: str) -> dict:
        if label not in self.edges:
            raise UnsupportedCypher(f"no edge label {label!r}")
        return self.edges[label]

    def all_vertices(self) -> str:
        """Every vertex as (id, _label, <the attributes every vertex table has>): an
        unlabeled vertex. The shared attributes are the base class's under the
        inheritance axis (an attribute absent from a table would be NULL, G21; the
        renderers never ask one of an unlabelled vertex)."""
        shared = None
        for v in self.vertices.values():
            attrs = {a for c, a in v["columns"] if c != v["key"]}
            shared = attrs if shared is None else shared & attrs
        extra = "".join(f", {a.lower()}" for a in sorted(shared or ()))
        return "(" + " UNION ALL ".join(f"SELECT {v['key']} AS id, '{label}' AS _label{extra} FROM {v['table']}" for label, v in self.vertices.items()) + ")"

    def all_edges(self, undirected: bool) -> str:
        """Every relationship as (eid, src_id, dst_id) -- or, undirected, (eid, _near, _far)
        in both orientations: an unlabeled edge."""
        return "(" + " UNION ALL ".join(self._edge_rows(e, undirected, attrs=False) for e in self.edges.values()) + ")"

    def edge_rows(self, label: str, undirected: bool) -> str:
        e = self.edge(label)
        return e["table"] if not undirected else "(" + self._edge_rows(e, True, attrs=True) + ")"

    @staticmethod
    def _edge_rows(e: dict, undirected: bool, attrs: bool) -> str:
        k, s, t = e["key"], e["source"], e["target"]
        extra = "".join(f", {a}" for c, a in e["columns"] if c not in (k, s, t)) if attrs else ""
        if not undirected:
            return f"SELECT {k} AS eid, {s} AS src_id, {t} AS dst_id{extra} FROM {e['table']}"
        back = f"SELECT {k} AS eid, {t} AS _near, {s} AS _far{extra} FROM {e['table']}" + ("" if SELF_LOOP_TWICE else f" WHERE {s} <> {t}")
        # (a self-loop appears in the first half only: the engine matches it once, G7)
        return f"SELECT {k} AS eid, {s} AS _near, {t} AS _far{extra} FROM {e['table']} UNION ALL {back}"


# --- the SQL text: every OPENCYPHER_TABLE call replaced ------------------------------------------

_CALL = re.compile(r"OPENCYPHER_TABLE\(\s*GRAPH WORKSPACE\s+(\w+)\s+QUERY\s+'", re.I)


def _sql_literal_end(text: str, start: int) -> int:
    """The index just past the closing quote of a SQL string literal opened at `start`
    (its quotes doubled)."""
    i = start
    while True:
        j = text.index("'", i)
        if text[j + 1 : j + 2] == "'":
            i = j + 2
            continue
        return j + 1


def _split_top(s: str) -> list:
    out, depth, cur, quote = [], 0, [], False
    for ch in s:
        if ch == "'":
            quote = not quote
        if not quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                out.append("".join(cur).strip())
                cur = []
                continue
        cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def calls(text: str) -> list:
    """[(start, end, cypher, params)] of every OPENCYPHER_TABLE call of a SQL text."""
    out = []
    for m in _CALL.finditer(text):
        lit_start = m.end() - 1
        lit_end = _sql_literal_end(text, lit_start + 1)
        cypher = text[lit_start + 1 : lit_end - 1].replace("''", "'")
        i = lit_end
        params: dict = {}
        rest = text[i:]
        pm = re.match(r"\s*PARAMETERS\s*\(", rest)
        if pm:
            j = i + pm.end()
            depth = 1
            k = j
            while depth:
                if text[k] == "(":
                    depth += 1
                elif text[k] == ")":
                    depth -= 1
                k += 1
            for item in _split_top(text[j : k - 1]):
                name, _, expr = item.partition("=")
                params[name.strip().strip("'")] = expr.strip()
            i = k
        close = re.match(r"\s*\)", text[i:])
        if not close:
            raise UnsupportedCypher("OPENCYPHER_TABLE call not closed")
        out.append((m.start(), i + close.end(), cypher, params))
    return out


def rewrite_sql(text: str, ws: Workspace) -> str:
    out, pos = [], 0
    for start, end, cypher, params in calls(text):
        out.append(text[pos:start])
        out.append("(" + to_sql(cypher, params, ws) + ")")
        pos = end
    out.append(text[pos:])
    return "".join(out)


def translatable(text: str) -> bool:
    """Whether every call of a text parses (the runner's valve)."""
    try:
        for _s, _e, cypher, _p in calls(text):
            _Parser(cypher).query()
    except (UnsupportedCypher, ValueError, IndexError):
        return False
    return True


# --- tokens ---------------------------------------------------------------------------------

_TOKEN = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<str>'(?:[^'\\]|\\.)*')|(?P<param>\$[A-Za-z_]\w*)|(?P<id>[A-Za-z_]\w*|`[^`]*`)"
    r"|(?P<op><>|<=|>=|->|\.\.|[(),\[\]{}.:=<>+\-*/%^|]))"
)
KEYWORDS = {"MATCH", "WHERE", "RETURN", "DISTINCT", "ORDER", "BY", "ASC", "DESC", "LIMIT", "SKIP", "AND", "OR", "NOT", "IS", "NULL", "CONTAINS", "STARTS", "ENDS", "WITH", "AS", "IN", "ALL"}
AGGREGATES = ("COUNT", "SUM", "MAX", "MIN")
CASTS = {"TOINTEGER": "CAST(%s AS INTEGER)", "TOFLOAT": "CAST(%s AS REAL)", "TOSTRING": "CAST(%s AS TEXT)"}


def tokenize(text: str) -> list:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise UnsupportedCypher(f"cannot tokenize {text[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        value = m.group(kind)
        if kind == "id" and value.upper() in KEYWORDS:
            kind, value = "kw", value.upper()
        out.append((kind, value))
    return out


# --- the parser: a query as a small tree ------------------------------------------------------


@dataclass
class Node:
    var: "str | None"
    label: "str | None"


@dataclass
class Edge:
    var: "str | None"
    label: "str | None"
    directed: bool
    varlen: "tuple | None"  # (min, max)


@dataclass
class Clause:
    patterns: list  # [(path var or None, [Node, Edge, Node, ...])]
    where: "tuple | None"


@dataclass
class Query:
    clauses: list
    distinct: bool
    items: list  # [(expr, alias)]
    order: list  # [(expr, "ASC" | "DESC")]
    limit: "int | None"
    skip: "int | None"


class _Parser:
    def __init__(self, text: str):
        self.toks = tokenize(text)
        self.i = 0

    # -- helpers
    def peek(self, k: int = 0):
        return self.toks[self.i + k] if self.i + k < len(self.toks) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def accept(self, kind, value=None) -> bool:
        k, v = self.peek()
        if k == kind and (value is None or v == value):
            self.i += 1
            return True
        return False

    def expect(self, kind, value=None):
        if not self.accept(kind, value):
            raise UnsupportedCypher(f"expected {value or kind}, got {self.peek()}")

    def ident(self) -> str:
        k, v = self.take()
        if k != "id":
            raise UnsupportedCypher(f"expected a name, got {v!r}")
        return v.strip("`")

    # -- grammar
    def query(self) -> Query:
        clauses = []
        while self.accept("kw", "MATCH"):
            clauses.append(self.clause())
        if not clauses:
            raise UnsupportedCypher("no MATCH clause")
        self.expect("kw", "RETURN")
        distinct = self.accept("kw", "DISTINCT")
        items = [self.item()]
        while self.accept("op", ","):
            items.append(self.item())
        order = []
        if self.accept("kw", "ORDER"):
            self.expect("kw", "BY")
            order.append(self.order_item())
            while self.accept("op", ","):
                order.append(self.order_item())
        limit = skip = None
        if self.accept("kw", "LIMIT"):
            limit = int(self.take()[1])
        if self.accept("kw", "SKIP"):
            skip = int(self.take()[1])
        if self.peek()[0] is not None:
            raise UnsupportedCypher(f"trailing {self.peek()}")
        return Query(clauses, distinct, items, order, limit, skip)

    def clause(self) -> Clause:
        patterns = [self.pattern()]
        while self.accept("op", ","):
            patterns.append(self.pattern())
        where = self.expr() if self.accept("kw", "WHERE") else None
        return Clause(patterns, where)

    def pattern(self):
        path_var = None
        if self.peek()[0] == "id" and self.peek(1) == ("op", "="):
            path_var = self.ident()
            self.take()
        elements = [self.node()]
        while self.peek() == ("op", "-"):
            elements.append(self.edge())
            elements.append(self.node())
        return path_var, elements

    def node(self) -> Node:
        self.expect("op", "(")
        var = self.ident() if self.peek()[0] == "id" else None
        label = None
        if self.accept("op", ":"):
            label = self.ident()
        self.expect("op", ")")
        return Node(var, label)

    def edge(self) -> Edge:
        self.expect("op", "-")
        self.expect("op", "[")
        var = self.ident() if self.peek()[0] == "id" else None
        label = None
        if self.accept("op", ":"):
            label = self.ident()
        varlen = None
        if self.accept("op", "*"):
            lo = int(self.take()[1])
            self.expect("op", "..")
            hi = int(self.take()[1])
            varlen = (lo, hi)
        self.expect("op", "]")
        if self.accept("op", "->"):
            return Edge(var, label, True, varlen)
        self.expect("op", "-")
        return Edge(var, label, False, varlen)

    def item(self):
        e = self.expr()
        self.expect("kw", "AS")
        return e, self.ident()

    def order_item(self):
        e = self.expr()
        d = "DESC" if self.accept("kw", "DESC") else ("ASC" if self.accept("kw", "ASC") else "ASC")
        return e, d

    # expressions as tuples: ("or", a, b) ("and", a, b) ("not", a) ("cmp", op, a, b)
    # ("isnull", a) ("str", op, a, b) ("arith", op, a, b) ("neg", a) ("num", text)
    # ("lit", text) ("param", name) ("attr", var, name) ("agg", fn, arg | None) ("cast", fn, a)
    def expr(self):
        left = self.and_()
        while self.accept("kw", "OR"):
            left = ("or", left, self.and_())
        return left

    def and_(self):
        left = self.not_()
        while self.accept("kw", "AND"):
            left = ("and", left, self.not_())
        return left

    def not_(self):
        if self.accept("kw", "NOT"):
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        left = self.add()
        k, v = self.peek()
        if k == "op" and v in ("=", "<>", "<", ">", "<=", ">="):
            self.take()
            return ("cmp", v, left, self.add())
        if self.accept("kw", "IS"):
            neg = self.accept("kw", "NOT")
            self.expect("kw", "NULL")
            node = ("isnull", left)
            return ("not", node) if neg else node
        if self.accept("kw", "CONTAINS"):
            return ("str", "contains", left, self.add())
        if self.accept("kw", "STARTS"):
            self.expect("kw", "WITH")
            return ("str", "starts", left, self.add())
        if self.accept("kw", "ENDS"):
            self.expect("kw", "WITH")
            return ("str", "ends", left, self.add())
        return left

    def add(self):
        left = self.mul()
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            left = ("arith", op, left, self.mul())
        return left

    def mul(self):
        left = self.unary()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/", "%", "^"):
            op = self.take()[1]
            if op == "^":
                raise UnsupportedCypher("^")
            left = ("arith", op, left, self.unary())
        return left

    def unary(self):
        if self.accept("op", "-"):
            return ("neg", self.unary())
        return self.primary()

    def primary(self):
        k, v = self.take()
        if k == "num":
            return ("num", v)
        if k == "str":
            return ("lit", v)
        if k == "param":
            return ("param", v[1:])
        if k == "op" and v == "(":
            e = self.expr()
            self.expect("op", ")")
            return e
        if k == "id":
            name = v.strip("`")
            if self.peek() == ("op", "("):
                fn = name.upper()
                self.take()
                if fn in AGGREGATES:
                    if self.accept("op", "*"):
                        arg = None
                    else:
                        arg = self.expr()
                    self.expect("op", ")")
                    return ("agg", fn, arg)
                if fn in CASTS:
                    arg = self.expr()
                    self.expect("op", ")")
                    return ("cast", fn, arg)
                raise UnsupportedCypher(f"function {name}")
            self.expect("op", ".")
            return ("attr", name, self.ident())
        raise UnsupportedCypher(f"unexpected {v!r}")


# --- the SQL of a query -------------------------------------------------------------------------


def _has_agg(e) -> bool:
    if not isinstance(e, tuple):
        return False
    if e[0] == "agg":
        return True
    return any(_has_agg(x) for x in e[1:])


def _str_literal(text: str) -> str:
    """A cypher string literal ('...' with \\' and \\\\) as a SQL literal."""
    body = text[1:-1]
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            out.append(body[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "'" + "".join(out).replace("'", "''") + "'"


class _Translator:
    def __init__(self, ws: Workspace, params: dict):
        self.ws = ws
        self.params = params
        self.froms: list = []
        self.conds: list = []
        self.bound: dict = {}  # var -> ("vertex", alias) | ("edge", alias, undirected) | ("path", alias)
        self.counter = 0

    def fresh(self, prefix: str) -> str:
        self.counter += 1
        return f"_{prefix}{self.counter}"

    # -- patterns
    def bind_vertex(self, n: Node, hint_view: "str | None") -> str:
        if n.var is not None and n.var in self.bound:
            kind, alias, *_ = self.bound[n.var]
            if kind != "vertex":
                raise UnsupportedCypher(f"{n.var} is not a vertex")
            return alias
        alias = n.var or self.fresh("v")
        if n.label is not None:
            self.froms.append(f"{self.ws.vertex_view(n.label)} AS {alias}")
        elif hint_view is not None:
            self.froms.append(f"{hint_view} AS {alias}")
        else:
            self.froms.append(f"{self.ws.all_vertices()} AS {alias}")
        self.bound[alias] = ("vertex", alias)
        return alias

    def clause_sql(self, clause: Clause) -> None:
        edge_aliases = []
        for path_var, elements in clause.patterns:
            if path_var is not None:
                self.path_sql(path_var, elements)
                continue
            left = self.bind_vertex(elements[0], None)
            for i in range(1, len(elements), 2):
                edge, node = elements[i], elements[i + 1]
                if edge.varlen is not None:
                    raise UnsupportedCypher("a variable-length pattern needs a path variable")
                e = self.ws.edge(edge.label) if edge.label else None
                hint = (e["target_table"] if edge.directed else None) if e else None
                right_alias = self.bind_vertex(node, hint)
                if e is not None and edge.directed and node.label is None and node.var not in self.bound:
                    pass
                alias = edge.var or self.fresh("e")
                if edge.var is not None and edge.var in self.bound:
                    raise UnsupportedCypher(f"edge variable {edge.var} bound twice")
                rows = self.ws.edge_rows(edge.label, not edge.directed) if e else self.ws.all_edges(not edge.directed)
                self.froms.append(f"{rows} AS {alias}")
                if edge.directed:
                    self.conds.append(f"{alias}.src_id = {left}.id AND {alias}.dst_id = {right_alias}.id")
                else:
                    self.conds.append(f"{alias}._near = {left}.id AND {alias}._far = {right_alias}.id")
                self.bound[alias] = ("edge", alias, not edge.directed)
                edge_aliases.append(alias)
                left = right_alias
        if ISOMORPHIC_WITHIN_CLAUSE:
            for i, a in enumerate(edge_aliases):
                for b in edge_aliases[i + 1 :]:
                    self.conds.append(f"{a}.eid <> {b}.eid")
        if clause.where is not None:
            self.conds.append(self.expr(clause.where))

    def path_sql(self, path_var: str, elements: list) -> None:
        if len(elements) != 3 or elements[1].varlen is None:
            raise UnsupportedCypher("a path is one variable-length edge between two vertices")
        start, edge, end = elements
        lo, hi = edge.varlen
        undirected = not edge.directed
        rows = self.ws.edge_rows(edge.label, undirected) if edge.label else self.ws.all_edges(undirected)
        near, far = ("_near", "_far") if undirected else ("src_id", "dst_id")
        levels = []
        for d in range(lo, hi + 1):
            steps = [f"{rows} AS p{i}" for i in range(1, d + 1)]
            conds = [f"p{i}.{near} = p{i - 1}.{far}" for i in range(2, d + 1)]
            if TRAIL_PATHS:
                conds += [f"p{i}.eid <> p{j}.eid" for i in range(1, d + 1) for j in range(i + 1, d + 1)]
            where = f" WHERE {' AND '.join(conds)}" if conds else ""
            levels.append(f"SELECT p1.{near} AS _start, p{d}.{far} AS _end, {d} AS _len FROM {', '.join(steps)}{where}")
        alias = path_var
        self.froms.append("(" + " UNION ALL ".join(levels) + f") AS {alias}")
        e = self.ws.edge(edge.label) if edge.label else None
        s_alias = self.bind_vertex(start, e["source_table"] if e and not undirected else None)
        t_alias = self.bind_vertex(end, e["target_table"] if e and not undirected else None)
        self.conds.append(f"{alias}._start = {s_alias}.id AND {alias}._end = {t_alias}.id")
        self.bound[alias] = ("path", alias)

    # -- expressions
    def expr(self, e) -> str:
        kind = e[0]
        if kind == "or":
            return f"({self.expr(e[1])} OR {self.expr(e[2])})"
        if kind == "and":
            return f"({self.expr(e[1])} AND {self.expr(e[2])})"
        if kind == "not":
            return f"NOT ({self.expr(e[1])})"
        if kind == "cmp":
            return f"({self.expr(e[2])} {e[1]} {self.expr(e[3])})"
        if kind == "isnull":
            return f"({self.expr(e[1])} IS NULL)"
        if kind == "str":
            x, c = self.expr(e[2]), self.expr(e[3])
            if e[1] == "contains":
                return f"(instr({x}, {c}) > 0)"
            if e[1] == "starts":
                return f"(substr({x}, 1, length({c})) = {c})"
            return f"(substr({x}, -length({c})) = {c})"
        if kind == "arith":
            return f"({self.expr(e[2])} {e[1]} {self.expr(e[3])})"
        if kind == "neg":
            return f"(-{self.expr(e[1])})"
        if kind == "num":
            return e[1]
        if kind == "lit":
            return _str_literal(e[1])
        if kind == "param":
            if e[1] not in self.params:
                raise UnsupportedCypher(f"undeclared parameter ${e[1]}")
            return self.params[e[1]]
        if kind == "attr":
            var, name = e[1], e[2]
            if var not in self.bound:
                raise UnsupportedCypher(f"unbound variable {var}")
            if self.bound[var][0] == "path":
                raise UnsupportedCypher("a path has no attribute")
            return f"{var}.{name}"
        if kind == "agg":
            fn, arg = e[1], e[2]
            inner = "*" if arg is None else self.expr(arg)
            if fn == "SUM" and SUM_EMPTY_ZERO:
                return f"COALESCE(SUM({inner}), 0)"
            return f"{fn}({inner})"
        if kind == "cast":
            return CASTS[e[1]] % self.expr(e[2])
        raise UnsupportedCypher(kind)

    # -- the query
    def query_sql(self, q: Query) -> str:
        if ONE_CLAUSE_ONLY and len(q.clauses) > 1:
            # the engine's own refusal, as an error at run time (not a parse failure, so
            # the runner does not drop the variant: the text is wrong)
            raise UnsupportedCypher("For heterogeneous graphs only one match clause is allowed (G8)")
        for clause in q.clauses:
            self.clause_sql(clause)
        items = [f"{self.expr(e)} AS {alias}" for e, alias in q.items]
        aggregated = any(_has_agg(e) for e, _ in q.items)
        group = [self.expr(e) for e, _ in q.items if not _has_agg(e)] if aggregated else []
        sql = f"SELECT {'DISTINCT ' if q.distinct else ''}{', '.join(items)} FROM {', '.join(self.froms)}"
        if self.conds:
            sql += " WHERE " + " AND ".join(self.conds)
        if group:
            sql += " GROUP BY " + ", ".join(group)
        if q.order:
            parts = []
            for e, d in q.order:
                nulls = ("NULLS FIRST" if d == "ASC" else "NULLS LAST") if NULLS_FIRST_ASC else ("NULLS LAST" if d == "ASC" else "NULLS FIRST")
                parts.append(f"{self.expr(e)} {d} {nulls}")
            sql += " ORDER BY " + ", ".join(parts)
        if q.limit is not None or q.skip is not None:
            sql += f" LIMIT {q.limit if q.limit is not None else -1}"
            if q.skip:
                sql += f" OFFSET {q.skip}"
        return sql


def to_sql(cypher: str, params: dict, ws: Workspace) -> str:
    """One openCypher text as a SQLite SELECT over the workspace's views."""
    return _Translator(ws, params).query_sql(_Parser(cypher).query())
