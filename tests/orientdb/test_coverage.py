"""The JaCoCo execution-data format and the agent's dump protocol (`ganda.orientdb.code_coverage.collector`),
against a fake agent -- no Docker, no JVM."""
import io
import socket
import struct
import threading
from types import SimpleNamespace

from ganda.orientdb.code_coverage import collector as coverage
from ganda.orientdb.code_coverage.collector import BLOCK_CMD_DUMP, BLOCK_CMD_OK, BLOCK_EXECUTIONDATA, HEADER, Collector, ExecData, unique_counts


def _data(*classes) -> ExecData:
    d = ExecData()
    for cid, name, size, bits in classes:
        d.add(cid, name, size, sum(1 << i for i in bits))
    return d


def test_the_wire_packs_probes_least_significant_bit_first():
    d = _data((7, "a/B", 10, [0, 2, 9]))
    raw = d.to_bytes("s", 1, 2)
    # header, session block, then: 0x11, id, utf name, varint 10, 0b101, 0b10
    tail = bytes([BLOCK_EXECUTIONDATA]) + struct.pack(">q", 7) + struct.pack(">H", 3) + b"a/B" + bytes([10, 0x05, 0x02])
    assert raw.startswith(HEADER)
    assert raw.endswith(tail)


def test_a_varint_takes_seven_bits_a_byte():
    buf = io.BytesIO()
    coverage._write_varint(buf, 300)
    assert buf.getvalue() == bytes([0xAC, 0x02])
    buf.seek(0)
    assert coverage._varint(buf) == 300


def test_round_trip_keeps_every_probe():
    d = _data((1, "x/A", 3, [1]), (-5, "x/y/C", 130, [0, 64, 129]), (9, "z/D", 0, []))
    back = ExecData.from_bytes(d.to_bytes())
    assert back.masks == d.masks and back.sizes == d.sizes and back.names == d.names


def test_merge_counts_the_new_probes_and_minus_keeps_the_rest():
    a = _data((1, "p/A", 8, [0, 1]), (2, "p/B", 4, [3]))
    b = _data((1, "p/A", 8, [1, 2]), (3, "q/C", 2, [0]))
    assert a.merge(b) == 2  # A.2 and C.0
    assert a.covered() == 5 and a.classes_hit() == 3 and a.loaded_probes() == 14
    rest = a.minus(_data((1, "p/A", 8, [0, 1, 2])))
    assert rest.covered() == 2 and set(rest.masks) == {2, 3}
    assert a.by_package(depth=1) == {"p": [4, 12, 2], "q": [1, 2, 1]}


def test_unique_counts_what_no_other_label_hit():
    labels = {
        "x": _data((1, "p/A", 8, [0, 1, 2])),
        "y": _data((1, "p/A", 8, [2, 3]), (2, "p/B", 4, [0])),
        "z": _data((1, "p/A", 8, [3])),
    }
    assert unique_counts(labels) == {"x": 2, "y": 1, "z": 0}


def _fake_agent(answers: list) -> "tuple[int, list]":
    """Serves one dump per answer; records each command's (dump, reset). Everything the
    client sends after its command must be whole header blocks (the ACK nudges)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    commands = []

    def serve():
        for answer in answers:
            conn, _ = srv.accept()
            with conn:
                got = b""
                while len(got) < len(HEADER) + 3:
                    got += conn.recv(64)
                assert got[: len(HEADER)] == HEADER and got[len(HEADER)] == BLOCK_CMD_DUMP
                commands.append((got[-2], got[-1]))
                conn.sendall(answer.to_bytes("agent", 1, 2) + bytes([BLOCK_CMD_OK]))
                rest = got[len(HEADER) + 3 :]
                while chunk := conn.recv(64):
                    rest += chunk
                assert rest and rest == HEADER * (len(rest) // len(HEADER))
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], commands


def test_dump_reads_until_the_agents_ok():
    answer = _data((4, "a/B", 5, [1, 4]))
    port, commands = _fake_agent([answer])
    got = coverage.dump("127.0.0.1", port, reset=True)
    assert got.masks == answer.masks
    assert commands == [(1, 1)]


def test_the_collector_attributes_each_window():
    windows = [
        _data((1, "p/A", 8, [0])),  # boot
        _data((1, "p/A", 8, [0, 1])),  # setup
        _data((1, "p/A", 8, [2]), (2, "p/B", 2, [0])),  # t1 / s1
        _data((1, "p/A", 8, [2, 3])),  # t1 / s2
        _data(),  # after the seed
    ]
    port, _ = _fake_agent(windows)
    server = SimpleNamespace(coverage_address=lambda: ("127.0.0.1", port))
    case = SimpleNamespace(generator_name="t1")
    c = Collector(server, "variant")
    c.after_setup()
    c.after_variant(case, "s1")
    c.after_variant(case, "s2")
    c.after_seed(42)
    s = c.summary()
    assert s["probes_covered"] == 5 and s["dumps"] == 5 and s["dump_failures"] == 0
    assert s["generators"]["t1"] == {"covered": 3, "unique": 3, "classes": 2}
    assert s["generators"]["boot"]["unique"] == 0 and s["generators"]["setup"]["unique"] == 1
    assert s["variants"]["t1/s1"] == {"covered": 2, "unique": 1}  # A.2 is s2's too
    assert s["variants"]["t1/s2"] == {"covered": 2, "unique": 1}
    assert s["growth"] == [{"instances": 1, "seed": 42, "elapsed": s["growth"][0]["elapsed"], "probes": 5, "classes": 2}]


def test_a_dead_agent_is_a_lost_window_not_a_crash():
    server = SimpleNamespace(coverage_address=lambda: ("127.0.0.1", 1))
    c = Collector(server, "seed")
    c.after_seed(1)
    assert c.summary()["dump_failures"] == 1 and c.summary()["probes_covered"] == 0
