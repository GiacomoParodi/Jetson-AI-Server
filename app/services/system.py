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


_HWMON_ROOT = Path("/sys/class/hwmon")


def _power_rails() -> dict[str, float]:
    """Potenza (W) per ogni linea di alimentazione misurata dai sensori INA3221 del Jetson."""
    rails: dict[str, float] = {}
    for hw in _HWMON_ROOT.glob("hwmon*"):
        try:
            if (hw / "name").read_text().strip() != "ina3221":
                continue
        except OSError:
            continue
        for volt in hw.glob("in*_input"):
            n = volt.name[2:].split("_")[0]
            try:
                mv = int(volt.read_text().strip())
                ma = int((hw / f"curr{n}_input").read_text().strip())
            except (OSError, ValueError):
                continue
            try:
                label = (hw / f"in{n}_label").read_text().strip()
            except OSError:
                label = f"canale {n}"
            if mv > 0 and ma >= 0:
                rails[label] = round(mv * ma / 1e6, 2)
    return rails


def _fan_percent() -> float | None:
    for hw in _HWMON_ROOT.glob("hwmon*"):
        try:
            if "fan" not in (hw / "name").read_text().strip():
                continue
            return round(int((hw / "pwm1").read_text().strip()) / 255 * 100)
        except (OSError, ValueError):
            continue
    return None


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
        "power_rails": (rails := _power_rails()),
        # VDD_IN è l'ingresso della scheda: il consumo totale del modulo.
        "power_w": rails.get("VDD_IN") if "VDD_IN" in rails else (round(sum(rails.values()), 2) if rails else None),
        "fan_percent": _fan_percent(),
    }
