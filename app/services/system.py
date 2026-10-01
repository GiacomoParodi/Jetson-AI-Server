"""Stato del dispositivo: CPU, RAM, disco, GPU e temperature (Jetson se disponibile)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psutil

from ..config import settings

_GPU_LOAD_FILES = [
    Path("/sys/devices/platform/gpu.0/load"),
    Path("/sys/devices/gpu.0/load"),
    Path("/sys/devices/platform/17000000.gpu/load"),
]


def _gpu_load() -> float | None:
    for f in _GPU_LOAD_FILES:
        try:
            return int(f.read_text().strip()) / 10  # il valore è in decimi di %
        except (OSError, ValueError):
            continue
    return None


def _temperatures() -> dict[str, float]:
    temps: dict[str, float] = {}
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            name = (zone / "type").read_text().strip()
            value = int((zone / "temp").read_text().strip()) / 1000
        except (OSError, ValueError):
            continue
        if value > 0:
            temps[name] = round(value, 1)
    return temps


def _device_model() -> str | None:
    try:
        return Path("/proc/device-tree/model").read_text().strip("\x00\n ")
    except OSError:
        return None


def status() -> dict[str, Any]:
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(str(settings.data_dir))
    return {
        "device": _device_model(),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_used_gb": round((mem.total - mem.available) / 1e9, 2),
        "ram_total_gb": round(mem.total / 1e9, 2),
        "disk_free_gb": round(disk.free / 1e9, 1),
        "disk_total_gb": round(disk.total / 1e9, 1),
        "gpu_percent": _gpu_load(),
        "temperatures": _temperatures(),
    }
