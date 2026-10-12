"""The layering rule: the core knows no engine, and every registered provider fills the
contract."""
import ast
import importlib
import pathlib

from ganda.core.limits import StorageLimits
from ganda.core.provider import Provider, Semantics
from ganda.registry import PROVIDERS, provider

ROOT = pathlib.Path(__file__).resolve().parent.parent / "ganda"
ENGINE_PACKAGES = tuple(f"ganda.{name}" for name in PROVIDERS)


def _imports(path: pathlib.Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_the_core_imports_no_provider():
    offenders = [(str(p.relative_to(ROOT)), m) for p in sorted((ROOT / "core").rglob("*.py")) for m in _imports(p) if m.startswith(ENGINE_PACKAGES)]
    assert not offenders, offenders


def test_the_core_imports_no_registry_at_module_level():
    # `replay` looks the engine up by name when it is called; nothing else may
    for p in sorted((ROOT / "core").rglob("*.py")):
        tree = ast.parse(p.read_text())
        top = [n.module for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "ganda.registry"]
        assert not top, p


def test_providers_do_not_import_each_other():
    for name in PROVIDERS:
        others = tuple(f"ganda.{n}" for n in PROVIDERS if n != name)
        offenders = [(str(p.relative_to(ROOT)), m) for p in sorted((ROOT / name).rglob("*.py")) for m in _imports(p) if m.startswith(others)]
        assert not offenders, offenders


def test_every_registered_provider_loads_and_names_itself():
    for name in PROVIDERS:
        engine = provider(name)
        assert isinstance(engine, Provider) and engine.name == name and engine.data_models
        assert engine.config_class is not None
        assert isinstance(engine.limits, StorageLimits), f"{name} declares no limits"
        assert callable(engine.server) and engine.database_name("ns", engine.data_models[0])


def test_a_full_provider_fills_every_slot():
    for name in PROVIDERS:
        engine = provider(name)
        if not engine.has_generators:
            continue
        assert isinstance(engine.semantics, Semantics)
        assert set(engine.mappers) == set(engine.data_models)
        for slot, members in {
            "pivots": ("NULL_OPS", "ORDERED_OPS", "LIKE_WILDCARDS", "ordered_ops", "cross_type", "refine"),
            "physical_design": ("draw_index_kind", "NON_UNIQUE_KINDS", "EDGE_INDEX_DATA_MODELS", "is_unique", "holds_one_record_per_key", "demoted_kind", "index_key_value", "draw_id_indexes", "draw_storage_options", "draw_write_form", "is_valid_key", "BOUNDARY_VALUES"),
            "traversal_syntax": ("syntaxes_for", "render", "can_pull_up"),
            "select_syntax": ("BASE", "METHODS", "method_fn", "build_rewrites", "extra_binds", "rewrite_params", "base_data_models", "syntaxes_for", "applicable", "render"),
            "path_syntax": ("render_all", "bind_vars"),
            "write_syntax": ("build_rewrites", "all_syntaxes", "applicable", "render", "execute", "shared_binds", "rewrite_params"),
            "rewrites": ("arith_exact",),
            "known_bugs": ("mark_known_bugs", "select_known", "write_known"),
        }.items():
            missing = [m for m in members if not hasattr(getattr(engine, slot), m)]
            assert not missing, (name, slot, missing)
        assert engine.mutants and len({m.name for m in engine.mutants}) == len(engine.mutants)


def test_the_fake_engine_fills_the_same_contract():
    fake = importlib.import_module("tests.fake_engine").FakeProvider()
    assert isinstance(fake, Provider) and isinstance(fake.semantics, Semantics) and isinstance(fake.limits, StorageLimits)



def test_every_providers_setup_steps_load_side_by_side():
    # the step registry is shared: two providers' plans must both round-trip in one process
    from ganda.core.setup_plan import plan_from_dict, plan_to_dict

    for name in PROVIDERS:
        engine = provider(name)
        if engine.has_generators:
            assert engine.mappers
    from ganda.orientdb.mapping.common import Sql as OrientSql

    assert type(plan_from_dict(plan_to_dict([OrientSql("SELECT 1")]))[0]) is OrientSql
