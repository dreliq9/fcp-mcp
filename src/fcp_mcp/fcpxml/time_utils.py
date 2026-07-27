"""Rational time math for FCPXML.

FCPXML uses CMTime-style rational fractions: "numerator/denominators" or "Ns"
Examples: "86400/24000s", "0s", "1001/30000s"

This module provides lossless time arithmetic — no floats until display.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RationalTime:
    """Rational time value matching FCPXML's CMTime representation."""

    numerator: int
    denominator: int = 1

    def __post_init__(self):
        if self.denominator == 0:
            raise ValueError("Denominator cannot be zero")
        if self.denominator < 0:
            object.__setattr__(self, "numerator", -self.numerator)
            object.__setattr__(self, "denominator", -self.denominator)

    @classmethod
    def from_fcpxml(cls, value: str) -> RationalTime:
        """Parse FCPXML time string like '86400/24000s' or '0s'."""
        if value is None:
            return cls(0, 1)
        value = value.strip()
        value = value.removesuffix("s")
        if "/" in value:
            num, den = value.split("/")
            return cls(int(num), int(den))
        return cls(int(value), 1)

    @classmethod
    def from_seconds(cls, seconds: float, timescale: int = 30000) -> RationalTime:
        """Create from seconds with given timescale."""
        return cls(round(seconds * timescale), timescale)

    @classmethod
    def from_frames(cls, frames: int, frame_duration: RationalTime) -> RationalTime:
        """Create time from frame count and frame duration."""
        return cls(
            frames * frame_duration.numerator,
            frame_duration.denominator,
        )

    @classmethod
    def zero(cls) -> RationalTime:
        return cls(0, 1)

    def to_fcpxml(self) -> str:
        """Convert to FCPXML time string."""
        if self.denominator == 1:
            return f"{self.numerator}s"
        return f"{self.numerator}/{self.denominator}s"

    def to_seconds(self) -> float:
        """Convert to float seconds."""
        return self.numerator / self.denominator

    def to_frames(self, frame_duration: RationalTime) -> int:
        """Convert to frame count given a frame duration."""
        # self / frame_duration
        result = (self.numerator * frame_duration.denominator) / (
            self.denominator * frame_duration.numerator
        )
        return round(result)

    def to_timecode(self, fps: float, drop_frame: bool = False) -> str:
        """Convert to HH:MM:SS:FF timecode string."""
        total_seconds = self.to_seconds()
        if total_seconds < 0:
            sign = "-"
            total_seconds = -total_seconds
        else:
            sign = ""

        if drop_frame and fps in (29.97, 59.94):
            return sign + self._to_drop_frame_tc(total_seconds, fps)

        total_frames = round(total_seconds * fps)
        frames_per_second = round(fps)
        ff = total_frames % frames_per_second
        total_seconds_int = total_frames // frames_per_second
        ss = total_seconds_int % 60
        mm = (total_seconds_int // 60) % 60
        hh = total_seconds_int // 3600
        return f"{sign}{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

    def _to_drop_frame_tc(self, total_seconds: float, fps: float) -> str:
        """Calculate drop-frame timecode."""
        frames_per_second = round(fps)
        drop = 2 if fps < 31 else 4  # 29.97 drops 2, 59.94 drops 4
        total_frames = round(total_seconds * fps)

        # Drop-frame algorithm
        d = total_frames // (17982 if fps < 31 else 35964)
        m = total_frames % (17982 if fps < 31 else 35964)
        if m < drop:
            m += drop
        frame_number = total_frames + drop * (d * 9 + (m - drop) // (1798 if fps < 31 else 3596))

        ff = frame_number % frames_per_second
        ss = (frame_number // frames_per_second) % 60
        mm = (frame_number // (frames_per_second * 60)) % 60
        hh = frame_number // (frames_per_second * 3600)
        return f"{hh:02d}:{mm:02d}:{ss:02d};{ff:02d}"

    def _common_denominator(self, other: RationalTime) -> tuple[int, int, int]:
        """Find common denominator and return (num_a, num_b, common_den)."""
        lcd = _lcm(self.denominator, other.denominator)
        a = self.numerator * (lcd // self.denominator)
        b = other.numerator * (lcd // other.denominator)
        return a, b, lcd

    def __add__(self, other: RationalTime) -> RationalTime:
        a, b, lcd = self._common_denominator(other)
        return RationalTime(a + b, lcd)._simplified()

    def __sub__(self, other: RationalTime) -> RationalTime:
        a, b, lcd = self._common_denominator(other)
        return RationalTime(a - b, lcd)._simplified()

    def __mul__(self, factor: float) -> RationalTime:
        if isinstance(factor, int):
            return RationalTime(self.numerator * factor, self.denominator)._simplified()
        return RationalTime.from_seconds(self.to_seconds() * factor, self.denominator)

    def __truediv__(self, other: RationalTime | float) -> RationalTime | float:
        if isinstance(other, RationalTime):
            # Returns a float ratio
            return (self.numerator * other.denominator) / (self.denominator * other.numerator)
        if isinstance(other, int):
            return RationalTime(self.numerator, self.denominator * other)._simplified()
        return RationalTime.from_seconds(self.to_seconds() / other, self.denominator)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RationalTime):
            return NotImplemented
        a, b, _ = self._common_denominator(other)
        return a == b

    def __lt__(self, other: RationalTime) -> bool:
        a, b, _ = self._common_denominator(other)
        return a < b

    def __le__(self, other: RationalTime) -> bool:
        a, b, _ = self._common_denominator(other)
        return a <= b

    def __gt__(self, other: RationalTime) -> bool:
        a, b, _ = self._common_denominator(other)
        return a > b

    def __ge__(self, other: RationalTime) -> bool:
        a, b, _ = self._common_denominator(other)
        return a >= b

    def __repr__(self) -> str:
        return f"RationalTime({self.numerator}/{self.denominator} = {self.to_seconds():.4f}s)"

    def _simplified(self) -> RationalTime:
        """Reduce fraction to lowest terms."""
        if self.numerator == 0:
            return RationalTime(0, self.denominator)
        g = math.gcd(abs(self.numerator), self.denominator)
        return RationalTime(self.numerator // g, self.denominator // g)

    @property
    def is_zero(self) -> bool:
        return self.numerator == 0


# Common frame durations
FRAME_DURATIONS = {
    23.976: RationalTime(1001, 24000),
    24: RationalTime(100, 2400),
    25: RationalTime(100, 2500),
    29.97: RationalTime(1001, 30000),
    30: RationalTime(100, 3000),
    50: RationalTime(100, 5000),
    59.94: RationalTime(1001, 60000),
    60: RationalTime(100, 6000),
}

# Common format names → frame durations
FORMAT_FRAME_DURATIONS = {
    "FFVideoFormat1080p2398": RationalTime(1001, 24000),
    "FFVideoFormat1080p24": RationalTime(100, 2400),
    "FFVideoFormat1080p25": RationalTime(100, 2500),
    "FFVideoFormat1080p2997": RationalTime(1001, 30000),
    "FFVideoFormat1080p30": RationalTime(100, 3000),
    "FFVideoFormat1080p50": RationalTime(100, 5000),
    "FFVideoFormat1080p5994": RationalTime(1001, 60000),
    "FFVideoFormat1080p60": RationalTime(100, 6000),
    "FFVideoFormat4Kp2398": RationalTime(1001, 24000),
    "FFVideoFormat4Kp24": RationalTime(100, 2400),
    "FFVideoFormat4Kp25": RationalTime(100, 2500),
    "FFVideoFormat4Kp2997": RationalTime(1001, 30000),
    "FFVideoFormat4Kp30": RationalTime(100, 3000),
    "FFVideoFormat4Kp50": RationalTime(100, 5000),
    "FFVideoFormat4Kp5994": RationalTime(1001, 60000),
    "FFVideoFormat4Kp60": RationalTime(100, 6000),
}


def _lcm(a: int, b: int) -> int:
    """Least common multiple."""
    return abs(a * b) // math.gcd(a, b)


def fps_from_frame_duration(frame_duration: RationalTime) -> float:
    """Calculate FPS from frame duration."""
    return frame_duration.denominator / frame_duration.numerator


def frame_duration_from_fps(fps: float) -> RationalTime:
    """Get standard frame duration for a given FPS."""
    if fps in FRAME_DURATIONS:
        return FRAME_DURATIONS[fps]
    # Fallback: construct from fps
    return RationalTime(1000, round(fps * 1000))
