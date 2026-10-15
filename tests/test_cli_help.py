"""Every flag of `run` says what it does in `--help`: the config fields carry their text
in the config class's `HELP`, the campaign's own flags in `campaign.add_arguments`."""
import argparse

import pytest

from ganda.core import campaign
from ganda.registry import provider


@pytest.mark.parametrize("name", ["orientdb", "arangodb", "hana"])
def test_every_run_flag_has_help_text(name):
    engine = provider(name)
    ap = argparse.ArgumentParser(prog=name)
    campaign.add_arguments(ap, engine)
    missing = [a.option_strings[0] for a in ap._actions if a.option_strings and a.dest != "help" and not (a.help or "").strip()]
    assert not missing, f"flags without help text: {missing}"


@pytest.mark.parametrize("name", ["orientdb", "arangodb", "hana"])
def test_every_config_field_has_its_own_description(name):
    import dataclasses

    from ganda.core.config import help_of

    cfg = provider(name).config_class
    text = help_of(cfg)
    assert [f.name for f in dataclasses.fields(cfg) if not text.get(f.name)] == []
