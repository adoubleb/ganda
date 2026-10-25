"""The OrientDB side of the session contract, without a server: which door a text
goes through, and that a drawn write's statements run through the right calls."""
from ganda.core.query.generators.write import sample_write
from ganda.core.query.generators.write_phase import build_write_case
from ganda.core.query.ir.write import apply
from ganda.core.engine.provider import EngineCrash, EngineError, Server, Session
from ganda.orientdb import PROVIDER
from ganda.orientdb.concrete_syntax.query import write as write_syntax
from ganda.orientdb.engine.connection import OrientCrash, OrientError, OrientServer, OrientSession
from ganda.orientdb.facade.generate import GeneratorConfig, build


class _Recorder(OrientSession):
    def __init__(self):
        self.calls = []
        self.rids = {}

    def sql(self, command, params=None, timeout_ms=30000):
        self.calls.append(("sql", command))
        return [{"count": 0}]

    def script(self, text, params=None):
        self.calls.append(("script", text))
        return [{"value": [{"count": 0, "@rid": "#1:0"}]}]


def test_the_orientdb_types_are_the_cores():
    assert issubclass(OrientSession, Session) and issubclass(OrientServer, Server)
    assert issubclass(OrientError, EngineError) and issubclass(OrientCrash, EngineCrash)
    assert OrientError("x", internal=True, status=501).internal


def test_execute_sends_a_script_through_the_script_door_and_unwraps_it():
    s = _Recorder()
    assert s.execute("SELECT FROM n1", {}) == [{"count": 0}] and s.calls[-1][0] == "sql"
    assert s.execute("BEGIN; UPDATE n1 SET f1 = 1; ROLLBACK; RETURN 1", {}) == [{"count": 0}] and s.calls[-1][0] == "script"
    assert s.explain("BEGIN; ROLLBACK;") is None
    assert PROVIDER.may_write(type("C", (), {"queries": {"doc": "BEGIN; x; ROLLBACK;"}})())
    assert not PROVIDER.may_write(type("C", (), {"queries": {"doc": "SELECT FROM n1"}})())


def test_a_drawn_write_runs_through_the_door_its_syntax_names():
    cfg = GeneratorConfig(max_node_types=3, max_edge_types=3, min_nodes_per_type=3, max_nodes_per_type=8, min_edges_per_type=3, max_edges_per_type=10)
    doors = set()
    for seed in range(1, 25):
        schema, instance, rng = build(seed, cfg)
        r = rng.for_generator("ir_write")
        inst = instance
        for step in range(4):
            drawn = sample_write(r, schema, inst, step)
            if drawn is None:
                continue
            q, rewrites, syntaxes = drawn
            after = apply(q, inst)
            case, statements = build_write_case(q, rewrites, syntaxes, step, inst, after, False)
            assert case.engine is PROVIDER
            for data_model, st in statements.items():
                if any("Ref(" in repr(v) for v in st.params.values()):
                    continue  # needs the record ids a live setup captures
                s = _Recorder()
                write_syntax.execute(s, st)
                calls = s.calls
                if st.positions is not None:
                    # the doc data model's longest list, read before the write
                    assert calls[0] == ("sql", st.positions[0])
                    calls = calls[1:]
                kinds = {kind for kind, _ in calls}
                assert kinds <= ({"script"} if st.door == "tx" else {"sql"}), (st.door, s.calls)
                doors.add(st.door)
            inst = after
    assert doors == {"auto", "tx"}
