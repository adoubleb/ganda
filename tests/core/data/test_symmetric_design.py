"""The symmetric physical design: every index on every data model, one id-index kind, and
the provider's per-data-model storage draws alike -- with the seed's other draws unchanged."""
import dataclasses

import pytest

from ganda.core.data.generator import build
from ganda.registry import provider

SEEDS = range(1, 41)
AXES = dict(index_probability=0.6, composite_index_probability=0.4, edge_index_probability=0.4,
            list_index_probability=0.5, inheritance_probability=0.5, date_map_probability=0.5,
            id_index_probability=0.5)


def _cfgs(engine, **extra):
    fields = {f.name for f in dataclasses.fields(engine.config_class)}
    cfg = engine.config_class(**{k: v for k, v in AXES.items() if k in fields}, **extra)
    return cfg, dataclasses.replace(cfg, symmetric_physical_design=True)


@pytest.mark.parametrize("name", ["orientdb", "arangodb", "surrealdb", "hana"])
def test_every_index_and_id_index_alike_on_every_data_model(name):
    engine = provider(name)
    off, on = _cfgs(engine)
    asymmetric = 0
    for seed in SEEDS:
        before, _, _ = build(seed, off, engine)
        after, _, _ = build(seed, on, engine)
        asymmetric += sum(ix.data_model != "both" for ix in before.indexes)
        assert all(ix.data_model == "both" for ix in after.indexes)
        assert len(set(after.id_index.get(dm) for dm in engine.data_models)) == 1
        # nothing else moves: the same indexes (two of one name on different data models
        # now collide, and the later is dropped) and the same fields (SurrealDB's SCHEMAFULL
        # option is the one storage draw that moves)
        assert {ix.name() for ix in before.indexes} == {ix.name() for ix in after.indexes}
        assert [nt.fields for nt in before.node_types] == [nt.fields for nt in after.node_types]
        assert before.edge_types == after.edge_types
        if engine.data_models[0] in before.id_index:
            assert after.id_index == {dm: before.id_index[engine.data_models[0]] for dm in engine.data_models}
        else:
            assert after.id_index == {}
    assert asymmetric, "the seeds drew no one-data-model index to make symmetric"


def test_surrealdb_schemafull_alike_on_every_data_model():
    engine = provider("surrealdb")
    off, on = _cfgs(engine, schemafull_probability=0.5)
    first = engine.data_models[0]
    for seed in SEEDS:
        before, _, _ = build(seed, off, engine)
        after, _, _ = build(seed, on, engine)
        for b, a in zip(before.node_types, after.node_types):
            full = a.option("schemafull", ())
            assert full in ((), tuple(engine.data_models))
            assert bool(full) == (first in b.option("schemafull", ()))


def test_off_by_default():
    for name in ("orientdb", "arangodb", "surrealdb", "hana"):
        assert provider(name).config_class().symmetric_physical_design is False
