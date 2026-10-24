"""The state check compares the data models' stored states with each other: the data
model off the consensus is the one with the problem, a tie names every one, and a
fault every data model shares is not seen."""
import dataclasses

from ganda.core.data.generator import build
from ganda.core.engine.state_check import compare_states
from tests.fake_engine import SMALL, FakeProvider


def _setup(seed: int = 3):
    engine = FakeProvider()
    schema, instance, _ = build(seed, engine.config_class(**SMALL), engine)
    server = engine.server()
    sessions = {}
    for dm in engine.data_models:
        sessions[dm] = server.fresh_session(engine.database_name("t", dm))
        sessions[dm].apply_setup(engine.mappers[dm](instance))
    return instance, sessions


def _set_value(session, record_id: str, value):
    rows = session.instance.nodes
    target = next(n for n in rows if n.record_id == record_id and n.values)
    field_name = next(iter(target.values))
    session.instance = dataclasses.replace(session.instance, nodes=[dataclasses.replace(n, values={**n.values, field_name: value}) if n is target else n for n in rows])
    return field_name


def test_the_data_model_off_the_consensus_has_the_problem():
    instance, sessions = _setup()
    assert all(d.ok for d in compare_states(sessions, instance).values())
    victim = next(n for n in instance.nodes if n.values)
    field_name = _set_value(sessions["graph"], victim.record_id, "damaged")
    diffs = compare_states(sessions, instance)
    assert diffs["doc"].ok and diffs["rel"].ok
    assert any(f"{victim.record_id}.{field_name}" in p and "doc, rel" in p for p in diffs["graph"].problems), diffs["graph"].problems


def test_a_missing_and_an_extra_record_are_named_on_their_data_model():
    instance, sessions = _setup()
    victim = instance.nodes[0]
    s = sessions["rel"]
    s.instance = dataclasses.replace(s.instance, nodes=[n for n in s.instance.nodes if n.record_id != victim.record_id], edges=[e for e in s.instance.edges if victim.record_id not in (e.from_record, e.to_record)])
    diffs = compare_states(sessions, instance)
    assert any(victim.record_id in p and "missing" in p for p in diffs["rel"].problems)
    assert diffs["doc"].ok and diffs["graph"].ok
    # the same record lost on two of three data models: the one still holding it is off
    t = sessions["graph"]
    t.instance = dataclasses.replace(t.instance, nodes=[n for n in t.instance.nodes if n.record_id != victim.record_id], edges=[e for e in t.instance.edges if victim.record_id not in (e.from_record, e.to_record)])
    diffs = compare_states(sessions, instance)
    assert any(victim.record_id in p and "unexpected" in p for p in diffs["doc"].problems)


def test_a_tie_names_every_data_model_and_a_shared_fault_is_not_seen():
    instance, sessions = _setup()
    two = {dm: sessions[dm] for dm in ("doc", "graph")}
    victim = next(n for n in instance.nodes if n.values)
    _set_value(sessions["graph"], victim.record_id, "damaged")
    diffs = compare_states(two, instance)
    assert not diffs["doc"].ok and not diffs["graph"].ok and "disagree" in diffs["doc"].problems[0]
    for dm in ("doc", "rel"):
        _set_value(sessions[dm], victim.record_id, "damaged")
    assert all(d.ok for d in compare_states(sessions, instance).values())


def test_a_null_consensus_is_still_a_consensus():
    instance, sessions = _setup()
    victim = next(n for n in instance.nodes if n.values)
    for dm in ("doc", "rel"):
        field_name = _set_value(sessions[dm], victim.record_id, None)
    diffs = compare_states(sessions, instance)
    assert diffs["doc"].ok and diffs["rel"].ok
    assert any(f"{victim.record_id}.{field_name}" in p and "store None" in p for p in diffs["graph"].problems), diffs["graph"].problems
