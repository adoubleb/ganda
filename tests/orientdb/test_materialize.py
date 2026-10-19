import json

from ganda.orientdb.generate import GeneratorConfig, build
from ganda.orientdb.mapping import MAPPERS
from ganda.orientdb.mapping.common import Batch, Ref, Script, Sql, _groups, plan_from_dict, plan_to_dict, resolve_refs, wrap_data
from ganda.core.data.model import WriteForm


def _all_steps(plan):
    for s in plan:
        if isinstance(s, Batch):
            yield from s.steps
        else:
            yield s


def test_plans_round_trip_through_json():
    for seed in range(1, 12):
        _, inst, _ = build(seed, GeneratorConfig(write_form_probability=1.0, custom_key_probability=0.3))
        for data_model, mapper in MAPPERS.items():
            plan = mapper(inst)
            encoded = json.dumps(plan_to_dict(plan), ensure_ascii=False)
            assert plan_to_dict(plan_from_dict(json.loads(encoded))) == plan_to_dict(plan)


def test_every_ref_follows_its_capture():
    """A Ref may only appear after the step (or group) that captures its id."""
    for seed in range(1, 25):
        _, inst, _ = build(seed, GeneratorConfig(write_form_probability=1.0, churn_node_delete_probability=0.3))
        for data_model, mapper in MAPPERS.items():
            captured = set()
            for step in mapper(inst):
                units = step.steps if isinstance(step, Batch) else [step]
                if isinstance(step, Script):
                    refs = _refs(step.params)
                    assert refs <= captured, (seed, data_model, refs - captured)
                    captured |= set(step.captures.values())
                    continue
                if isinstance(step, Batch):
                    refs = set().union(*(_refs(s.params) for s in units)) if units else set()
                    assert refs <= captured, (seed, data_model)
                    captured |= {s.capture for s in units if s.capture}
                    continue
                assert _refs(step.params) <= captured, (seed, data_model, step.command)
                if step.capture:
                    captured.add(step.capture)


def _refs(value) -> set:
    if isinstance(value, Ref):
        return {value.id}
    if isinstance(value, list):
        return set().union(*(_refs(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(_refs(v) for v in value.values())) if value else set()
    return set()


def test_resolve_refs():
    assert resolve_refs({"a": [Ref("x"), {"b": Ref("y")}], "c": 1}, {"x": "#1:0", "y": "#2:0"}) == {"a": ["#1:0", {"b": "#2:0"}], "c": 1}


def test_groups_split_before_a_ref_to_a_record_captured_in_the_run():
    steps = [Sql("INSERT a", {}, capture="n/a"), Sql("INSERT b", {}, capture="n/b"), Sql("LINK", {"t": Ref("n/a")}), Sql("INSERT c", {}, capture="n/c")]
    runs = _groups(steps)
    assert [len(r) for r in runs] == [2, 2]


def test_batch_form_keeps_complex_params_outside_the_batch():
    steps = [Sql("A", {"x": 1}), Sql("B", {"l": [1, 2]}), Sql("C", {"y": 2})]
    out = wrap_data(steps, WriteForm(transaction="batch"))
    kinds = [type(s).__name__ for s in out]
    assert kinds == ["Batch", "Sql", "Batch"]


def test_script_form_renames_parameters_per_statement():
    steps = [Sql("INSERT INTO t SET id = :id", {"id": "a"}, capture="t/a"), Sql("UPDATE t SET f = :v WHERE id = :id", {"v": 1, "id": "a"})]
    (script,) = wrap_data(steps, WriteForm(transaction="script"))
    assert script.text.startswith("BEGIN; LET $c0 = INSERT INTO t SET id = :s0_id; UPDATE t SET f = :s1_v WHERE id = :s1_id; COMMIT;")
    assert script.params == {"s0_id": "a", "s1_v": 1, "s1_id": "a"}
    assert script.captures == {"$c0": "t/a"}


def test_content_update_carries_no_parameter_and_resets_links():
    _, inst, _ = build(6, GeneratorConfig(write_form_probability=0.0, churn_update_probability=0.9))
    import dataclasses

    inst = dataclasses.replace(inst, write_forms={**inst.write_forms, "doc": WriteForm(update_form="content")})
    plan = MAPPERS["doc"](inst)
    contents = [s for s in _all_steps(plan) if isinstance(s, Sql) and " CONTENT " in s.command]
    assert contents
    for s in contents:
        assert ":doc" not in s.command and "Ref(" not in s.command


def _statements(script: Script) -> int:
    """How many statements a folded script carries (every data step has parameters,
    renamed `s<i>_...` per statement)."""
    return len({k.split("_", 1)[0] for k in script.params})


def test_chunked_doors_fold_every_run_into_bounded_requests():
    from ganda.orientdb.mapping import common

    steps = [Sql("INSERT INTO n1 SET id = :id, f = :f", {"id": f"n1/r{i}", "f": i}, capture=f"n1/r{i}") for i in range(12)]
    steps += [Sql("UPDATE n1 SET e1 = :links WHERE id = :id", {"links": [{"target": Ref(f"n1/r{i}")}], "id": f"n1/r{i}"}) for i in range(3)]
    plain = wrap_data(steps, WriteForm(transaction="none", chunk_size=5))
    assert all(isinstance(s, Script) and not s.text.startswith("BEGIN;") and s.text.endswith("RETURN 1;") for s in plain)
    assert [_statements(s) for s in plain] == [5, 5, 2, 3]  # the inserts, then the run the Refs open
    assert [sorted(s.captures.values()) for s in plain][:1] == [[f"n1/r{i}" for i in range(5)]]
    tx = wrap_data(steps, WriteForm(transaction="script", chunk_size=5))
    assert all(isinstance(s, Script) and s.text.startswith("BEGIN;") and "COMMIT;" in s.text for s in tx) and [_statements(s) for s in tx] == [5, 5, 2, 3]
    batched = wrap_data(steps, WriteForm(transaction="batch", chunk_size=5))
    assert [len(s.steps) for s in batched if isinstance(s, Batch)] == [5, 5, 2] and sum(isinstance(s, Sql) for s in batched) == 3  # the list parameters run plainly
    assert wrap_data(steps, WriteForm(transaction="none")) == steps  # no chunk size: the plain door is untouched
    assert len(wrap_data(steps, WriteForm(transaction="script"))) == 2  # one script per run, as before
    # the request budget splits a run too: a step too big for it travels alone
    budget = common._REQUEST_BUDGET
    common._REQUEST_BUDGET = 400
    try:
        heavy = [Sql("INSERT INTO n1 SET id = :id, s = :s", {"id": f"n1/r{i}", "s": "x" * (600 if i == 2 else 10)}, capture=f"n1/r{i}") for i in range(6)]
        out = wrap_data(heavy, WriteForm(transaction="none", chunk_size=50))
        assert [_statements(s) for s in out][:3] == [2, 1, 3] or [_statements(s) for s in out] == [2, 1, 2, 1]
    finally:
        common._REQUEST_BUDGET = budget


def test_chunked_plans_keep_every_ref_after_its_capture_and_round_trip():
    for seed in range(1, 10):
        _, inst, _ = build(seed, GeneratorConfig(write_form_probability=1.0, churn_node_delete_probability=0.3, statements_per_request=6, min_nodes_per_type=12, max_nodes_per_type=20))
        for data_model, mapper in MAPPERS.items():
            plan = mapper(inst)
            assert inst.write_form(data_model).chunk_size == 6
            captured = set()
            for step in plan:
                if isinstance(step, Script):
                    assert _refs(step.params) <= captured, (seed, data_model)
                    assert _statements(step) <= 6
                    captured |= set(step.captures.values())
                elif isinstance(step, Batch):
                    assert len(step.steps) <= 6 and set().union(*(_refs(s.params) for s in step.steps)) <= captured
                    captured |= {s.capture for s in step.steps if s.capture}
                else:
                    assert _refs(step.params) <= captured
                    if step.capture:
                        captured.add(step.capture)
            data = [s for s in plan if isinstance(s, Sql) and s.command.split()[0] in ("INSERT", "CREATE", "UPDATE", "DELETE") and s.command.split()[1] not in ("CLASS", "PROPERTY", "INDEX")]
            # under the plain door every data step travels inside a script
            if inst.write_form(data_model).transaction == "none":
                assert not data, (seed, data_model, data[:2])
            assert plan_to_dict(plan_from_dict(json.loads(json.dumps(plan_to_dict(plan), ensure_ascii=False)))) == plan_to_dict(plan)
