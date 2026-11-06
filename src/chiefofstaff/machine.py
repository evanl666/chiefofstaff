"""How much room this computer has left.

Copilot sessions themselves are light: an open, idle one takes about half a megabyte in the
Copilot CLI, and one that has worked about ten (measured on macOS). What weighs is what workers
run (test suites, builds, browsers) and the MCP servers each working session starts for itself.
So new worker turns wait while memory is low, and quiet workers let go of their Copilot session
(Workers.release_idle), which frees their MCP servers until their next message.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import time
from pathlib import Path

# New worker turns wait while less than this share of memory (percent) is available.
MIN_FREE_PERCENT = float(os.environ.get("CHIEFOFSTAFF_MIN_FREE_MEMORY", "10"))
CACHE_SECONDS = 5.0
_cached: tuple[float, float | None] = (0.0, None)


def free_memory() -> float | None:
    """The share of memory still available (percent), as the system reckons it: on macOS the
    kernel's memory status level (Activity Monitor's memory pressure), on Linux MemAvailable.
    None where it cannot be read."""
    if sys.platform == "darwin":
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"))
            value, size = ctypes.c_uint32(0), ctypes.c_size_t(4)
            name = b"kern.memorystatus_level"
            if libc.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0) == 0:
                return float(value.value)
        except (OSError, AttributeError):
            return None
        return None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            fields = {
                line.split(":")[0]: int(line.split()[1])
                for line in meminfo.read_text().splitlines()
                if line.split()[1:2] and line.split()[1].isdigit()
            }
            return 100 * fields["MemAvailable"] / fields["MemTotal"]
        except (OSError, KeyError, ZeroDivisionError, ValueError):
            return None
    return None


def short_of_memory() -> float | None:
    """The share of memory available when it is too low to start more work, else None."""
    global _cached
    at, free = _cached
    if time.monotonic() - at > CACHE_SECONDS:
        free = free_memory()
        _cached = (time.monotonic(), free)
    return free if free is not None and free < MIN_FREE_PERCENT else None
