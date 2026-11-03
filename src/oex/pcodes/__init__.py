"""P-code tagging via fieldmaps.io edge-matched humanitarian admin polygons."""

from typing import Any

from oex.config.schema import PcodesSourceConfig
from oex.pcodes.cache import (
    PcodeCacheEntry,
    PcodeCacheError,
    ensure_admin_parquets,
)
from oex.pcodes.tagger import PcodeTagReport, tag_table, write_country_admin_parquets


def resolve_pcodes_config(source: dict[str, Any]) -> PcodesSourceConfig:
    raw = source.get("pcodes")
    if raw is None:
        return PcodesSourceConfig()
    if isinstance(raw, PcodesSourceConfig):
        return raw
    if isinstance(raw, dict):
        return PcodesSourceConfig(**raw)
    raise TypeError(
        f"source.pcodes has unexpected type {type(raw).__name__}; "
        "expected PcodesSourceConfig or dict"
    )


__all__ = [
    "PcodeCacheEntry",
    "PcodeCacheError",
    "PcodeTagReport",
    "ensure_admin_parquets",
    "resolve_pcodes_config",
    "tag_table",
    "write_country_admin_parquets",
]
