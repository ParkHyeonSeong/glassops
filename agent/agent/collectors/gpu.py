"""Collects NVIDIA GPU metrics via pynvml. Gracefully returns empty if unavailable."""

import logging
import time as _time
from typing import Optional

logger = logging.getLogger("glassops.agent")

_nvml_initialized = False
_driver_version: str = ""
# Per-process util samples carry a `lastSeenTimeStamp` cursor that NVML uses to
# return only newly-recorded windows; we advance it per device each cycle so we
# pick up fresh utilization data without re-receiving stale rows.
_last_util_ts: dict[int, int] = {}


def _ensure_nvml() -> bool:
    global _nvml_initialized, _driver_version
    if _nvml_initialized:
        return True
    try:
        import pynvml  # type: ignore[import-untyped]
        pynvml.nvmlInit()
        _nvml_initialized = True
        _driver_version = pynvml.nvmlSystemGetDriverVersion()
        return True
    except Exception:
        return False


def shutdown_nvml() -> None:
    global _nvml_initialized
    if _nvml_initialized:
        try:
            import pynvml  # type: ignore[import-untyped]
            pynvml.nvmlShutdown()
        except Exception:
            pass
        _nvml_initialized = False


def _safe_call(func, *args, default=0):
    """Call pynvml function, return default on error."""
    try:
        return func(*args)
    except Exception:
        return default


def collect_gpu() -> Optional[list[dict]]:
    if not _ensure_nvml():
        return None

    try:
        import pynvml  # type: ignore[import-untyped]

        device_count = pynvml.nvmlDeviceGetCount()
        gpus = []

        for i in range(device_count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)

            # Basic metrics
            temp = _safe_call(pynvml.nvmlDeviceGetTemperature, handle, pynvml.NVML_TEMPERATURE_GPU)
            power = _safe_call(pynvml.nvmlDeviceGetPowerUsage, handle) / 1000  # mW → W
            power_limit = _safe_call(pynvml.nvmlDeviceGetEnforcedPowerLimit, handle) / 1000

            # Clock speeds
            clock_sm = _safe_call(pynvml.nvmlDeviceGetClockInfo, handle, pynvml.NVML_CLOCK_SM)
            clock_mem = _safe_call(pynvml.nvmlDeviceGetClockInfo, handle, pynvml.NVML_CLOCK_MEM)

            # Fan speed
            fan_speed = _safe_call(pynvml.nvmlDeviceGetFanSpeed, handle)

            # UUID
            uuid = ""
            try:
                uuid = pynvml.nvmlDeviceGetUUID(handle)
            except Exception:
                pass

            # GPU processes (compute + graphics) — VRAM ownership.
            processes = []
            seen_pids: set[int] = set()
            for getter in (pynvml.nvmlDeviceGetComputeRunningProcesses, pynvml.nvmlDeviceGetGraphicsRunningProcesses):
                try:
                    for proc in getter(handle):
                        if proc.pid not in seen_pids:
                            seen_pids.add(proc.pid)
                            vram = proc.usedGpuMemory
                            processes.append({
                                "pid": proc.pid,
                                "vram_bytes": vram if vram is not None else -1,
                                "sm_util": 0,
                            })
                except Exception:
                    pass

            # Per-process SM utilization (NVML keeps ~1s of sampled util per pid,
            # delivered incrementally past `lastSeenTimeStamp`). Sample window
            # spans the last ~1s; we pass a 1s-back cursor for the first call and
            # then store the highest timestamp returned so subsequent calls only
            # see fresh rows.
            try:
                cursor = _last_util_ts.get(i, int((_time.time() - 1) * 1_000_000))
                util_samples = pynvml.nvmlDeviceGetProcessUtilization(handle, cursor)
                by_pid: dict[int, dict] = {}
                max_ts = cursor
                for s in util_samples or []:
                    pid = getattr(s, "pid", 0)
                    if pid <= 0:
                        continue
                    ts = getattr(s, "timeStamp", 0)
                    if ts > max_ts:
                        max_ts = ts
                    existing = by_pid.get(pid)
                    # Keep the most recent sample per pid (highest timestamp).
                    if existing is None or ts >= existing.get("ts", 0):
                        by_pid[pid] = {
                            "ts": ts,
                            "sm": getattr(s, "smUtil", 0) or 0,
                        }
                _last_util_ts[i] = max_ts
                for proc in processes:
                    s = by_pid.get(proc["pid"])
                    if s is not None:
                        proc["sm_util"] = s["sm"]
            except Exception:
                # NVML returns NVML_ERROR_NOT_FOUND when no util data is available
                # yet — fine, just leave sm_util=0 on the processes.
                pass

            gpus.append({
                "index": i,
                "name": pynvml.nvmlDeviceGetName(handle),
                "uuid": uuid,
                "driver_version": _driver_version,
                "gpu_util": util.gpu,
                "mem_util": util.memory,
                "mem_total": mem.total,
                "mem_used": mem.used,
                "temperature": temp,
                "power_watts": power,
                "power_limit_watts": power_limit,
                "clock_sm_mhz": clock_sm,
                "clock_mem_mhz": clock_mem,
                "fan_speed": fan_speed,
                "processes": processes,
            })

        return gpus if gpus else None
    except Exception:
        logger.exception("Failed to collect GPU metrics")
        return None
