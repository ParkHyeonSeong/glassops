"""Collects CPU, Memory, and Disk metrics via psutil."""

import logging
import os

import psutil

# Use host /proc if mounted (for Docker container monitoring host)
_host_proc = os.environ.get("HOST_PROC")
if _host_proc:
    os.environ["PSUTIL_PROC"] = _host_proc

logger = logging.getLogger("glassops.agent")


def collect_cpu() -> dict:
    try:
        freq = psutil.cpu_freq()
        return {
            "percent_total": psutil.cpu_percent(interval=0),
            "percent_per_core": psutil.cpu_percent(interval=0, percpu=True),
            "count_logical": psutil.cpu_count(logical=True) or 0,
            "count_physical": psutil.cpu_count(logical=False) or 0,
            "freq_current": freq.current if freq else 0,
            "freq_max": freq.max if freq else 0,
        }
    except Exception:
        logger.exception("Failed to collect CPU metrics")
        return {
            "percent_total": 0, "percent_per_core": [],
            "count_logical": 0, "count_physical": 0,
            "freq_current": 0, "freq_max": 0,
        }


def collect_memory() -> dict:
    try:
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        return {
            "total": vm.total,
            "available": vm.available,
            "used": vm.used,
            "percent": vm.percent,
            "swap_total": sw.total,
            "swap_used": sw.used,
            "swap_percent": sw.percent,
        }
    except Exception:
        logger.exception("Failed to collect memory metrics")
        return {
            "total": 0, "available": 0, "used": 0, "percent": 0,
            "swap_total": 0, "swap_used": 0, "swap_percent": 0,
        }


def collect_disk() -> dict:
    try:
        usage = psutil.disk_usage("/")
        io = psutil.disk_io_counters()
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": usage.percent,
            "read_bytes": io.read_bytes if io else 0,
            "write_bytes": io.write_bytes if io else 0,
        }
    except Exception:
        logger.exception("Failed to collect disk metrics")
        return {
            "total": 0, "used": 0, "free": 0, "percent": 0,
            "read_bytes": 0, "write_bytes": 0,
        }


def collect_all() -> dict:
    return {
        "cpu": collect_cpu(),
        "memory": collect_memory(),
        "disk": collect_disk(),
    }
