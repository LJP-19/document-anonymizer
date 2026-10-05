"""Detect the local machine's RAM and recommend a catalog entry.

RAM is the deciding factor, not CPU core count or disk space. Core count
affects how FAST inference feels, not whether a model can be loaded at
all - using it as the primary recommendation signal would risk
recommending a model that technically starts but constantly swaps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .catalog import CATALOG, LlmChoice

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HardwareInfo:
    total_ram_gb: float
    logical_cpu_count: int
    #: None if detection itself failed - a genuinely offline or sandboxed
    #: environment should not crash the picker, just fall back to the
    #: smallest, safest recommendation.
    detected: bool


def detect_hardware() -> HardwareInfo:
    try:
        import psutil

        total_ram_gb = psutil.virtual_memory().total / 1e9
        cpu_count = psutil.cpu_count(logical=True) or 1
        return HardwareInfo(total_ram_gb=total_ram_gb, logical_cpu_count=cpu_count, detected=True)
    except Exception:  # noqa: BLE001 - hardware detection must never be fatal
        log.warning("hardware detection failed; falling back to the smallest recommendation", exc_info=True)
        return HardwareInfo(total_ram_gb=0.0, logical_cpu_count=1, detected=False)


def recommend(hardware: Optional[HardwareInfo] = None) -> LlmChoice:
    """The largest catalog entry whose min_ram_gb the machine's real RAM
    comfortably covers, defaulting to the smallest entry if detection
    failed or the machine falls short of even that - never recommend a
    choice that likely will not load."""
    hw = hardware or detect_hardware()
    if not hw.detected:
        return CATALOG[0]
    eligible = [c for c in CATALOG if hw.total_ram_gb >= c.min_ram_gb]
    if not eligible:
        return CATALOG[0]
    return max(eligible, key=lambda c: c.min_ram_gb)
