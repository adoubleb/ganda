"""Seed-for-seed parity with the legacy OrientDB fuzzer (`scripts/golden_dump.py`).

The baseline in `tests/golden/orientdb.json` was recorded from the legacy tree. A
slice of it runs here; `python3 scripts/golden_dump.py --check` runs all of it.
"""
import pytest

import golden_dump

GOLDEN = golden_dump.load_golden()
API = golden_dump._ganda_api()
SLICE = [(profile, seed) for profile in GOLDEN for seed in sorted(GOLDEN[profile], key=int)[:6]]


@pytest.mark.parametrize("profile,seed", SLICE)
def test_the_orientdb_provider_reproduces_the_legacy_fuzzer(profile, seed):
    assert golden_dump.digests(API, profile, int(seed), legacy=False) == GOLDEN[profile][seed]
