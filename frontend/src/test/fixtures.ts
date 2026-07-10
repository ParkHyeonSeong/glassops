import type { ContainerInfo, MetricSnapshot } from "../stores/metricsStore";

export function makeContainer(overrides: Partial<ContainerInfo> = {}): ContainerInfo {
  return {
    id: "container-a",
    name: "worker",
    image: "glassops/test:latest",
    status: "running",
    state: "running",
    cpu_percent: 10,
    mem_usage: 256,
    mem_limit: 1024,
    ports: [],
    ...overrides,
  };
}

export function makeMetricSnapshot(overrides: Partial<MetricSnapshot> = {}): MetricSnapshot {
  return {
    cpu: {
      percent_total: 10,
      percent_per_core: [10],
      count_logical: 1,
      count_physical: 1,
      freq_current: 1000,
      freq_max: 2000,
    },
    memory: {
      total: 1024,
      available: 768,
      used: 256,
      percent: 25,
      swap_total: 0,
      swap_used: 0,
      swap_percent: 0,
    },
    disk: {
      total: 1024,
      used: 256,
      free: 768,
      percent: 25,
      read_bytes: 0,
      write_bytes: 0,
    },
    timestamp: 1,
    ...overrides,
  };
}

export function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
