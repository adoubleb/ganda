"""Source lines of an LLVM source-based coverage build, decoded in Python with `llvm-cov`'s
own semantics: the `LineMapper` (`ganda.core.engine.mc_report`) of a build whose raw
coverage is counter increments (`ganda.core.engine.llvm_coverage`).

The build's coverage mapping (`__llvm_covmap`: one header and filename list per
translation unit; `__llvm_covfun`: one record per function) is read from the binary
itself, ELF64 or Mach-O 64, formats Version4..Version7. What follows replicates, step by
step, what `llvm-cov` (LLVM's `CoverageMappingReader.cpp` and `CoverageMapping.cpp`)
does with it:

- reading: records are keyed by their NameRef (the first one wins; a dummy record, hash 0
  and a single zero region, gives way to a real one); an expansion region's count is the
  count of the first region of the file it expands (propagated through nested
  expansions); filenames of Version6+ are relative to the compilation directory stored
  as filename 0 (joined and `..`-resolved as `llvm-cov` does);
- loading, against the profile: a function whose name is in the profile with another
  hash is dropped (a hash mismatch); one absent from it counts zero everywhere; a
  function with a counter beyond its profile record is dropped; a single-zero-region
  record of a function the profile saw run is dropped; two records with the same files
  and the same name (less its `<file>;` prefix) are one;
- the file view (`llvm-cov show`, `export`, the lcov `DA:` lines): every region of every
  function in the file (macro bodies at the macro's definition, expansion regions at the
  use), sorted, regions of the same area and kind merged (instantiations add up), cut
  into segments (`SegmentBuilder`), then line by line (`LineCoverageStats`): a line is
  mapped when a region starts on it with a count or a counted segment wraps it (and it
  does not start with a skipped region), and executed when its count (the largest of the
  wrapped count and the counts of the regions that start on it) is not zero.

A window's executed lines are those the file view executes when every counter counts its
increments in the window (absent counters zero). A line's count is the largest of a few
regions' counts (the wrapped one, the ones starting on it), and a merged region's count
is the sum of its members' (instantiations, a macro used twice), so a line is executed in
a window exactly when one of its contributing regions counts in it. The contributing
regions of every line are found once per file, with every function of the build present
(a lambda's lines are the lambda's even though its parent's body wraps them), and kept
per function: a window's lines are the union of its units' lines
(`LlvmLineMapper.unit_lines`), each exact in the file view of the whole build. Two
residuals of the decomposition are counted, not hidden (`Build.stats`): a line whose
mapped-ness depends on counts (`ambiguous_lines`: a segment llvm-cov merges away when two
counts are equal, in front of a skipped region on the same line; settled by the
cumulative profile; none in the builds checked) and members of a merged region whose
counts cancel (a negative count, which only torn counter reads give: the union counts
the line, every instantiation being a function of its own).

`Build.report_lines` replicates `llvm-cov report`'s line totals, which are not the file
view's (they sum each function's own main-file lines, instantiations merged by maximum);
`scripts/coverage/check_llvm_mapping.py` checks all of it against llvm-cov on a build.
Not handled: covmap formats before Version4 (LLVM < 11), zstd-compressed ELF sections
(decompress them first), several raw profiles in one file (an instrumented shared
library's), single-byte counters' OR-merging (their lines are right, counts are sums).
"""
from __future__ import annotations

import hashlib
import mmap
import os
import struct
import sys
import zlib
from array import array

from ganda.core.engine.mc_report import LineMapper

U64 = 1 << 64
UINT_MAX = 0xFFFFFFFF
# CounterMappingRegion::RegionKind
CODE, EXPANSION, SKIPPED, GAP, BRANCH, MCDC_DECISION, MCDC_BRANCH = range(7)
KIND_NAMES = ("code", "expansion", "skipped", "gap", "branch", "mcdc_decision", "mcdc_branch")
# CounterExpression::ExprKind
SUBTRACT, ADD = 0, 1
# the covmap header's version field (CovMapVersion; Version4 is stored as 3)
VERSIONS = {3: 4, 4: 5, 5: 6, 6: 7}
SECTIONS = ("__llvm_covmap", "__llvm_covfun", "__llvm_prf_names")


class MappingError(ValueError):
    pass


def _uleb(buf, pos: int) -> "tuple[int, int]":
    b = buf[pos]
    if b < 0x80:
        return b, pos + 1
    value, shift = b & 0x7F, 7
    pos += 1
    while True:
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if b < 0x80:
            return value, pos
        shift += 7


def md5_ref(data: bytes) -> int:
    """The low 64 bits of MD5 (a NameRef, a FilenamesRef)."""
    return int.from_bytes(hashlib.md5(data).digest()[:8], "little")


# --- containers ---------------------------------------------------------------------


def _elf_sections(mm, wanted) -> dict:
    if mm[4] != 2 or mm[5] != 1:
        raise MappingError("only 64-bit little-endian ELF is read")
    shoff, = struct.unpack_from("<Q", mm, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", mm, 0x3A)
    if shoff == 0:
        raise MappingError("ELF without section headers")

    def header(i):
        return struct.unpack_from("<IIQQQQIIQQ", mm, shoff + i * shentsize)

    if shnum == 0:
        shnum = header(0)[5]
    if shstrndx == 0xFFFF:
        shstrndx = header(0)[6]
    strtab = header(shstrndx)
    strs = mm[strtab[4] : strtab[4] + strtab[5]]
    out: dict = {}
    for i in range(shnum):
        name_off, typ, flags, _addr, offset, size, _link, _info, _align, _ent = header(i)
        name = strs[name_off : strs.index(b"\0", name_off)].decode()
        if name not in wanted or typ == 8:  # SHT_NOBITS
            continue
        data = mm[offset : offset + size]
        if flags & 0x800:  # SHF_COMPRESSED: Elf64_Chdr
            ch_type, _res, ch_size, _ch_align = struct.unpack_from("<IIQQ", data, 0)
            if ch_type == 1:
                data = zlib.decompress(data[24:])
                if len(data) != ch_size:
                    raise MappingError(f"{name}: decompressed {len(data)} bytes, header says {ch_size}")
            elif ch_type == 2:
                raise MappingError(f"{name} is zstd-compressed (ELFCOMPRESS_ZSTD): decompress it first (`objcopy --decompress-debug-sections` or `llvm-objcopy --decompress-debug-sections`)")
            else:
                raise MappingError(f"{name}: unknown ELF compression type {ch_type}")
        out.setdefault(name, []).append(bytes(data))
    return out


def _macho_sections(mm, base, wanted) -> dict:
    magic, _cpu, _sub, _ftype, ncmds, _size, _flags = struct.unpack_from("<IiiIIII", mm, base)
    if magic != 0xFEEDFACF:
        raise MappingError("only 64-bit little-endian Mach-O is read")
    pos = base + 32
    out: dict = {}
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", mm, pos)
        if cmd == 0x19:  # LC_SEGMENT_64
            nsects, = struct.unpack_from("<I", mm, pos + 64)
            s = pos + 72
            for _ in range(nsects):
                sect = mm[s : s + 16].rstrip(b"\0").decode()
                _addr, size, offset, _align, _reloff, _nreloc, flags = struct.unpack_from("<QQIIIII", mm, s + 32)
                if sect in wanted and (flags & 0xFF) not in (1, 12, 18):  # zerofill kinds
                    out.setdefault(sect, []).append(bytes(mm[base + offset : base + offset + size]))
                s += 80
        pos += cmdsize
    return out


def binary_sections(path: str, wanted=SECTIONS) -> dict:
    """{section name: [contents, ...]} of the coverage sections of an ELF64 or Mach-O 64
    binary (a universal Mach-O: the slice of this machine's architecture, else the
    first 64-bit one)."""
    with open(path, "rb") as fh:
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            if mm[:4] == b"\x7fELF":
                return _elf_sections(mm, wanted)
            magic, = struct.unpack_from("<I", mm, 0)
            if magic == 0xFEEDFACF:
                return _macho_sections(mm, 0, wanted)
            if magic in (0xBEBAFECA, 0xBFBAFECA):  # FAT_MAGIC / FAT_MAGIC_64, big-endian
                wide = magic == 0xBFBAFECA
                n, = struct.unpack_from(">I", mm, 4)
                arch = {"arm64": 0x0100000C, "x86_64": 0x01000007}.get(os.uname().machine)
                slices = []
                for i in range(n):
                    if wide:
                        cpu, _sub, off, _size, _al, _r = struct.unpack_from(">iiQQII", mm, 8 + 32 * i)
                    else:
                        cpu, _sub, off, _size, _al = struct.unpack_from(">iiIII", mm, 8 + 20 * i)
                    if cpu & 0x01000000:
                        slices.append((cpu != arch, off))
                if not slices:
                    raise MappingError(f"{path}: no 64-bit slice")
                return _macho_sections(mm, min(slices)[1], wanted)
            raise MappingError(f"{path}: neither ELF nor Mach-O (magic 0x{magic:08x})")
        finally:
            mm.close()


def _join_aligned(chunks: list) -> bytes:
    """Sections concatenated as llvm-cov concatenates the covfun sections: each padded to 8."""
    out = bytearray()
    for c in chunks:
        out += c
        out += b"\0" * (-len(out) % 8)
    return bytes(out)


def names_section(raw: bytes) -> dict:
    """{NameRef: name} of a names section (`__llvm_prf_names`; blocks of ULEB length,
    ULEB compressed length, zlib or raw text, names split by \\x01)."""
    out, pos = {}, 0
    while pos < len(raw):
        full, pos = _uleb(raw, pos)
        packed, pos = _uleb(raw, pos)
        if full == 0 and packed == 0:
            break
        if packed:
            text, pos = zlib.decompress(raw[pos : pos + packed]), pos + packed
        else:
            text, pos = raw[pos : pos + full], pos + full
        for name in text.split(b"\x01"):
            out[md5_ref(name)] = name.decode("utf-8", "replace")
        while pos < len(raw) and raw[pos] == 0:  # each block is padded to 8
            pos += 1
    return out


# --- paths, as llvm::sys::path does them (POSIX) --------------------------------------


def _remove_dots(path: str) -> str:
    """`sys::path::remove_dots(P, /*remove_dot_dot=*/true)`."""
    absolute = path.startswith("/")
    parts: list = []
    for c in path.split("/"):
        if c in ("", "."):
            continue
        if c == "..":
            if parts and parts[-1] != "..":
                parts.pop()
                continue
            if absolute:
                continue
        parts.append(c)
    return ("/" if absolute else "") + "/".join(parts)


def _append(base: str, name: str) -> str:
    """`sys::path::append(P, Filename)`."""
    if not name:
        return base
    if base.endswith("/"):
        return base + name.lstrip("/")
    if base and not name.startswith("/"):
        return base + "/" + name
    return base + name


# --- the coverage mapping -------------------------------------------------------------


class Function:
    """One function record as llvm-cov loads it: its files (an id per virtual file), its
    expressions and its regions."""

    __slots__ = ("name_ref", "hash", "name", "files", "expr_kind", "expr_lhs", "expr_rhs", "regions", "counted", "max_counter", "key")

    def __init__(self, name_ref, fhash, name, files, expr_kind, expr_lhs, expr_rhs, regions):
        self.name_ref = name_ref
        self.hash = fhash
        self.name = name
        self.files = files  # global filename id per virtual file id
        self.expr_kind = expr_kind
        self.expr_lhs = expr_lhs
        self.expr_rhs = expr_rhs
        # [kind, counter, false_counter, file_id, expanded_file_id, ls, cs, le, ce]
        self.regions = regions
        self.counted = None  # the regions of the file view: array, STRIDE ints per region
        self.max_counter = -1
        self.key = None  # (NameRef, hash) of the profile records whose counts are this function's


# a Counter is an int: 0 zero, (id << 2) | 1 a counter, (id << 2) | 2 an expression


def _counters_of(fn: Function, counter: int, out: set) -> None:
    stack = [counter]
    seen: set = set()
    while stack:
        c = stack.pop()
        tag = c & 3
        if tag == 1:
            out.add(c >> 2)
        elif tag == 2 and c not in seen:
            seen.add(c)
            e = c >> 2
            stack.append(fn.expr_lhs[e])
            stack.append(fn.expr_rhs[e])


def evaluate(fn: Function, counter: int, counts, memo: dict) -> int:
    """`CounterMappingContext::evaluate` (int64 arithmetic, read back as uint64 by the
    caller); `counts` is indexable by counter id (a list or a dict with zeros absent)."""
    tag = counter & 3
    if tag == 0:
        return 0
    if tag == 1:
        return counts.get(counter >> 2, 0) if isinstance(counts, dict) else counts[counter >> 2]
    v = memo.get(counter)
    if v is not None:
        return v
    # iterative post-order: expressions can nest deeply
    stack = [counter]
    while stack:
        c = stack[-1]
        e = c >> 2
        pending = [x for x in (fn.expr_lhs[e], fn.expr_rhs[e]) if x & 3 == 2 and x not in memo]
        if pending:
            stack.extend(pending)
            continue
        stack.pop()
        if c in memo:
            continue
        lhs, rhs = fn.expr_lhs[e], fn.expr_rhs[e]
        a = memo[lhs] if lhs & 3 == 2 else (0 if lhs & 3 == 0 else (counts.get(lhs >> 2, 0) if isinstance(counts, dict) else counts[lhs >> 2]))
        b = memo[rhs] if rhs & 3 == 2 else (0 if rhs & 3 == 0 else (counts.get(rhs >> 2, 0) if isinstance(counts, dict) else counts[rhs >> 2]))
        memo[c] = a - b if fn.expr_kind[e] == SUBTRACT else a + b
    return memo[counter]


class CoverageMapping:
    """The records of a binary's coverage mapping, read as `BinaryCoverageReader` reads
    them (NameRef dedup with the dummy rule), decoded lazily."""

    def __init__(self, covmap: bytes, covfun: bytes, names: "dict | None" = None, compilation_dir: str = ""):
        self.names = names or {}
        self.compilation_dir = compilation_dir
        self.filenames: list = []  # unique: a file id is a position in it
        self._file_ids: dict = {}
        self.tu_files: list = []  # every translation unit's filenames (as ids), concatenated
        self.file_ranges: dict = {}  # FilenamesRef -> (start, length) in tu_files, or None (a collision)
        self.version = None
        self._read_headers(covmap)
        self.covfun = covfun
        # NameRef -> [hash, mapping start, mapping end, filenames start, filenames length]
        self.records: dict = {}
        self._read_function_records(covfun)

    def _read_headers(self, buf: bytes) -> None:
        pos = 0
        while pos < len(buf):
            if pos + 16 > len(buf) or buf[pos : pos + 16] == bytes(16):
                if not any(buf[pos:]):
                    break  # trailing padding
                raise MappingError("coverage mapping header section is larger than buffer size")
            nrecords, fsize, csize, raw_version = struct.unpack_from("<IIII", buf, pos)
            if self.version is None:
                if raw_version not in VERSIONS:
                    raise MappingError(f"coverage mapping version {raw_version + 1} (raw {raw_version}) not supported: Version4..Version7 are read (a newer LLVM: check the format and extend VERSIONS)")
                self.version = VERSIONS[raw_version]
            if nrecords or csize:
                raise MappingError("Version4+ coverage headers carry no records")
            pos += 16
            region = buf[pos : pos + fsize]
            if len(region) != fsize:
                raise MappingError("filenames region is larger than buffer size")
            start = len(self.tu_files)
            for name in self._read_filenames(region):
                fid = self._file_ids.get(name)
                if fid is None:
                    fid = self._file_ids[name] = len(self.filenames)
                    self.filenames.append(name)
                self.tu_files.append(fid)
            rng = (start, len(self.tu_files) - start)
            ref = md5_ref(region)
            if ref in self.file_ranges:
                orig = self.file_ranges[ref]
                if orig is not None and self.tu_files[orig[0] : orig[0] + orig[1]] != self.tu_files[rng[0] : rng[0] + rng[1]]:
                    self.file_ranges[ref] = None  # a hash collision: invalid
            else:
                self.file_ranges[ref] = rng
            pos += fsize
            pos += -pos % 8

    def _read_filenames(self, region: bytes) -> list:
        n, pos = _uleb(region, 0)
        if n == 0:
            raise MappingError("number of filenames is zero")
        full, pos = _uleb(region, pos)
        packed, pos = _uleb(region, pos)
        if packed:
            data, pos = zlib.decompress(region[pos : pos + packed]), 0
            if len(data) != full:
                raise MappingError("filenames decompressed to another length")
        else:
            data = region
        out = []
        for _ in range(n):
            length, pos = _uleb(data, pos)
            out.append(data[pos : pos + length].decode("utf-8", "surrogateescape"))
            pos += length
        if self.version < 6:
            return out
        cwd = out[0]
        joined = [cwd]
        for name in out[1:]:
            if name.startswith("/"):
                joined.append(name)
            else:
                joined.append(_remove_dots(_append(self.compilation_dir or cwd, name)))
        return joined

    def _read_function_records(self, buf: bytes) -> None:
        pos, end = 0, len(buf)
        records = self.records
        while pos < end:
            if end - pos < 28:
                if any(buf[pos:]):
                    raise MappingError("truncated function record")
                break
            name_ref, size, fhash, fref = struct.unpack_from("<QIQQ", buf, pos)
            start = pos + 28
            stop = start + size
            if stop > end:
                raise MappingError("coverage mapping data is larger than buffer size")
            pos = stop + (-stop % 8)
            if name_ref == 0 and size == 0 and fhash == 0 and fref == 0:
                continue  # padding between concatenated sections
            if fref not in self.file_ranges:
                raise MappingError(f"no filename found for function with hash=0x{fref:x}")
            rng = self.file_ranges[fref]
            if rng is None:
                continue
            old = records.get(name_ref)
            if old is None:
                records[name_ref] = [fhash, start, stop, rng[0], rng[1]]
            elif self._is_dummy(old[0], old[1], old[2]) and not self._is_dummy(fhash, start, stop):
                records[name_ref] = [fhash, start, stop, rng[0], rng[1]]

    def _is_dummy(self, fhash: int, start: int, stop: int) -> bool:
        """`RawCoverageMappingDummyChecker`: hash 0, one file, no expressions, one region
        with a zero counter."""
        if fhash:
            return False
        buf = self.covfun
        n, pos = _uleb(buf, start)
        if n != 1:
            return False
        _, pos = _uleb(buf, pos)
        n, pos = _uleb(buf, pos)
        if n != 0:
            return False
        n, pos = _uleb(buf, pos)
        if n != 1:
            return False
        enc, pos = _uleb(buf, pos)
        return enc & 3 == 0

    def decode(self, name_ref: int) -> Function:
        """`RawCoverageMappingReader::read` of one record."""
        fhash, start, stop, fstart, flen = self.records[name_ref]
        buf = self.covfun
        pos = start
        nfiles, pos = _uleb(buf, pos)
        files = []
        for _ in range(nfiles):
            idx, pos = _uleb(buf, pos)
            if idx >= flen:
                raise MappingError("filename index out of range")
            files.append(self.tu_files[fstart + idx])
        nexpr, pos = _uleb(buf, pos)
        kinds = [SUBTRACT] * nexpr
        lhs = [0] * nexpr
        rhs = [0] * nexpr

        def counter(enc):
            tag = enc & 3
            if tag == 0:
                return 0
            if tag == 1:
                return enc
            e = enc >> 2
            if e >= nexpr:
                raise MappingError("counter expression id out of range")
            kinds[e] = tag - 2  # Expression = 2: 2 subtract, 3 add
            return (e << 2) | 2

        for i in range(nexpr):
            a, pos = _uleb(buf, pos)
            b, pos = _uleb(buf, pos)
            lhs[i] = counter(a)
            rhs[i] = counter(b)
        regions = []
        for fid in range(nfiles):
            nreg, pos = _uleb(buf, pos)
            line = 0
            for _ in range(nreg):
                enc, pos = _uleb(buf, pos)
                kind, c, c2, expanded = CODE, 0, 0, 0
                if enc & 3:
                    c = counter(enc)
                elif enc & 4:
                    kind = EXPANSION
                    expanded = enc >> 3
                    if expanded >= nfiles:
                        raise MappingError("ExpandedFileID is invalid")
                else:
                    k = enc >> 3
                    if k == CODE:
                        pass
                    elif k == SKIPPED:
                        kind = SKIPPED
                    elif k == BRANCH:
                        kind = BRANCH
                        a, pos = _uleb(buf, pos)
                        c = counter(a)
                        a, pos = _uleb(buf, pos)
                        c2 = counter(a)
                    elif k == MCDC_BRANCH:
                        kind = MCDC_BRANCH
                        a, pos = _uleb(buf, pos)
                        c = counter(a)
                        a, pos = _uleb(buf, pos)
                        c2 = counter(a)
                        for _ in range(3):  # ID, TrueID, FalseID
                            _, pos = _uleb(buf, pos)
                    elif k == MCDC_DECISION:
                        kind = MCDC_DECISION
                        for _ in range(2):  # BitmapIdx, NumConditions
                            _, pos = _uleb(buf, pos)
                    else:
                        raise MappingError(f"region kind {k} is incorrect")
                dl, pos = _uleb(buf, pos)
                cs, pos = _uleb(buf, pos)
                nl, pos = _uleb(buf, pos)
                ce, pos = _uleb(buf, pos)
                line += dl
                if ce & (1 << 31):
                    kind = GAP
                    ce &= ~(1 << 31)
                if cs == 0 and ce == 0:
                    cs, ce = 1, UINT_MAX
                if (line, cs) > (line + nl, ce):
                    raise MappingError("counter mapping region locations are incorrect")
                regions.append([kind, c, c2, fid, expanded, line, cs, line + nl, ce])
        if pos != stop:
            pos = stop  # trailing bytes are ignored, as llvm-cov does
        # an expansion region counts as the first region of the file it expands
        if nfiles > 1:
            for _ in range(1, nfiles):
                by_file: list = [None] * nfiles
                for r in regions:
                    if r[0] == EXPANSION:
                        by_file[r[4]] = r
                for r in regions:
                    e = by_file[r[3]]
                    if e is not None:
                        e[1] = r[1]
                        by_file[r[3]] = None
        if nexpr:
            kinds, lhs, rhs = array("b", kinds), array("q", lhs), array("q", rhs)
        else:  # most functions (a header's unused inline functions): share one empty table
            kinds = lhs = rhs = ()
        return Function(name_ref, fhash, self.names.get(name_ref), tuple(files), kinds, lhs, rhs, regions)


def _strip_prefix(name: "str | None", first_file: "str | None") -> "str | None":
    """`getFuncNameWithoutPrefix`: a local function's `<file>;` (or `<file>:`) dropped."""
    if name is None or not first_file:
        return name
    if name.startswith(first_file):
        return name[len(first_file) + 1 :]
    return name


# --- the file view: segments and line stats -----------------------------------------


class _Seg:
    __slots__ = ("line", "col", "has", "reg", "entry", "gap", "maybe")

    def __init__(self, line, col, has, reg, entry, gap, maybe=False):
        self.line, self.col, self.has, self.reg, self.entry, self.gap, self.maybe = line, col, has, reg, entry, gap, maybe


# a merged region: [ls, cs, le, ce, kind, value, members]; value is a count (uint64) in
# the concrete view, or None (symbolic: `members` decide; `[]` = provably zero)
LS, CS, LE, CE, KIND, VALUE, MEMBERS = range(7)
# a function's counted regions (those of the file view: code, expansion, skipped, gap),
# STRIDE ints each: virtual file, kind, counter, ls, cs, le, ce, expanded file
STRIDE = 8


def _sort_and_combine(regions: list, symbolic: bool) -> list:
    """`sortNestedRegions` then `combineRegions`."""
    regions.sort(key=lambda r: (r[LS], r[CS], -r[LE], -r[CE], r[KIND]))
    out: list = []
    for r in regions:
        if out:
            a = out[-1]
            if a[LS] == r[LS] and a[CS] == r[CS] and a[LE] == r[LE] and a[CE] == r[CE]:
                if a[KIND] == r[KIND]:
                    if symbolic:
                        a[MEMBERS].extend(r[MEMBERS])
                    else:
                        a[VALUE] = (a[VALUE] + r[VALUE]) % U64
                continue
        out.append(r)
    return out


def build_segments(regions: list, symbolic: bool = False) -> list:
    """`SegmentBuilder::buildSegments` over one file's regions. Symbolic: a segment the
    builder would drop because its count equals the previous segment's is kept and
    marked `maybe` unless that equality holds for every count (same merged region, or
    both provably zero)."""
    regions = _sort_and_combine(regions, symbolic)
    segs: list = []
    active: list = []

    def same(a, b):
        if not symbolic:
            return a[VALUE] == b[VALUE]
        if a is b or (not a[MEMBERS] and not b[MEMBERS]):
            return True
        return None

    def start(reg, line, col, entry, emit_skipped=False):
        has = not emit_skipped and reg[KIND] != SKIPPED
        maybe = False
        if segs and not entry and not emit_skipped:
            last = segs[-1]
            if last.has == has and not last.entry:
                if not has:
                    return  # both uncounted: 0 == 0
                if last.maybe:
                    maybe = True
                else:
                    eq = same(last.reg, reg)
                    if eq:
                        return
                    if eq is None:
                        maybe = True
        if has:
            segs.append(_Seg(line, col, True, reg, entry, reg[KIND] == GAP, maybe))
        else:
            segs.append(_Seg(line, col, False, None, entry, False))

    def complete_until(loc, first):
        tail = active[first:]
        tail.sort(key=lambda r: (r[LE], r[CE]))  # stable
        active[first:] = tail
        n = len(active)
        for i in range(first + 1, n):
            done = active[i]
            prev = active[i - 1]
            seg_loc = (prev[LE], prev[CE])
            if loc is not None and seg_loc == loc:
                break
            if seg_loc == (done[LE], done[CE]):
                continue
            for j in range(i + 1, n):
                if (done[LE], done[CE]) == (active[j][LE], active[j][CE]):
                    done = active[j]
            start(done, seg_loc[0], seg_loc[1], False)
        last = active[-1]
        last_end = (last[LE], last[CE])
        if first and last_end != loc:
            start(active[first - 1], last_end[0], last_end[1], False)
        elif not first and (loc is None or loc != last_end):
            segs.append(_Seg(last_end[0], last_end[1], False, None, False, False))
        del active[first:]

    n = len(regions)
    for idx, cr in enumerate(regions):
        cur = (cr[LS], cr[CS])
        if active:
            keep = [r for r in active if not ((r[LE], r[CE]) <= cur)]
            if len(keep) != len(active):
                done = [r for r in active if (r[LE], r[CE]) <= cur]
                active[:] = keep + done
                complete_until(cur, len(keep))
        gap = cr[KIND] == GAP
        if cur == (cr[LE], cr[CE]):
            skipped = idx + 1 == n or cr[KIND] == SKIPPED
            start(active[-1] if active else cr, cur[0], cur[1], not gap, skipped)
            if skipped and active:
                start(active[-1], cur[0], cur[1], False)
            continue
        if idx + 1 == n or cur != (regions[idx + 1][LS], regions[idx + 1][CS]):
            start(cr, cur[0], cur[1], not gap)
        active.append(cr)
    if active:
        complete_until(None, 0)
    return segs


def line_stats(segs: list, fix: bool = True):
    """`LineCoverageIterator` from line 1 with `LineCoverageStats` per line: yields
    (line, mapped, wrapped segment, region-entry segments, ambiguous). `fix`: the rule
    of LLVM 18+ that a line on which a counted region starts is mapped even when a
    skipped region starts it. `ambiguous` (symbolic segments only): whether the line is
    mapped depends on counts."""
    i, n = 0, len(segs)
    wrapped = None
    line_segs: list = []
    line = 1
    while i < n:
        if line_segs:
            wrapped = line_segs[-1]
        line_segs = []
        while i < n and segs[i].line == line:
            line_segs.append(segs[i])
            i += 1
        entries = [s for s in line_segs if s.entry and s.has and not s.gap]
        counted_entry = any(s.entry and s.has for s in line_segs)
        wrapped_has = wrapped is not None and wrapped.has

        def mapped_if(start_skipped):
            m = not start_skipped and (wrapped_has or bool(entries))
            return m or (fix and counted_entry)

        if line_segs:
            front = line_segs[0]
            if front.maybe:
                certain = next((s for s in line_segs if not s.maybe), None)
                sos = certain is not None and not certain.has and certain.entry
                m_kept, m_dropped = mapped_if(False), mapped_if(sos)
                yield line, m_kept or m_dropped, wrapped, entries, m_kept != m_dropped
            else:
                yield line, mapped_if(not front.has and front.entry), wrapped, entries, False
        else:
            yield line, mapped_if(False), wrapped, entries, False
        line += 1


def line_counts(segs: list, fix: bool = True) -> dict:
    """{line: execution count} of the mapped lines of concrete segments (`DA:` lines)."""
    out = {}
    for line, mapped, wrapped, entries, _ in line_stats(segs, fix):
        if not mapped:
            continue
        count = wrapped.reg[VALUE] if wrapped is not None and wrapped.has else 0
        for s in entries:
            count = max(count, s.reg[VALUE])
        out[line] = count
    return out


# --- the build: functions loaded against a profile -----------------------------------


class Build:
    """llvm-cov's coverage of a binary against a profile's function set.

    `profile` is a list of units (one per profile data record, in the order the window
    log numbers them): (NameRef, FuncHash, counts) with `counts` the cumulative counter
    values. Functions are loaded, the file views' line structure is computed once, and
    every function keeps the lines it can execute with the regions that decide them."""

    def __init__(self, mapping: CoverageMapping, profile: list, fix: "bool | None" = None):
        self.mapping = mapping
        self.fix = mapping.version >= 7 if fix is None else fix
        self.units = profile
        merged: dict = {}  # (NameRef, hash) -> summed counts
        by_name: dict = {}  # NameRef -> set of hashes
        for name_ref, fhash, counts in profile:
            key = (name_ref, fhash)
            by_name.setdefault(name_ref, set()).add(fhash)
            if key in merged:
                old = merged[key]
                merged[key] = [(a + b) % U64 for a, b in zip(old, counts)] if len(old) == len(counts) else old
            else:
                merged[key] = counts
        self.cumulative = merged
        self.stats = {"records": len(mapping.records), "hash_mismatch": 0, "counter_out_of_range": 0, "dummy_with_counts": 0, "duplicate_provenance": 0, "functions": 0, "regions": 0, "ambiguous_lines": 0, "maybe_segments": 0}
        self.functions: list = []
        self.by_key: dict = {}  # (NameRef, hash) -> function index
        provenance: set = set()
        files = mapping.filenames
        for name_ref in mapping.records:
            fn = mapping.decode(name_ref)
            hashes = by_name.get(name_ref)
            if hashes is not None and fn.hash not in hashes:
                self.stats["hash_mismatch"] += 1
                continue
            needed: set = set()
            for r in fn.regions:
                if r[0] != MCDC_DECISION:
                    _counters_of(fn, r[1], needed)
                    _counters_of(fn, r[2], needed)
            fn.max_counter = max(needed, default=-1)
            if hashes is not None:
                counts = merged[(name_ref, fn.hash)]
                if fn.max_counter >= len(counts):
                    self.stats["counter_out_of_range"] += 1
                    continue
                fn.key = (name_ref, fn.hash)
                first = counts[0] if counts else 0
            else:
                first = 0
            if len(fn.regions) == 1 and fn.regions[0][1] == 0 and first > 0:
                self.stats["dummy_with_counts"] += 1
                continue
            names = tuple(files[f] for f in fn.files)
            prov = (names, _strip_prefix(fn.name, names[0] if names else None))
            if prov in provenance:
                self.stats["duplicate_provenance"] += 1
                continue
            provenance.add(prov)
            counted = array("q")
            for r in fn.regions:
                if r[0] in (CODE, EXPANSION, SKIPPED, GAP):
                    counted.extend((r[3], r[0], r[1], r[5], r[6], r[7], r[8], r[4]))
            fn.counted = counted
            fn.regions = None  # the file view needs the counted regions only
            self.stats["regions"] += len(counted) // STRIDE
            if fn.key is not None:
                self.by_key[fn.key] = len(self.functions)
            self.functions.append(fn)
        self.stats["functions"] = len(self.functions)
        self.unit_function = [self.by_key.get((name_ref, fhash)) for name_ref, fhash, _ in profile]
        self._index()

    # each file's regions: (function, region index) pairs
    def _file_regions(self) -> dict:
        per_file: dict = {}
        for fi, fn in enumerate(self.functions):
            c = fn.counted
            files = fn.files
            for k in range(0, len(c), STRIDE):
                f = files[c[k]]
                a = per_file.get(f)
                if a is None:
                    a = per_file[f] = array("I")
                a.append(fi)
                a.append(k // STRIDE)
        return per_file

    def _regions_of(self, pairs, values=None) -> list:
        out = []
        fns = self.functions
        for j in range(0, len(pairs), 2):
            fi, ri = pairs[j], pairs[j + 1]
            fn = fns[fi]
            c = fn.counted
            k = ri * STRIDE
            counter = c[k + 2]
            if values is None:
                members = [(fi, ri)] if counter and fn.key is not None else []
                out.append([c[k + 3], c[k + 4], c[k + 5], c[k + 6], c[k + 1], None, members])
            else:
                out.append([c[k + 3], c[k + 4], c[k + 5], c[k + 6], c[k + 1], values(fi, counter), None])
        return out

    def _index(self) -> None:
        """Per function: array of (file, line, region) triples, the line executed when
        that region counts; per file: the mapped lines (cumulative profile)."""
        self.entries = [array("I") for _ in self.functions]
        self.executable: dict = {}
        ambiguous_files = set()
        self._per_file = per_file = self._file_regions()
        for file_id, pairs in per_file.items():
            segs = build_segments(self._regions_of(pairs), symbolic=True)
            self.stats["maybe_segments"] += sum(s.maybe for s in segs)
            mapped_lines = set()
            for line, mapped, wrapped, entries, ambiguous in line_stats(segs, self.fix):
                if ambiguous:
                    self.stats["ambiguous_lines"] += 1
                    ambiguous_files.add(file_id)
                if not mapped:
                    continue
                mapped_lines.add(line)
                regs = [s.reg for s in entries]
                if wrapped is not None and wrapped.has:
                    regs.append(wrapped.reg)
                for reg in regs:
                    for fi, ri in reg[MEMBERS]:
                        e = self.entries[fi]
                        e.append(file_id)
                        e.append(line)
                        e.append(ri)
            self.executable[file_id] = mapped_lines
        # a line whose mapped-ness depends on counts: as the cumulative profile decides
        if ambiguous_files:
            values = self.cumulative_values()
            unmapped = set()
            for file_id in ambiguous_files:
                exact = self.file_line_counts(file_id, values)
                unmapped |= {(file_id, line) for line in self.executable[file_id] - set(exact)}
                self.executable[file_id] = set(exact)
            if unmapped:
                for fi, e in enumerate(self.entries):
                    keep = array("I")
                    for k in range(0, len(e), 3):
                        if (e[k], e[k + 1]) not in unmapped:
                            keep.extend(e[k : k + 3])
                    self.entries[fi] = keep

    # --- counts -------------------------------------------------------------------

    def cumulative_values(self):
        """A `values(function, counter)` of the cumulative profile."""
        memos: dict = {}

        def values(fi, counter):
            fn = self.functions[fi]
            if fn.key is None or counter == 0:
                return 0
            memo = memos.setdefault(fi, {})
            return evaluate(fn, counter, self.cumulative[fn.key], memo) % U64

        return values

    def window_values(self, window: dict):
        """A `values(function, counter)` of a window {unit: {counter: increment}}
        (units of one function add up)."""
        per_fn: dict = {}
        for unit, counts in window.items():
            fi = self.unit_function[unit]
            if fi is None:
                continue
            acc = per_fn.setdefault(fi, {})
            for c, inc in counts.items():
                acc[c] = acc.get(c, 0) + inc
        memos: dict = {}

        def values(fi, counter):
            counts = per_fn.get(fi)
            if counts is None or counter == 0:
                return 0
            return evaluate(self.functions[fi], counter, counts, memos.setdefault(fi, {})) % U64

        return values

    def file_line_counts(self, file_id: int, values) -> dict:
        """{line: count} of one file's mapped lines under `values`: llvm-cov's file view,
        computed directly (the reference the decomposition is checked against)."""
        return line_counts(build_segments(self._regions_of(self._per_file[file_id], values)), self.fix)

    def line_counts(self, values) -> dict:
        """{filename: {line: count}} of every file: what `llvm-cov export -format=lcov`
        prints as `DA:` lines."""
        names = self.mapping.filenames
        out: dict = {}
        for file_id in self._per_file:
            out.setdefault(names[file_id], {}).update(self.file_line_counts(file_id, values))
        return out

    def report_lines(self, values) -> dict:
        """{filename: [lines, covered]} as `llvm-cov report` counts them: per function the
        lines of its own main-file view, instantiations (functions of one file starting at
        one place) merged by taking the largest numbers, summed per file. It differs from
        the file view (a macro's definition lines, lines shared by two functions)."""
        names = self.mapping.filenames
        groups: dict = {}
        for fi, fn in enumerate(self.functions):
            c = fn.counted
            expanded = {c[k + 7] for k in range(0, len(c), STRIDE) if c[k + 1] == EXPANSION}
            main = next((v for v in range(len(fn.files)) if v not in expanded), None)
            if main is None:
                continue
            regs = [[c[k + 3], c[k + 4], c[k + 5], c[k + 6], c[k + 1], values(fi, c[k + 2]), None] for k in range(0, len(c), STRIDE) if c[k] == main]
            if not regs:
                continue
            key = (names[fn.files[main]], regs[0][LS], regs[0][CS])
            counts = line_counts(build_segments(regs), self.fix)
            g = groups.setdefault(key, [0, 0])
            g[0] = max(g[0], len(counts))
            g[1] = max(g[1], sum(1 for v in counts.values() if v))
        out: dict = {}
        for (f, _, _), (n, cov) in groups.items():
            t = out.setdefault(f, [0, 0])
            t[0] += n
            t[1] += cov
        return out

    def function_lines(self, fi: int, counts: dict) -> set:
        """The (file id, line) function `fi` executes when its counters count `counts`."""
        fn = self.functions[fi]
        e = self.entries[fi]
        if not e:
            return set()
        c = fn.counted
        memo: dict = {}
        val: dict = {}
        out = set()
        for k in range(0, len(e), 3):
            key = (e[k], e[k + 1])
            if key in out:
                continue
            ri = e[k + 2]
            v = val.get(ri)
            if v is None:
                v = val[ri] = evaluate(fn, c[ri * STRIDE + 2], counts, memo) % U64
            if v:
                out.add(key)
        return out


# --- the LineMapper ---------------------------------------------------------------------


def load_binary(path: str, compilation_dir: str = "") -> CoverageMapping:
    sections = binary_sections(path)
    if "__llvm_covmap" not in sections:
        raise MappingError(f"{path}: no __llvm_covmap section (not built with -fcoverage-mapping / -C instrument-coverage?)")
    if len(sections["__llvm_covmap"]) != 1:
        raise MappingError(f"{path}: the size of coverage mapping section is not one")
    names = {}
    for raw in sections.get("__llvm_prf_names", []):
        names.update(names_section(raw))
    return CoverageMapping(sections["__llvm_covmap"][0], _join_aligned(sections.get("__llvm_covfun", [])), names, compilation_dir)


def profile_units(profile_path: str) -> "tuple[list, dict]":
    """([(NameRef, FuncHash, cumulative counts)] in the window log's unit order, {NameRef:
    name}) of a raw profile (`llvm_coverage.Profile`)."""
    from ganda.core.engine.llvm_coverage import Profile

    p = Profile(profile_path)
    try:
        raw = p.counters()
        units = []
        for start, name_ref, fhash, n in p.records():
            if p.counter_size == 8:
                counts = array("Q", raw[8 * start : 8 * (start + n)])
                if sys.byteorder != "little":
                    counts.byteswap()
            else:  # single-byte counters: 0 = covered
                counts = array("Q", (1 if b == 0 else 0 for b in raw[start : start + n]))
            units.append((name_ref, fhash, counts))
        return units, p._names()
    finally:
        p.close()


class LlvmLineMapper(LineMapper):
    """`mc_report.LineMapper` of an LLVM-instrumented build: `binary` (ELF or Mach-O) and
    the raw profile of the campaign (`profile`: its data records number the units of the
    window log; its counters, cumulative, settle what llvm-cov would on it; or a list of
    units (NameRef, FuncHash, counts)). `compilation_dir` is llvm-cov's
    `-compilation-dir`; `fix` the line rule of llvm-cov 18+ (default: from the covmap
    version, Version7 being LLVM 18+)."""

    def __init__(self, binary: str, profile: "str | list", compilation_dir: str = "", fix: "bool | None" = None):
        mapping = load_binary(binary, compilation_dir)
        if isinstance(profile, str):
            units, names = profile_units(profile)
            for k, v in names.items():
                mapping.names.setdefault(k, v)
        else:
            units = profile
        self.build = Build(mapping, units, fix)
        if units and mapping.records and not any(f is not None for f in self.build.unit_function) and not any(fn.key for fn in self.build.functions):
            raise MappingError(f"none of the profile's {len(units)} functions has a coverage record in {binary}: a profile of another build (or of another image)?")
        self._names = mapping.filenames

    def unit_lines(self, unit, name, encoded) -> frozenset:
        u = int(unit)
        b = self.build
        fi = b.unit_function[u] if u < len(b.unit_function) else None
        if fi is None:
            return frozenset()
        counts: dict = {}
        for c, inc in encoded:
            counts[c] = counts.get(c, 0) + inc
        names = self._names
        return frozenset((names[f], line) for f, line in b.function_lines(fi, counts))

    def unit_files(self, unit) -> set:
        """The files function `unit`'s lines are in (its line entries' files)."""
        u = int(unit)
        b = self.build
        fi = b.unit_function[u] if u < len(b.unit_function) else None
        if fi is None:
            return set()
        e = b.entries[fi]
        names = self._names
        return {names[e[k]] for k in range(0, len(e), 3)}

    def merge(self, encodings: list) -> list:
        """Several windows' [[counter, increment], ...] of one unit, summed."""
        total: dict = {}
        for enc in encodings:
            for c, inc in enc:
                total[c] = total.get(c, 0) + inc
        return [[c, inc] for c, inc in total.items()]

    def window_lines(self, window: dict) -> frozenset:
        """The lines a whole window {unit: [[counter, increment], ...]} executes."""
        out: set = set()
        for unit, enc in window.items():
            out |= self.unit_lines(unit, None, enc)
        return frozenset(out)

    def executable_lines(self) -> dict:
        out: dict = {}
        for file_id, lines in self.build.executable.items():
            if lines:
                out.setdefault(self._names[file_id], set()).update(lines)
        return out

    def covered_lines(self) -> dict:
        """{file: {line: count}} of the cumulative profile (llvm-cov's `DA:` lines)."""
        return self.build.line_counts(self.build.cumulative_values())


def main(argv=None) -> int:  # pragma: no cover - a debugging aid
    import argparse

    ap = argparse.ArgumentParser(description="dump the line coverage of an LLVM build as lcov DA lines")
    ap.add_argument("binary")
    ap.add_argument("profile")
    a = ap.parse_args(argv)
    m = LlvmLineMapper(a.binary, a.profile)
    for f, lines in sorted(m.covered_lines().items()):
        print(f"SF:{f}")
        for line, count in sorted(lines.items()):
            print(f"DA:{line},{count}")
        print("end_of_record")
    print(m.build.stats, file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
