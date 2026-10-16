"""ADS 区块编译器与标量解码器单元测试。

覆盖：单点/相邻/不连续/多组/乱序分组、类型宽度、max_block_bytes
边界（空洞计入跨度）、超大点、完全/部分/大覆小/同偏移不同组重叠、
重叠判定先于分块、各原生类型解码与越界/坏 UTF-8 错误传播。
"""

from __future__ import annotations

import struct

import pytest

from core.application.errors import ConfigError
from core.infrastructure.protocol.ads.codec import (
    compile_decoder,
    compile_notification_blocks,
)
from core.infrastructure.protocol.ads.mapping import ADSPoint


def p(
    name: str,
    offset: int,
    kind: str = "DINT",
    *,
    group: int = 0x4020,
    size: int | None = None,
) -> ADSPoint:
    width = size if size is not None else {
        "BOOL": 1, "SINT": 1, "USINT": 1, "INT": 2, "UINT": 2,
        "DINT": 4, "UDINT": 4, "LINT": 8, "ULINT": 8,
        "REAL": 4, "LREAL": 8,
    }[kind]
    return ADSPoint(name, group, offset, kind, width)


# ── 区块划分 ──────────────────────────────────────────────────────────────


def test_single_point_builds_one_block() -> None:
    blocks = compile_notification_blocks((p("a", 100),))
    assert len(blocks) == 1
    block = blocks[0]
    assert (block.group, block.offset, block.size) == (0x4020, 100, 4)
    assert [(field[0].point_id, field[1]) for field in block.fields] == [("a", 0)]


def test_unsorted_input_is_sorted_within_block() -> None:
    blocks = compile_notification_blocks(
        (p("c", 108), p("a", 100), p("b", 104))
    )
    assert len(blocks) == 1
    assert blocks[0].offset == 100
    assert blocks[0].size == 12
    assert [field[0].point_id for field in blocks[0].fields] == ["a", "b", "c"]
    assert [field[1] for field in blocks[0].fields] == [0, 4, 8]


def test_disjoint_points_share_block_hole_counts_in_span() -> None:
    blocks = compile_notification_blocks((p("a", 100), p("b", 200)))
    assert len(blocks) == 1
    assert blocks[0].offset == 100
    assert blocks[0].size == 104  # 200 + 4 - 100，中间空洞计入跨度


def test_different_groups_split_blocks_regardless_of_offsets() -> None:
    blocks = compile_notification_blocks(
        (p("a", 100, group=1), p("b", 100, group=2))
    )
    assert len(blocks) == 2
    assert blocks[0].group == 1
    assert blocks[1].group == 2
    for block in blocks:
        assert block.size == 4


def test_blocks_sorted_by_group_and_offset() -> None:
    # 同组两点跨度超过 max_bytes 拆块，组间按 (group, offset) 排序。
    blocks = compile_notification_blocks(
        (
            p("g2", 100, group=2),
            p("g1b", 2000, group=1),
            p("g1a", 0, group=1),
        ),
        max_bytes=1024,
    )
    assert [(b.group, b.offset) for b in blocks] == [(1, 0), (1, 2000), (2, 100)]


def test_max_block_bytes_exact_boundary_keeps_one_block() -> None:
    # 100..107 总跨度恰好 8。
    blocks = compile_notification_blocks(
        (p("a", 100), p("b", 104)), max_bytes=8
    )
    assert len(blocks) == 1
    assert blocks[0].size == 8


def test_max_block_bytes_one_byte_over_starts_new_block() -> None:
    blocks = compile_notification_blocks(
        (p("a", 100), p("b", 108)), max_bytes=8
    )
    assert len(blocks) == 2
    assert [(b.offset, b.size) for b in blocks] == [(100, 4), (108, 4)]


def test_hole_filling_span_can_exceed_max_bytes_and_split() -> None:
    # a 在 100，b 在 200：同一区块跨度 104 > 64，拆为两个区块。
    blocks = compile_notification_blocks(
        (p("a", 100), p("b", 200)), max_bytes=64
    )
    assert len(blocks) == 2
    assert [b.offset for b in blocks] == [100, 200]


def test_oversized_point_rejected() -> None:
    with pytest.raises(ValueError, match="exceeds notification block size"):
        compile_notification_blocks((p("s", 0, "STRING", size=2048),), max_bytes=1024)


def test_max_bytes_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_bytes must be positive"):
        compile_notification_blocks((p("a", 0),), max_bytes=0)


# ── 重叠（必须先于分块判定）────────────────────────────────────────────────


def test_identical_offset_overlap_rejected() -> None:
    with pytest.raises(ConfigError, match="overlap"):
        compile_notification_blocks((p("a", 100), p("b", 100)))


def test_partial_overlap_rejected() -> None:
    with pytest.raises(ConfigError, match="overlap"):
        compile_notification_blocks((p("a", 100), p("b", 102)))


def test_big_point_covers_small_point_rejected() -> None:
    with pytest.raises(ConfigError, match="overlap"):
        compile_notification_blocks(
            (p("big", 100, "LREAL"), p("small", 104, "INT"))
        )


def test_same_offset_different_groups_is_not_overlap() -> None:
    blocks = compile_notification_blocks(
        (p("a", 100, group=1), p("b", 100, group=2))
    )
    assert len(blocks) == 2


def test_overlap_rejected_before_block_size_split() -> None:
    """即使第二点会使跨度超 max_bytes，也必须先报重叠而非静默拆块。"""
    with pytest.raises(ConfigError, match="overlap"):
        compile_notification_blocks(
            (p("a", 100), p("b", 102, group=0x4020)), max_bytes=8
        )


def test_adjacency_is_not_overlap() -> None:
    blocks = compile_notification_blocks((p("a", 100), p("b", 104)))
    assert len(blocks) == 1


# ── 标量解码 ──────────────────────────────────────────────────────────────


def test_decode_all_integer_widths_and_bool() -> None:
    raw = struct.pack("<BbHhIiqQ?", 1, 2, 3, 4, 5, 6, 7, 8, True)
    specs = [
        ("USINT", 1, 0), ("SINT", 1, 1), ("UINT", 2, 2), ("INT", 2, 4),
        ("UDINT", 4, 6), ("DINT", 4, 10), ("ULINT", 8, 14), ("LINT", 8, 22),
        ("BOOL", 1, 30),
    ]
    for kind, width, offset in specs:
        point = p(kind, 0, kind)
        assert point.size == width
        decoder = compile_decoder(point)
        assert decoder(raw, offset) == {
            "USINT": 1, "SINT": 2, "UINT": 3, "INT": 4, "UDINT": 5,
            "DINT": 6, "ULINT": 7, "LINT": 8, "BOOL": True,
        }[kind]


def test_decode_real_and_lreal() -> None:
    raw = struct.pack("<fd", 1.25, -2.5)
    assert compile_decoder(p("r", 0, "REAL"))(raw, 0) == pytest.approx(1.25)
    assert compile_decoder(p("d", 0, "LREAL"))(raw, 4) == pytest.approx(-2.5)


def test_decode_string_stops_at_nul_and_handles_full_length() -> None:
    point = p("s", 0, "STRING", size=8)
    decoder = compile_decoder(point)
    assert decoder(b"hello\x00??", 0) == "hello"
    # 无 NUL 终止时使用整个定长区域。
    assert decoder(b"wind-hub", 0) == "wind-hub"


def test_decode_beyond_raw_raises_struct_error() -> None:
    decoder = compile_decoder(p("a", 0))
    with pytest.raises(struct.error):
        decoder(b"\x01\x02", 0)


def test_decode_invalid_utf8_raises_unicode_error() -> None:
    decoder = compile_decoder(p("s", 0, "STRING", size=4))
    with pytest.raises(UnicodeDecodeError):
        decoder(b"\xff\xff\xff\xff", 0)


def test_decoder_size_mismatch_rejected() -> None:
    broken = ADSPoint("bad", 0x4020, 0, "DINT", 2)
    with pytest.raises(ValueError, match="size mismatch"):
        compile_decoder(broken)
