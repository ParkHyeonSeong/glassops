"""Collects NVIDIA GPU metrics via pynvml. Gracefully returns empty if unavailable."""

from typing import Optional

_nvml_initialized = False


def _ensure_nvml() -> bool:
    global _nvml_initialized
    if _nvml_initialized:
        return True
    try:
        import pynvml  # type: ignore[import-untyped]
        pynvml.nvmlInit()
        _nvml_initialized = True
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
            temp = pynvml.nvmlDeviceGetTemperature(
                handle, pynvml.NVML_TEMPERATURE_GPU
            )
            try:
                power = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000
            except pynvml.NVMLError:
                power = 0

            gpus.append(
                {
                    "index": i,
                    "name": pynvml.nvmlDeviceGetName(handle),
                    "gpu_util": util.gpu,
                    "mem_util": util.memory,
                    "mem_total": mem.total,
                    "mem_used": mem.used,
                    "temperature": temp,
                    "power_watts": power,
                }
            )

        return gpus if gpus else None
    except Exception:
        return None
