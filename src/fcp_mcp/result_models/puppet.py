"""Typed result contracts for puppet inspection tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class PuppetPresetParameterRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    default: int | float


class PuppetPresetRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    parts: list[str]
    parameters: list[PuppetPresetParameterRecord]
    usage: str


class PuppetPresetListResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    presets: list[PuppetPresetRecord]
