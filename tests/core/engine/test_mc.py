"""Metamorphic coverage (`ganda.core.engine.mc`) and the continuous-mode LLVM profile
reader (`ganda.core.engine.llvm_coverage`), over hand-made coverage sets and a hand-made
`.profraw` -- no server, no instrumented build."""
import hashlib
import itertools
import struct
import zlib
from types import SimpleNamespace

from ganda.core.engine import llvm_coverage as llvm
from ganda.core.engine import mc


def _case(generator="ir_select", spellings=None):
    return SimpleNamespace(generator_name=generator, spellings=spellings or {})


def _bits(*ids):
    return sum(1 << i for i in ids)


def test_split_is_reached_by_some_but_not_all():
    windows = {"doc": {"u": _bits(0, 1, 2)}, "graph": {"u": _bits(0, 1)}, "rel": {"u": _bits(0, 3), "v": _bits(5)}}
    union, out = mc.split(windows)
    assert union == {"u": _bits(0, 1, 2, 3), "v": _bits(5)}
    assert out == {"u": _bits(1, 2, 3), "v": _bits(5)}  # 0 is everyone's


def test_n_way_equals_the_union_of_every_pairs_symmetric_difference():
    windows = {"doc": {"u": 0b1011}, "graph": {"u": 0b0110}, "rel": {"u": 0b1110}, "rel:flat": {"u": 0b0011}}
    _, nway = mc.split(windows)
    pairwise = 0
    for a, b in itertools.combinations(windows, 2):
        pairwise |= windows[a]["u"] ^ windows[b]["u"]
    assert nway["u"] == pairwise


def test_a_case_counts_only_its_compared_variants():
    m = mc.MetamorphicCoverage()
    # `rel` and `rel:flat` are one data model, never compared; `doc` partners both
    size = m.add_case(_case(), {"doc": {"u": _bits(0)}, "rel": {"u": _bits(0, 1)}, "rel:flat": {"u": _bits(0, 2)}})
    assert size == 2
    assert m.pairs["doc~rel"]["pairs"] == 2 and m.pairs["doc~rel"]["mc"] == {"u": _bits(1, 2)}
    assert m.add_case(_case(), {"doc": {"u": _bits(0)}}) is None  # nothing to compare


def test_background_is_taken_out_and_reported():
    m = mc.MetamorphicCoverage()
    m.add_background({"u": _bits(7)})
    m.add_case(_case("ir_path"), {"doc": {"u": _bits(0, 7)}, "graph": {"u": _bits(0)}})
    m.add_case(_case("ir_path"), {"doc": {"u": _bits(1)}, "graph": {"u": _bits(1)}})
    s = m.summary(unit_name=lambda u: "mod", denominator=100)
    assert s["mc"] == 0 and s["mc_raw"] == 1 and s["background_in_mc"] == 1
    assert s["zero_mc_cases"] == 2 and s["covered"] == 2 and s["elements"] == 100
    assert s["generators"]["ir_path"] == {"mc": 0, "covered": 2, "cases": 2, "zero_mc_cases": 2}
    assert s["modules"] == {"mod": {"mc": 0, "covered": 2}}


def test_idle_windows_or_every_quiet_interval():
    answers = iter([{"u": 1}, {"u": 2}, {"v": 4}])
    assert mc.idle_windows(lambda: next(answers), seconds=0.02, every=0.01) == {"u": 2, "v": 4}


# --- the profile reader -----------------------------------------------------------


def _uleb(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _profraw(functions, counters):
    """A version-10 continuous-mode raw profile: `functions` [(name, first counter,
    counters)], the u64 `counters`; data records of 64 bytes, counters on a page."""
    fields = llvm.HEADER_FIELDS[10]
    data_size, n = 64, len(functions)
    names = b"\x01".join(f[0].encode() for f in functions)
    packed = zlib.compress(names)
    names_section = _uleb(len(names)) + _uleb(len(packed)) + packed
    names_section += b"\0" * (-len(names_section) % 8)
    head = len(fields) * 8
    pad_before = -(head + n * data_size) % llvm.PAGE
    counters_delta = head + n * data_size + pad_before - head  # counters' address minus the data's
    h = {k: 0 for k in fields}
    h.update(Magic=llvm.MAGIC, Version=10, NumData=n, PaddingBytesBeforeCounters=pad_before, NumCounters=len(counters), NamesSize=len(names_section), CountersDelta=counters_delta)
    out = bytearray(struct.pack(f"<{len(fields)}Q", *(h[k] for k in fields)))
    for i, (name, first, k) in enumerate(functions):
        ref = int.from_bytes(hashlib.md5(name.encode()).digest()[:8], "little")
        # CounterPtr: the function's counters' address minus the record's own
        rel = counters_delta - i * data_size + first * 8
        out += struct.pack("<QQq", ref, 0, rel) + b"\0" * (data_size - 24)
    out += b"\0" * pad_before
    out += struct.pack(f"<{len(counters)}Q", *counters)
    out += names_section
    return bytes(out)


def test_a_profile_maps_counters_to_functions_by_name(tmp_path):
    path = tmp_path / "1.profraw"
    fns = [("_ZN8arangodb3aql5Query4execEv", 0, 3), ("_ZN14surrealdb_core3sql5parseE", 3, 2)]
    path.write_bytes(_profraw(fns, [1, 0, 0, 5, 0]))
    p = llvm.Profile(str(path))
    assert p.data_size == 64 and p.counters_offset % llvm.PAGE == 0 and p.num_counters == 5
    assert p.functions() == ([0, 3], [fns[0][0], fns[1][0]])
    assert struct.unpack("<5Q", p.counters()) == (1, 0, 0, 5, 0)
    assert llvm.module_of(fns[0][0]) == "arangodb::aql" and llvm.module_of(fns[1][0]) == "surrealdb_core::sql"
    p.close()


def test_changed_names_each_moved_counter_once():
    before = struct.pack("<4Q", 0, 1, 255, 7)
    after = struct.pack("<4Q", 1, 1, 256, 7)
    assert llvm.changed(before, after, 8) == [0, 2]


def test_the_collector_windows_a_live_profile(tmp_path):
    fns = [("_ZN1a1fEv", 0, 2), ("_ZN1b1gEv", 2, 2)]
    path = tmp_path / "9.profraw"
    path.write_bytes(_profraw(fns, [1, 0, 0, 0]))  # boot ran a::f's entry
    c = llvm.LlvmCollector(str(tmp_path), "variant", wait=1)
    raw = bytearray(path.read_bytes())
    off = c.profile.counters_offset

    def bump(i):
        v = struct.unpack_from("<Q", raw, off + 8 * i)[0]
        struct.pack_into("<Q", raw, off + 8 * i, v + 1)
        path.write_bytes(raw)

    c.calibrated = True  # no idle windows
    case = _case()
    bump(1)
    c.after_variant(case, "doc")
    bump(1)
    bump(2)
    c.after_variant(case, "graph")
    c.after_case(case)
    s = c.summary()
    assert s["counters"] == 4 and s["counters_covered"] == 3
    assert s["mc"]["mc"] == 1 and s["mc"]["covered"] == 2  # b::g's entry, only graph's
    assert s["mc"]["modules"] == {"b::g": {"mc": 1, "covered": 1}, "a::f": {"mc": 0, "covered": 1}}


def test_a_scope_keeps_only_its_functions(tmp_path):
    fns = [("_ZN1a1fEv", 0, 2), ("_ZN2v81gEv", 2, 3)]
    path = tmp_path / "1.profraw"
    path.write_bytes(_profraw(fns, [1, 0, 4, 0, 0]))
    c = llvm.LlvmCollector(str(tmp_path), "seed", wait=1, scope=lambda name: llvm.module_of(name, 1) == "a")
    c.after_seed(1)
    s = c.summary()
    assert s["counters"] == 2 and s["counters_covered"] == 1 and s["scope"] == "1 functions in scope"


def test_the_window_log_keeps_every_variant_and_its_repeat(tmp_path):
    from ganda.core.engine import window_log

    fns = [("_ZN1a1fEv", 0, 2), ("_ZN2v81gEv", 2, 2)]
    path = tmp_path / "prof" / "c1-1.profraw"
    path.parent.mkdir()
    path.write_bytes(_profraw(fns, [0, 0, 0, 0]))
    log = tmp_path / "w.jsonl.gz"
    c = llvm.LlvmCollector(str(path.parent), "variant", wait=1, scope=lambda n: llvm.module_of(n, 1) == "a", idle_seconds=0, log_path=str(log))
    raw = bytearray(path.read_bytes())
    off = c.profile.counters_offset

    def bump(i, by=1):
        v = struct.unpack_from("<Q", raw, off + 8 * i)[0]
        struct.pack_into("<Q", raw, off + 8 * i, v + by)
        path.write_bytes(raw)

    case = _case()
    bump(0, 3)
    bump(2)  # out of scope: logged, not measured
    c.after_variant(case, "doc")
    bump(0)
    c.after_variant(case, "doc#again")
    bump(1)
    c.after_variant(case, "graph")
    c.after_case(case)
    c.after_seed(7)
    c.write(str(tmp_path), "t")
    recs = list(window_log.read(str(log)))
    assert [r["t"] for r in recs] == ["meta", "unit", "unit", "case", "seed"]
    rec = recs[3]
    assert rec["pairs"] == [["doc", "graph"]]
    assert rec["w"] == {"doc": {"0": [[0, 3]], "1": [[0, 1]]}, "doc#again": {"0": [[0, 1]]}, "graph": {"0": [[1, 1]]}}
    s = c.summary()["mc"]
    assert s["mc"] == 2 and s["repeats"] == 1 and s["repeat_noise"] == 0  # counter 0 moved both times
