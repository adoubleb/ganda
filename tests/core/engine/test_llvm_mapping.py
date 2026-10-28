"""The LLVM coverage-mapping decoder (`ganda.core.engine.llvm_mapping`) over hand-encoded
mapping bytes, hand-made ELF / Mach-O containers and a hand-made raw profile: no
compiler. The expected lines follow llvm-cov's `SegmentBuilder` / `LineCoverageStats`
step by step (the derivations are in the comments); the decoder itself is checked against
llvm-cov on real builds by `scripts/coverage/check_llvm_mapping.py`."""
import hashlib
import struct
import zlib

import pytest

from ganda.core.engine import llvm_coverage as llvm
from ganda.core.engine import llvm_mapping as lm

CWD = "/src/proj"


def uleb(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def ref(text):
    return int.from_bytes(hashlib.md5(text.encode()).digest()[:8], "little")


# counters as encoded: zero, counter i, subtract expression e, add expression e
Z = 0


def C(i):
    return (i << 2) | 1


def SUB(e):
    return (e << 2) | 2


def ADD(e):
    return (e << 2) | 3


def code(counter, ls, cs, le, ce, gap=False):
    return ("code", counter, ls, cs, le, ce, gap)


def filenames_blob(names, compress=False):
    raw = b"".join(uleb(len(n)) + n.encode() for n in names)
    if compress:
        packed = zlib.compress(raw)
        return uleb(len(names)) + uleb(len(raw)) + uleb(len(packed)) + packed
    return uleb(len(names)) + uleb(len(raw)) + uleb(0) + raw


def covmap(blobs, version=6):
    out = bytearray()
    for blob in blobs:
        out += struct.pack("<IIII", 0, len(blob), 0, version - 1) + blob
        out += b"\0" * (-len(out) % 8)
    return bytes(out)


def mapping(files, exprs, regions_per_file):
    """The encoded mapping of one function: `files` (TU filename indices), `exprs`
    [(lhs, rhs)], per virtual file a list of regions: ("code", counter, ls, cs, le, ce,
    gap), ("skipped", ls, cs, le, ce), ("expansion", file, ls, cs, le, ce), ("branch", c,
    c2, ls, cs, le, ce), ("decision", bitmap, conds, ...), ("mcdc_branch", c, c2, id, t,
    f, ...)."""
    out = bytearray(uleb(len(files)))
    for f in files:
        out += uleb(f)
    out += uleb(len(exprs))
    for a, b in exprs:
        out += uleb(a) + uleb(b)
    for regions in regions_per_file:
        out += uleb(len(regions))
        prev = 0
        for r in regions:
            kind = r[0]
            if kind == "code":
                _, counter, ls, cs, le, ce, gap = r
                head = uleb(counter)
            elif kind == "skipped":
                _, ls, cs, le, ce = r
                head, gap = uleb(lm.SKIPPED << 3), False
            elif kind == "expansion":
                _, f, ls, cs, le, ce = r
                head, gap = uleb((f << 3) | 4), False
            elif kind == "branch":
                _, c1, c2, ls, cs, le, ce = r
                head, gap = uleb(lm.BRANCH << 3) + uleb(c1) + uleb(c2), False
            elif kind == "decision":
                _, bitmap, conds, ls, cs, le, ce = r
                head, gap = uleb(lm.MCDC_DECISION << 3) + uleb(bitmap) + uleb(conds), False
            else:
                _, c1, c2, i, t, f, ls, cs, le, ce = r
                head, gap = uleb(lm.MCDC_BRANCH << 3) + uleb(c1) + uleb(c2) + uleb(i) + uleb(t) + uleb(f), False
            assert ls >= prev, "regions of a file are sorted by their start line"
            out += head + uleb(ls - prev) + uleb(cs) + uleb(le - ls) + uleb(ce | (1 << 31 if gap else 0))
            prev = ls
    return bytes(out)


def covfun(records, blob):
    """records [(name, hash, mapping bytes)] of one TU whose filenames region is `blob`."""
    out = bytearray()
    for name, fhash, data in records:
        out += struct.pack("<QIQQ", ref(name), len(data), fhash, lm.md5_ref(blob)) + data
        out += b"\0" * (-len(out) % 8)
    return bytes(out)


def build(records, names=(CWD, "a.c", "m.h"), units=None, version=6, fix=None):
    blob = filenames_blob(list(names))
    cm = lm.CoverageMapping(covmap([blob], version), covfun(strip(records), blob), {ref(r[0]): r[0] for r in records})
    if units is None:
        units = [(ref(n), h, counts) for n, h, counts in ((r[0], r[1], r[3]) for r in records if len(r) > 3)]
    return lm.Build(cm, units, fix)


def strip(records):
    return [r[:3] for r in records]


# --- primitives -----------------------------------------------------------------------


def test_uleb_and_paths():
    for n in (0, 1, 127, 128, 300, 1 << 31, (1 << 64) - 1):
        assert lm._uleb(uleb(n) + b"\xff", 0) == (n, len(uleb(n)))
    assert lm._remove_dots("/a/b/../c/./d//e/") == "/a/c/d/e"
    assert lm._remove_dots("/../x") == "/x" and lm._remove_dots("../a/../../b") == "../../b"
    assert lm._append("/src/", "a.c") == "/src/a.c" and lm._append("/src", "a.c") == "/src/a.c" and lm._append("", "a.c") == "a.c"


def test_filenames_are_joined_to_the_compilation_directory_from_version6():
    names = [CWD, "a.c", "../lib/./b.h", "/usr/include/c.h"]
    for compress in (False, True):
        blob = filenames_blob(names, compress)
        cm = lm.CoverageMapping(covmap([blob]), b"")
        assert cm.filenames == [CWD, "/src/proj/a.c", "/src/lib/b.h", "/usr/include/c.h"]
        cm = lm.CoverageMapping(covmap([blob], version=5), b"")  # Version5: as stored
        assert cm.filenames == names
    cm = lm.CoverageMapping(covmap([filenames_blob(names)]), b"", compilation_dir="/elsewhere")
    assert cm.filenames[1] == "/elsewhere/a.c"
    with pytest.raises(lm.MappingError, match="version"):
        lm.CoverageMapping(covmap([filenames_blob(names)], version=9), b"")


def test_two_translation_units_share_file_ids_and_a_record_names_its_unit():
    b1, b2 = filenames_blob([CWD, "a.c", "h.h"]), filenames_blob([CWD, "b.c", "h.h"])
    data = mapping([1], [], [[code(C(0), 1, 1, 2, 1)]])
    cm = lm.CoverageMapping(covmap([b1, b2]), covfun([("f", 1, data)], b2) + covfun([("g", 2, mapping([2], [], [[code(C(0), 3, 1, 4, 1)]]))], b1))
    assert cm.filenames == [CWD, "/src/proj/a.c", "/src/proj/h.h", "/src/proj/b.c"]
    assert cm.decode(ref("f")).files == (3,) and cm.decode(ref("g")).files == (2,)


# --- decoding -------------------------------------------------------------------------


def test_every_region_kind_and_expression_decodes():
    # e0 = c0 - c1, e1 = e0 + c2 (the kind of an expression is set by the counter that refers to it)
    data = mapping([1, 2], [(C(0), C(1)), (SUB(0), C(2))], [
        [
            code(C(0), 1, 10, 9, 2),
            code(ADD(1), 2, 5, 3, 6),
            ("branch", C(1), SUB(0), 2, 9, 2, 14),
            code(SUB(0), 3, 6, 4, 3, gap=True),
            ("decision", 0, 2, 5, 1, 5, 20),
            ("mcdc_branch", C(1), Z, 1, 2, 0, 5, 1, 5, 9),
            ("expansion", 1, 6, 3, 6, 9),
            ("skipped", 7, 0, 7, 0),
        ],
        [code(C(2), 1, 20, 1, 30)],
    ])
    blob = filenames_blob([CWD, "a.c", "m.h"])
    cm = lm.CoverageMapping(covmap([blob], 7), covfun([("f", 9, data)], blob))
    assert cm.version == 7
    fn = cm.decode(ref("f"))
    assert list(fn.expr_kind) == [lm.SUBTRACT, lm.ADD]
    kinds = [r[0] for r in fn.regions]
    assert kinds == [lm.CODE, lm.CODE, lm.BRANCH, lm.GAP, lm.MCDC_DECISION, lm.MCDC_BRANCH, lm.EXPANSION, lm.SKIPPED, lm.CODE]
    assert fn.regions[3][5:] == [3, 6, 4, 3]  # the gap bit is not part of the column
    assert fn.regions[7][5:] == [7, 1, 7, lm.UINT_MAX]  # (0, 0): the whole line
    assert fn.regions[6][1] == C(2)  # an expansion counts as the first region it expands
    assert fn.regions[2][1:3] == [C(1), SUB(0)] and fn.regions[5][1:3] == [C(1), Z]
    assert fn.regions[8][3] == 1 and fn.files == (1, 2)
    assert lm.evaluate(fn, ADD(1), [10, 4, 3], {}) == 9 and lm.evaluate(fn, SUB(0), {0: 10, 1: 4}, {}) == 6


def test_nested_expansions_propagate_their_counts():
    # file 0 expands file 1 whose first region is an expansion of file 2 (listed later)
    data = mapping([1, 2, 2], [], [
        [code(C(0), 1, 1, 9, 1), ("expansion", 1, 2, 1, 2, 5)],
        [("expansion", 2, 1, 1, 1, 9), code(C(1), 2, 1, 2, 4)],
        [code(C(2), 3, 1, 3, 9)],
    ])
    blob = filenames_blob([CWD, "a.c", "m.h"])
    fn = lm.CoverageMapping(covmap([blob]), covfun([("f", 1, data)], blob)).decode(ref("f"))
    assert fn.regions[1][1] == C(2) and fn.regions[2][1] == C(2)


def test_a_dummy_record_gives_way_to_the_real_one():
    blob = filenames_blob([CWD, "a.c"])
    dummy = mapping([0], [], [[code(Z, 1, 1, 3, 2)]])
    real = mapping([0], [], [[code(C(0), 1, 1, 3, 2), code(C(1), 2, 1, 2, 5)]])
    cm = lm.CoverageMapping(covmap([blob]), covfun([("f", 0, dummy), ("f", 77, real), ("f", 0, dummy)], blob))
    assert cm.records[ref("f")][0] == 77
    cm = lm.CoverageMapping(covmap([blob]), covfun([("f", 77, real), ("f", 78, real)], blob))
    assert cm.records[ref("f")][0] == 77  # the first real one wins


# --- the file view --------------------------------------------------------------------

BODY = [
    code(C(0), 1, 10, 9, 2),  # the function body, count c0
    code(C(1), 2, 5, 3, 6),  # a then-branch, c1
    code(SUB(0), 3, 6, 4, 3, gap=True),  # c0 - c1 from the branch's end to the else
    code(SUB(0), 4, 3, 5, 8),  # the else, c0 - c1
    ("expansion", 1, 6, 3, 6, 9),  # a macro used at 6:3, its body in m.h
    ("skipped", 7, 1, 8, 1),  # `#if 0`
]


def test_segments_and_lines_follow_llvm_cov():
    # segments: 1:10 c0 entry, 2:5 c1 entry, 3:6 gap c0-c1, 4:3 c0-c1 entry, 5:8 c0,
    # 6:3 c2 entry (the expansion), 6:9 c0, 7:1 skipped entry, 8:1 c0, 9:2 none.
    # line 3 is wrapped by the then-branch; 7 starts with the skipped region; 8 is
    # wrapped by the skipped region (no count) with no region entry: unmapped.
    b = build([("f", 5, mapping([1, 2], [(C(0), C(1))], [BODY, [code(C(2), 1, 20, 1, 30)]]), [10, 4, 3])])
    counts = b.line_counts(b.cumulative_values())
    assert counts["/src/proj/a.c"] == {1: 10, 2: 10, 3: 4, 4: 6, 5: 6, 6: 10, 9: 10}
    assert counts["/src/proj/m.h"] == {1: 3}
    m = lm.LlvmLineMapper.__new__(lm.LlvmLineMapper)
    m.build, m._names = b, b.mapping.filenames
    assert m.executable_lines() == {"/src/proj/a.c": {1, 2, 3, 4, 5, 6, 9}, "/src/proj/m.h": {1}}
    # a window where only the then-branch ran again: c0 = c1 = 1, so c0 - c1 = 0 and
    # line 5 (wrapped by the else, whose count is c0 - c1) is not executed
    assert m.unit_lines("0", None, [[0, 1], [1, 1]]) == {("/src/proj/a.c", n) for n in (1, 2, 3, 6, 9)}
    # c0 - c1 alone (an inconsistent window) counts too: the else lines
    assert ("/src/proj/a.c", 4) in m.unit_lines("0", None, [[0, 1]])
    assert m.unit_lines("0", None, [[2, 4]]) == {("/src/proj/a.c", 6), ("/src/proj/m.h", 1)}
    assert m.unit_lines("0", None, []) == frozenset()


def test_a_function_inside_another_owns_its_lines():
    # a lambda (its own function) inside main: main's region wraps it, but the lambda's
    # regions decide its lines. Line 3 starts the lambda (main wraps it: main's count),
    # lines 4-5 are the lambda's.
    outer = ("main", 1, mapping([1], [], [[code(C(0), 1, 1, 10, 1)]]), [1])
    inner = ("lambda", 2, mapping([1], [], [[code(C(0), 3, 5, 5, 6)]]), [0])
    b = build([outer, inner])
    assert b.line_counts(b.cumulative_values())["/src/proj/a.c"] == {1: 1, 2: 1, 3: 1, 4: 0, 5: 0, 6: 1, 7: 1, 8: 1, 9: 1, 10: 1}
    main_lines = {ln for _, ln in b.function_lines(0, {0: 1})}
    assert main_lines == {1, 2, 3, 6, 7, 8, 9, 10}
    assert {ln for _, ln in b.function_lines(1, {0: 2})} == {3, 4, 5}


def test_instantiations_merge_and_a_window_is_the_union_of_its_functions():
    body = mapping([2], [(C(0), C(1))], [[code(C(0), 1, 1, 4, 2), code(C(1), 2, 3, 2, 9), code(SUB(0), 3, 3, 3, 9)]])
    b = build([("T<int>", 3, body, [2, 2]), ("T<long>", 3, body, [1, 0])])
    # one merged region per area: counts add up
    assert b.line_counts(b.cumulative_values())["/src/proj/m.h"] == {1: 3, 2: 3, 3: 3, 4: 3}
    assert b.function_lines(0, {0: 1, 1: 1}) | b.function_lines(1, {}) == b.function_lines(0, {0: 1, 1: 1})
    window = {0: {0: 0}, 1: {0: 1}}  # only T<long> ran, its else
    exact = b.line_counts(b.window_values(window))["/src/proj/m.h"]
    union = b.function_lines(0, {}) | b.function_lines(1, {0: 1})
    assert {ln for ln, c in exact.items() if c} == {ln for _, ln in union} == {1, 2, 3, 4}
    # line 2: c1 of both is 0 there but the body wraps it (largest of wrapped and entries)


def test_loading_follows_the_profile():
    body = mapping([1], [], [[code(C(0), 1, 1, 2, 1)]])
    units = [(ref("mismatch"), 99, [5]), (ref("f"), 1, [3, 0])]
    b = build([("mismatch", 1, body), ("absent", 1, body), ("f", 1, mapping([1], [], [[code(C(3), 4, 1, 5, 1)]]))], units=units)
    # the hash mismatch is dropped, the absent function counts zero, f refers to a
    # counter its profile record does not have (dropped)
    assert b.stats["hash_mismatch"] == 1 and b.stats["counter_out_of_range"] == 1 and b.stats["functions"] == 1
    assert b.line_counts(b.cumulative_values()) == {"/src/proj/a.c": {1: 0, 2: 0}}  # 2: wrapped
    assert b.unit_function == [None, None]
    # a single-zero-region record of a function that ran is dropped
    b = build([("g", 0, mapping([1], [], [[code(Z, 1, 1, 2, 1)]]))], units=[(ref("g"), 0, [4])])
    assert b.stats["dummy_with_counts"] == 1 and b.functions == []
    # the same local function seen twice (same files, same name less its `<file>;`)
    one = ("/src/proj/a.c;h", 1, body)
    two = ("/src/proj/a.c:h", 2, body)
    b = build([one, two], units=[])
    assert b.stats["duplicate_provenance"] == 1


def test_a_line_whose_mapping_depends_on_counts_is_settled_by_the_cumulative_profile():
    # A (1:1-9:1) holds B (2:1-6:1) holds C (3:1-4:5); a skipped region at 6:5-6:9. At
    # 6:1 A resumes after B: llvm-cov drops that segment when A's count equals B's, and
    # line 6 then starts with the skipped region (unmapped); otherwise it is mapped.
    regs = [code(C(0), 1, 1, 9, 1), code(C(1), 2, 1, 6, 1), code(C(2), 3, 1, 4, 5), ("skipped", 6, 5, 6, 9)]
    for counts, mapped in (([2, 1, 1], True), ([2, 2, 1], False)):
        b = build([("f", 1, mapping([1], [], [regs]), counts)], fix=False)
        assert b.stats["ambiguous_lines"] == 1
        assert (6 in b.executable[1]) is mapped
        assert (6 in b.line_counts(b.cumulative_values())["/src/proj/a.c"]) is mapped
    # LLVM 18's rule changes nothing here (no counted region starts on line 6)
    assert build([("f", 1, mapping([1], [], [regs]), [2, 2, 1])], fix=True).stats["ambiguous_lines"] == 1


def test_llvm18_maps_a_line_with_a_counted_region_after_a_skipped_one():
    regs = [code(C(0), 1, 1, 5, 1), ("skipped", 2, 1, 2, 6), code(C(1), 2, 8, 2, 12)]
    old = build([("f", 1, mapping([1], [], [regs]), [1, 1])], fix=False)
    new = build([("f", 1, mapping([1], [], [regs]), [1, 1])], fix=True)
    assert 2 not in old.line_counts(old.cumulative_values())["/src/proj/a.c"]
    assert new.line_counts(new.cumulative_values())["/src/proj/a.c"][2] == 1


def test_zero_length_regions_and_the_report_replica():
    # a zero-length region inside another takes the enclosing count (2:4, an entry); the
    # last region, zero-length, is a skipped segment: line 3 starts with it, unmapped
    regs = [code(C(0), 1, 1, 3, 2), code(C(1), 2, 4, 2, 4), code(C(0), 3, 2, 3, 2)]
    b = build([("f", 1, mapping([1], [], [regs]), [3, 0])])
    assert b.line_counts(b.cumulative_values())["/src/proj/a.c"] == {1: 3, 2: 3}
    assert b.report_lines(b.cumulative_values()) == {"/src/proj/a.c": [2, 2]}


# --- containers and the raw profile ---------------------------------------------------


def _elf(sections, compress=()):
    """A minimal ELF64 LE with the given {name: bytes} sections (SHF_COMPRESSED zlib for
    names in `compress`)."""
    names = [""] + list(sections) + [".shstrtab"]
    strtab = b"\0"
    offs = {}
    for n in names[1:]:
        offs[n] = len(strtab)
        strtab += n.encode() + b"\0"
    body = bytearray(b"\0" * 64)
    placed = []
    for n in names[1:-1]:
        data = sections[n]
        flags = 0
        if n in compress:
            data = struct.pack("<IIQQ", 1, 0, len(data), 8) + zlib.compress(data)
            flags = 0x800
        body += b"\0" * (-len(body) % 8)
        placed.append((n, 1, flags, len(body), len(data)))
        body += data
    placed.append((".shstrtab", 3, 0, len(body), len(strtab)))
    body += strtab
    body += b"\0" * (-len(body) % 8)
    shoff = len(body)
    body += b"\0" * 64  # the null section
    for n, typ, flags, off, size in placed:
        body += struct.pack("<IIQQQQIIQQ", offs[n], typ, flags, 0, off, size, 0, 0, 8, 0)
    head = b"\x7fELF" + bytes([2, 1, 1]) + b"\0" * 9 + struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64, len(names), len(names) - 1)
    body[:64] = head
    return bytes(body)


def _macho(sections):
    nsects = len(sections)
    cmdsize = 72 + 80 * nsects
    data_off = 32 + cmdsize
    out = bytearray(struct.pack("<IiiIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, cmdsize, 0) + b"\0" * 4)
    out += struct.pack("<II16sQQQQiiII", 0x19, cmdsize, b"__LLVM_COV", 0, 0, data_off, 0, 0, 0, nsects, 0)
    payload = bytearray()
    for name, data in sections.items():
        out += struct.pack("<16s16sQQIIIIIIII", name.encode(), b"__LLVM_COV", 0, len(data), data_off + len(payload), 3, 0, 0, 0, 0, 0, 0)
        payload += data + b"\0" * (-len(data) % 8)
    return bytes(out + payload)


def _profraw_v8(functions, counters):
    """A version-8 raw profile (LLVM 14-17): 11 header fields, 48-byte data records."""
    fields = llvm.HEADER_FIELDS[8]
    data_size, n = 48, len(functions)
    names = b"\x01".join(f[0].encode() for f in functions)
    names_section = uleb(len(names)) + uleb(0) + names
    names_section += b"\0" * (-len(names_section) % 8)
    head = len(fields) * 8
    counters_delta = n * data_size
    h = dict.fromkeys(fields, 0)
    h.update(Magic=llvm.MAGIC, Version=8, NumData=n, NumCounters=len(counters), NamesSize=len(names_section), CountersDelta=counters_delta)
    out = bytearray(struct.pack(f"<{len(fields)}Q", *(h[k] for k in fields)))
    for i, (name, fhash, first, k) in enumerate(functions):
        out += struct.pack("<QQqQQIHH", ref(name), fhash, counters_delta - i * data_size + first * 8, 0, 0, k, 0, 0)
    assert len(out) == head + n * data_size
    out += struct.pack(f"<{len(counters)}Q", *counters)
    out += names_section
    return bytes(out)


def _sections():
    blob = filenames_blob([CWD, "a.c", "m.h"])
    body = mapping([1, 2], [(C(0), C(1))], [BODY, [code(C(2), 1, 20, 1, 30)]])
    other = mapping([1], [], [[code(C(0), 20, 1, 22, 2)]])
    fun = covfun([("other", 4, other), ("f", 5, body)], blob)
    return {"__llvm_covmap": covmap([blob]), "__llvm_covfun": fun}


def test_containers_yield_the_coverage_sections(tmp_path):
    secs = _sections()
    for i, image in enumerate((_elf(secs), _elf(secs, compress={"__llvm_covfun"}), _macho(secs))):
        p = tmp_path / f"b{i}"
        p.write_bytes(image)
        got = lm.binary_sections(str(p))
        assert got["__llvm_covmap"] == [secs["__llvm_covmap"]] and got["__llvm_covfun"] == [secs["__llvm_covfun"]]
    p = tmp_path / "zstd"
    image = bytearray(_elf(secs, compress={"__llvm_covfun"}))
    image[image.index(struct.pack("<IIQ", 1, 0, len(secs["__llvm_covfun"])))] = 2
    p.write_bytes(bytes(image))
    with pytest.raises(lm.MappingError, match="zstd"):
        lm.binary_sections(str(p))
    p.write_bytes(b"not a binary")
    with pytest.raises(lm.MappingError):
        lm.binary_sections(str(p))


def test_the_line_mapper_reads_a_binary_and_a_raw_profile(tmp_path):
    binary = tmp_path / "prog"
    binary.write_bytes(_elf(_sections()))
    prof = tmp_path / "p.profraw"
    # units sorted by first counter: "f" (counters 0-2) is unit 0, "other" unit 1
    prof.write_bytes(_profraw_v8([("other", 4, 3, 1), ("f", 5, 0, 3)], [10, 4, 3, 0]))
    p = llvm.Profile(str(prof))
    assert p.data_size == 48 and p.records() == [(0, ref("f"), 5, 3), (3, ref("other"), 4, 1)]
    assert p.functions() == ([0, 3], ["f", "other"])
    p.close()
    m = lm.LlvmLineMapper(str(binary), str(prof))
    assert m.executable_lines() == {"/src/proj/a.c": {1, 2, 3, 4, 5, 6, 9, 20, 21, 22}, "/src/proj/m.h": {1}}
    assert m.covered_lines()["/src/proj/a.c"] == {1: 10, 2: 10, 3: 4, 4: 6, 5: 6, 6: 10, 9: 10, 20: 0, 21: 0, 22: 0}
    assert m.unit_lines("1", "other", [[0, 1]]) == {("/src/proj/a.c", n) for n in (20, 21, 22)}
    assert m.unit_lines("0", "f", [[2, 1]]) == {("/src/proj/a.c", 6), ("/src/proj/m.h", 1)}
    assert m.window_lines({"0": [[2, 1]], "1": [[0, 1]]}) == m.unit_lines("0", "f", [[2, 1]]) | m.unit_lines("1", "other", [[0, 1]])
