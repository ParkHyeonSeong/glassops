"""Collects NVIDIA GPU metrics via pynvml. Gracefully returns empty if unavailable."""

import logging
from typing import Optional

logger = logging.getLogger("glassops.agent")

_nvml_initialized = False
_driver_version: str = ""


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

            # GPU processes (compute + graphics)
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
                            })
                except Exception:
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
