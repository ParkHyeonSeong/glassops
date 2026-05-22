"""GlassOps Agent — lightweight daemon for collecting and pushing server metrics."""

import asyncio
import json
import logging
import signal
import time as _time

from agent.config import AGENT_ID, COLLECT_INTERVAL, ENABLE_GPU, ENABLE_DOCKER
from agent.collectors.system import collect_all as collect_system
from agent.collectors.gpu import collect_gpu, shutdown_nvml
from agent.collectors.docker_collector import collect_containers
from agent.collectors.network import collect_network
from agent.collectors.process import collect_processes
from agent.collectors import cgroup_stats
from agent.transport.ws_client import MetricsPusher, serve_rpc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("glassops.agent")


def _attach_gpu_to_containers(containers: list[dict], gpus: list[dict]) -> None:
    """Sum GPU VRAM per container by mapping each GPU process pid to its cgroup.

    Containers that own no GPU process get no `gpu` field — keeps the payload
    smaller and lets the UI decide whether to render the GPU chart.
    """
    agg: dict[str, dict] = {}
    seen_pids: set[int] = set()
    for gpu in gpus:
        gpu_idx = gpu.get("index", 0)
        for proc in gpu.get("processes") or []:
            pid = proc.get("pid")
            vram = proc.get("vram_bytes", 0)
            if not isinstance(pid, int) or pid <= 0:
                continue
            seen_pids.add(pid)
            if not isinstance(vram, int) or vram < 0:
                continue
            sid = cgroup_stats.container_id_for_pid(pid)
            if sid is None:
                continue
            entry = agg.setdefault(sid, {"vram_bytes": 0, "processes": []})
            entry["vram_bytes"] += vram
            entry["processes"].append({"pid": pid, "vram_bytes": vram, "gpu_index": gpu_idx})

    cgroup_stats.gc_pid_cache(seen_pids)
    for c in containers:
        sid = c.get("id")
        if sid and sid in agg:
            c["gpu"] = agg[sid]


async def collect_metrics() -> dict:
    """Gather all enabled metrics."""
    metrics = collect_system()

    gpu_data = None
    if ENABLE_GPU:
        gpu_data = collect_gpu()
        if gpu_data is not None:
            metrics["gpu"] = gpu_data

    if ENABLE_DOCKER:
        containers = collect_containers()
        if containers is not None:
            if gpu_data:
                _attach_gpu_to_containers(containers, gpu_data)
            metrics["containers"] = containers

    metrics["network"] = collect_network()
    metrics["processes"] = collect_processes()
    metrics["agent_id"] = AGENT_ID
    metrics["timestamp"] = _time.time()
    return metrics


async def main() -> None:
    logger.info("GlassOps Agent starting (id=%s, interval=%ds)", AGENT_ID, COLLECT_INTERVAL)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    pusher = MetricsPusher()
    rpc_task = asyncio.create_task(serve_rpc(pusher, stop))

    # Initial CPU percent call to prime psutil (first call always returns 0)
    import psutil
    psutil.cpu_percent(interval=0)

    while not stop.is_set():
        # Ensure connection
        if not pusher.connected:
            try:
                await asyncio.wait_for(pusher.connect(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("Connect timeout, will retry next cycle...")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=COLLECT_INTERVAL)
                except asyncio.TimeoutError:
                    pass
                continue

        # Collect & push
        try:
            metrics = await collect_metrics()
            success = await pusher.send(metrics)
            if not success:
                continue
            logger.debug("Pushed metrics: %s", json.dumps(metrics)[:200])
        except Exception:
            logger.exception("Error collecting/pushing metrics")

        # Wait for next cycle or stop signal
        try:
            await asyncio.wait_for(stop.wait(), timeout=COLLECT_INTERVAL)
        except asyncio.TimeoutError:
            pass

    rpc_task.cancel()
    try:
        await rpc_task
    except asyncio.CancelledError:
        pass
    await pusher.close()
    shutdown_nvml()
    logger.info("Agent stopped.")


if __name__ == "__main__":
    asyncio.run(main())
