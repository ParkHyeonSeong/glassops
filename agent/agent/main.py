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
from agent.transport.ws_client import MetricsPusher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("glassops.agent")


async def collect_metrics() -> dict:
    """Gather all enabled metrics."""
    metrics = collect_system()

    if ENABLE_GPU:
        gpu_data = collect_gpu()
        if gpu_data is not None:
            metrics["gpu"] = gpu_data

    if ENABLE_DOCKER:
        containers = collect_containers()
        if containers is not None:
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

    await pusher.close()
    shutdown_nvml()
    logger.info("Agent stopped.")


if __name__ == "__main__":
    asyncio.run(main())
