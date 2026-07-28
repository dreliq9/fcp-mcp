"""Tests for rational time math."""

from fcp_mcp.fcpxml.time_utils import (
    FRAME_DURATIONS,
    RationalTime,
    fps_from_frame_duration,
    frame_duration_from_fps,
)


class TestRationalTimeParsing:
    def test_parse_fraction(self):
        t = RationalTime.from_fcpxml("86400/24000s")
        assert t.numerator == 86400
        assert t.denominator == 24000

    def test_parse_integer(self):
        t = RationalTime.from_fcpxml("0s")
        assert t.numerator == 0
        assert t.denominator == 1

    def test_parse_integer_nonzero(self):
        t = RationalTime.from_fcpxml("10s")
        assert t.numerator == 10
        assert t.denominator == 1

    def test_parse_none(self):
        t = RationalTime.from_fcpxml(None)
        assert t.is_zero


class TestRationalTimeOutput:
    def test_to_fcpxml_fraction(self):
        t = RationalTime(1001, 30000)
        assert t.to_fcpxml() == "1001/30000s"

    def test_to_fcpxml_integer(self):
        t = RationalTime(0, 1)
        assert t.to_fcpxml() == "0s"

    def test_to_seconds(self):
        t = RationalTime(30030, 30000)
        assert abs(t.to_seconds() - 1.001) < 0.0001

    def test_roundtrip(self):
        original = "150150/30000s"
        t = RationalTime.from_fcpxml(original)
        assert t.to_fcpxml() == original


class TestRationalTimeArithmetic:
    def test_add(self):
        a = RationalTime(1001, 30000)
        b = RationalTime(1001, 30000)
        result = a + b
        assert result.to_seconds() == pytest.approx(2 * 1001 / 30000, abs=1e-9)

    def test_subtract(self):
        a = RationalTime(2002, 30000)
        b = RationalTime(1001, 30000)
        result = a - b
        assert result == RationalTime(1001, 30000)

    def test_multiply_int(self):
        t = RationalTime(1001, 30000)
        result = t * 10
        assert result.to_seconds() == pytest.approx(10 * 1001 / 30000, abs=1e-9)

    def test_divide_int(self):
        t = RationalTime(2002, 30000)
        result = t / 2
        assert result == RationalTime(1001, 30000)

    def test_divide_rational(self):
        a = RationalTime(3003, 30000)
        b = RationalTime(1001, 30000)
        result = a / b
        assert result == pytest.approx(3.0, abs=1e-9)


class TestRationalTimeComparison:
    def test_equal(self):
        assert RationalTime(1001, 30000) == RationalTime(1001, 30000)

    def test_equal_different_denominator(self):
        assert RationalTime(2, 4) == RationalTime(1, 2)

    def test_less_than(self):
        assert RationalTime(1001, 30000) < RationalTime(2002, 30000)

    def test_greater_than(self):
        assert RationalTime(2002, 30000) > RationalTime(1001, 30000)


class TestTimecode:
    def test_timecode_ndf(self):
        # 5 seconds at 29.97fps
        t = RationalTime(150150, 30000)  # 5.005 seconds
        tc = t.to_timecode(29.97)
        assert tc.startswith("00:00:05:")

    def test_timecode_zero(self):
        t = RationalTime.zero()
        tc = t.to_timecode(24)
        assert tc == "00:00:00:00"


class TestFrameDurations:
    def test_2997_fps(self):
        fd = FRAME_DURATIONS[29.97]
        fps = fps_from_frame_duration(fd)
        assert fps == pytest.approx(29.97, abs=0.01)

    def test_24_fps(self):
        fd = FRAME_DURATIONS[24]
        fps = fps_from_frame_duration(fd)
        assert fps == 24.0

    def test_frame_duration_from_fps(self):
        fd = frame_duration_from_fps(29.97)
        assert fd == RationalTime(1001, 30000)


import pytest
