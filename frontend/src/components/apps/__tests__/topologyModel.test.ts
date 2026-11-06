import { describe, expect, it } from "vitest";
import { DEFAULT_THRESHOLDS } from "../../../lib/thresholds";
import type { ContainerInfo, MetricSnapshot } from "../../../stores/metricsStore";
import { makeContainer } from "../../../test/fixtures";
import {
  buildTopology,
  cpusetCount,
  formatAge,
  formatPair,
  parseDockerTime,
  pm2Owner,
  type TopologyGraph,
} from "../topologyModel";
import { GiB, MiB, NOW, base, full, gpu, withProcessOnGpu } from "./topologyFixtures";

const build = (s: MetricSnapshot): TopologyGraph => buildTopology(s, NOW, DEFAULT_THRESHOLDS);
const byKey = (g: TopologyGraph) => Object.fromEntries(g.nodes.map((n) => [n.key, n]));

describe("topologyModel", () => {
  it("labels hardware icons", () => {
    const n = byKey(build(base()));
    expect(n["hw:cpu"]).toMatchObject({ sub: "128 cores", badge: { text: "23%", level: "ok" } });
    expect(n["hw:mem"].sub).toBe("206/503 GB");
  });

  it("formats pairs and ages", () => {
    expect(formatPair(1.8 * GiB, 4 * GiB)).toBe("1.8/4.0 GB");
    expect(formatPair(512 * MiB, 2 * GiB)).toBe("0.5/2.0 GB");
    expect(formatAge((2 * 60 + 59) * 60_000)).toBe("2h");
    expect(formatAge(3 * 86_400_000)).toBe("3d");
  });

  it("groups containers by compose project and orders groups", () => {
    expect(build(full()).groups.map((g) => g.id)).toEqual(
      ["hw", "gpu", "compose:llm", "compose:shop", "solo", "pm2:/home/hspark/.pm2", "other"]);
  });

  it("draws limit lines only for running containers with limits", () => {
    const e = build(full()).edges;
    expect(e).toContainEqual({ from: "ct:api", to: "hw:cpu", kind: "cpu", label: "max 2 cores" });
    expect(e).toContainEqual({ from: "ct:api", to: "hw:mem", kind: "mem", label: "max 4.0 GB · reserved 2.0 GB" });
    expect(e).toContainEqual({ from: "ct:train", to: "hw:cpu", kind: "cpu", label: "max 16 cores · pinned 0-15" });
    expect(e.filter((x) => x.from === "ct:batch")).toEqual([]);
  });

  it("separates GPU use from idle reservation", () => {
    const g = build(full());
    expect(g.edges).toContainEqual({ from: "ct:vllm", to: "gpu:GPU-0", kind: "gpu", label: "uses 38.2 GB" });
    expect(g.edges).toContainEqual({ from: "ct:jupyter", to: "gpu:GPU-1", kind: "gpuReserved", label: "reserved · idle" });
    expect(g.edges.filter((x) => x.from === "ct:vllm" && x.kind === "gpuReserved")).toEqual([]);
    expect(byKey(g)["gpu:GPU-1"].sub).toBe("A6000 · reserved");
  });

  it("skips edges to GPUs missing from the snapshot", () => {
    expect(() => build(withProcessOnGpu(7))).not.toThrow();
    expect(build(withProcessOnGpu(7)).edges.filter((x) => x.kind === "gpu")).toEqual([]);
    expect(build(withProcessOnGpu(0)).edges.filter((x) => x.kind === "gpu")).toHaveLength(1);
  });

  it("puts a PM2 app's GPU use on the PM2 icon", () => {
    const g = build(full());
    expect(g.edges).toContainEqual(expect.objectContaining({ from: "pm2:/home/hspark/.pm2:admin-api", to: "gpu:GPU-0", kind: "gpu" }));
    expect(byKey(g)["proc:900"]).toMatchObject({ groupId: "other", sub: "hspark · 18.4 GB", persist: false });
    expect(byKey(g)["proc:200"]).toBeUndefined();
  });

  it("draws one line per PM2 app and GPU even with several processes", () => {
    const s = full();
    s.pm2_apps = [{ name: "admin-api", home: "/home/hspark/.pm2", instances: 2, pids: [200, 201], cpu_percent: 1, mem_bytes: 1024 }];
    s.gpu![0].processes.push({ pid: 201, vram_bytes: 2 * GiB, name: "node", user: "hspark" });
    const g = build(s);
    const pm2Edges = g.edges.filter((x) => x.from.startsWith("pm2:"));
    expect(pm2Edges).toEqual([
      { from: "pm2:/home/hspark/.pm2:admin-api", to: "gpu:GPU-0", kind: "gpu", label: "uses 3.0 GB" },
    ]);
    expect(byKey(g)["pm2:/home/hspark/.pm2:admin-api"].name).toBe("admin-api ×2");
  });

  it("marks older agents and draws no limit lines for them", () => {
    const old = build(full({ stripNewFields: true }));
    expect(old.outdatedAgent).toBe(true);
    expect(old.edges.filter((x) => x.kind !== "gpu")).toEqual([]);
    expect(build(full()).outdatedAgent).toBe(false);
    expect(build(base({ containers: undefined, gpu: undefined, pm2_apps: [] })).groups.map((x) => x.id)).toEqual(["hw"]);
  });

  it("ages stopped containers from Docker timestamps", () => {
    expect(parseDockerTime("2026-11-05T09:00:00.123456789Z")).toBe(Date.UTC(2026, 10, 5, 9, 0, 0, 123));
    expect(parseDockerTime("2026-11-05T11:00:00+02:00")).toBe(Date.UTC(2026, 10, 5, 9, 0, 0, 0));
    expect(parseDockerTime("")).toBeNull();
    expect(parseDockerTime(undefined)).toBeNull();
    expect(parseDockerTime("garbage")).toBeNull();
    const n = byKey(build(full()));
    expect(n["ct:batch"].sub).toBe("exit 0 · 3h ago");
    expect(n["ct:idle"].sub).toBe("created");
  });

  it("derives the PM2 owner from its home", () => {
    expect(pm2Owner("/home/hspark/.pm2")).toBe("hspark");
    expect(pm2Owner("/root/.pm2")).toBe("root");
    expect(pm2Owner("/srv/pm2")).toBe("/srv/pm2");
  });

  it("summarises CPU allocation and the host", () => {
    const g = build(full());
    expect(byKey(g)["hw:cpu"].tip.rows).toEqual(expect.arrayContaining([
      { label: "Container CPU limits", value: "27 / 128 cores", tone: "cpu" },
      { label: "No limit", value: "jupyter · open-webui · redis" }]));
    expect(g.summary).toMatch(/^128 cores · .+ memory · .+ disk · 4 GPUs · \d+ containers \(\d+ running\) · \d+ outside containers$/);
  });

  it("counts a cpuset-pinned container as CPU-limited", () => {
    const s = full();
    const pinned = (name: string, cpuset: string) =>
      makeContainer({ name, id: name, limits: { cpus: null, cpuset, memory: 0, memory_reservation: 0 } });
    s.containers!.push(pinned("numa1", "32-63"), pinned("odd", "0-3:2"));
    const rows = byKey(build(s))["hw:cpu"].tip.rows;
    const row = (label: string) => rows.find((r) => r.label === label)?.value;
    expect(row("Container CPU limits")).toBe("59 / 128 cores"); // 27 + 32; a cpuset that does not parse adds nothing
    expect(row("Per container")).toBe("numa1 pinned 32-63 · train 16 · vllm 8 · api 2 · rabbitmq 1 · odd pinned 0-3:2");
    expect(row("No limit")).toBe("jupyter · open-webui · redis");
  });

  it("counts the CPUs in a cpuset", () => {
    expect(cpusetCount("0-3,8,10-11")).toBe(7);
    expect(cpusetCount("10-11,0-3,8")).toBe(7);
    expect(cpusetCount("32-63")).toBe(32);
    expect(cpusetCount("0-3,2-5")).toBe(6); // a CPU listed twice counts once
    for (const bad of ["3-1", "0-3:2", "1,,2", "a", "-1"]) expect(cpusetCount(bad), bad).toBeNull();
  });

  it("describes container RAM by what the agent knows", () => {
    const n = byKey(build(full()));
    expect(n["ct:api"].sub).toBe("RAM 1.8/4.0 GB");
    expect(n["ct:redis"].sub).toBe("RAM 256 B · ∞");
    const old = byKey(build(full({ stripNewFields: true })));
    expect(old["ct:api"].sub).toBe("RAM 1.8 GB");
    expect(old["ct:redis"].sub).toBe("RAM 256 B");
  });

  it("lists each GPU user with the VRAM it holds there", () => {
    const n = byKey(build(full()));
    const usedBy = (key: string) => n[key].tip.rows.find((r) => r.label === "Used by")?.value;
    expect(usedBy("gpu:GPU-0")).toBe("vllm 38.2 GB · admin-api 1.0 GB");
    expect(usedBy("gpu:GPU-3")).toBe("python 18.4 GB");
    expect(usedBy("gpu:GPU-1")).toBeUndefined();
  });

  it("puts the clock in the CPU tooltip, not on the tile", () => {
    const n = byKey(build(base()));
    expect(n["hw:cpu"].sub).toBe("128 cores");
    const rows = n["hw:cpu"].tip.rows;
    const i = rows.findIndex((r) => r.label === "Cores");
    expect(rows[i + 1]).toEqual({ label: "Clock", value: "2.9 GHz" });
    const none = byKey(build(base({ cpu: { ...base().cpu, freq_current: 0 } })));
    expect(none["hw:cpu"].tip.rows.some((r) => r.label === "Clock")).toBe(false);
  });

  it("writes network rates with one-letter units", () => {
    const net = (send: number, recv: number) =>
      byKey(build(base({ network: { ...base().network!, rates: { send_rate: send, recv_rate: recv } } })))["hw:net"];
    expect(net(2048, 1.2 * 1024 ** 2).sub).toBe("↑ 2.0K · ↓ 1.2M");
    expect(net(0, 512).sub).toBe("↑ 0B · ↓ 512B");
    expect(net(0.5, 3 * 1024 ** 3).sub).toBe("↑ 0B · ↓ 3.0G");
    expect(net(2048, 0).tip.rows[0].value).toBe("2.0 KB/s");
  });

  it("names a GPU by the first word of its model", () => {
    const names: [string, string][] = [
      ["NVIDIA A100-SXM4-80GB", "A100"], ["NVIDIA H100 80GB HBM3", "H100"], ["NVIDIA RTX A6000", "A6000"],
      ["NVIDIA GeForce RTX 4090", "4090"], ["Tesla V100-SXM2-32GB", "V100"], ["NVIDIA", "GPU"],
    ];
    for (const [name, word] of names) {
      const node = byKey(build(base({ gpu: [gpu(0, { name, mem_used: 21 * GiB, mem_total: 80 * GiB })] })))["gpu:GPU-0"];
      expect(node.sub).toBe(`${word} · 21/80 GB`);
      expect(node.tip.rows[0]).toEqual({ label: "Model", value: name });
    }
  });

  it("uses ∞ for a container without a memory limit and short sizes", () => {
    const ct = (name: string, o: Partial<ContainerInfo>) => makeContainer({ name, id: name, ...o });
    const limits = { cpus: null, cpuset: "", memory: 0, memory_reservation: 0 };
    const n = byKey(build(base({
      containers: [
        ct("a", { mem_usage: 512 * MiB, limits }),
        ct("b", { mem_usage: 41.3 * GiB, limits }),
        ct("c", { mem_usage: 1.8 * GiB }),
        ct("d", { mem_usage: 0, limits }),
      ],
    })));
    expect(n["ct:a"].sub).toBe("RAM 512 MB · ∞");
    expect(n["ct:b"].sub).toBe("RAM 41.3 GB · ∞");
    expect(n["ct:c"].sub).toBe("RAM 1.8 GB");
    expect(n["ct:d"].sub).toBe("RAM 0 B · ∞");
    expect(n["ct:a"].tip.rows).toEqual(expect.arrayContaining([{ label: "Memory limit", value: "no limit" }]));
  });

  it("writes a stopped container short on the tile and long in the tooltip", () => {
    const n = byKey(build(full()));
    const state = (k: string) => n[k].tip.rows.find((r) => r.label === "State")!.value;
    expect(n["ct:batch"].sub).toBe("exit 0 · 3h ago");
    expect(state("ct:batch")).toBe("exited (0) · 3h ago");
    expect(state("ct:idle")).toBe("created");
    expect(state("ct:api")).toMatch(/^running · /);
    const dead = byKey(build(base({
      containers: [makeContainer({ name: "d", id: "d", state: "dead", status: "Dead", finished_at: "2026-11-03T12:00:00Z", exit_code: 137 })],
    })))["ct:d"];
    expect(dead.sub).toBe("dead 137 · 2d ago");
    expect(dead.tip.rows.find((r) => r.label === "State")!.value).toBe("dead (137) · 2d ago");
  });

  it("shows PM2 memory without a trailing decimal", () => {
    const s = base({ pm2_apps: [{ name: "w", home: "/home/u/.pm2", instances: 1, pids: [1], cpu_percent: 0, mem_bytes: 210 * MiB }] });
    expect(byKey(build(s))["pm2:/home/u/.pm2:w"].sub).toBe("RAM 210 MB");
  });

  it("fits the tile", () => {
    const limits = { cpus: null, cpuset: "", memory: 0, memory_reservation: 0 };
    const lim = (m: number) => ({ ...limits, memory: m * GiB });
    const s = base({
      gpu: [21, 21, 31, 10].map((u, i) => gpu(i, { name: "NVIDIA A100-SXM4-80GB", mem_used: u * GiB, mem_total: 80 * GiB })),
      network: { ...base().network!, rates: { send_rate: 2048, recv_rate: 1.2 * 1024 ** 2 } },
      containers: [
        makeContainer({ name: "a", id: "a", mem_usage: 512 * MiB, limits }),
        makeContainer({ name: "b", id: "b", mem_usage: 41.3 * GiB, limits }),
        makeContainer({ name: "c", id: "c", mem_usage: 2.1 * GiB, limits: lim(8) }),
        makeContainer({ name: "d", id: "d", mem_usage: 21.4 * GiB, limits: lim(64) }),
        makeContainer({ name: "e", id: "e", state: "exited", status: "Exited", finished_at: "2026-11-04T19:00:00Z", exit_code: 0 }),
      ],
      pm2_apps: [{ name: "p", home: "/home/u/.pm2", instances: 1, pids: [1], cpu_percent: 0, mem_bytes: 210 * MiB }],
    });
    const g = build(s);
    expect(byKey(g)["ct:e"].sub).toBe("exit 0 · 17h ago");
    for (const n of g.nodes.filter((x) => x.kind !== "proc")) expect(n.sub.length, `${n.key}: ${n.sub}`).toBeLessThanOrEqual(17);
  });

  it("scores GPU badges", () => {
    const s = base({ gpu: [gpu(0, { gpu_util: 95, mem_used: 47 * GiB }), gpu(1)] });
    const n = byKey(build(s));
    expect(n["gpu:GPU-0"].badge).toEqual({ text: "95%", level: "warn" });
    expect(n["gpu:GPU-0"].meter?.level).toBe("warn");
    expect(n["gpu:GPU-0"].sub).toBe("A6000 · 47/48 GB");
    expect(n["gpu:GPU-1"].badge).toEqual({ text: "0%", level: "idle" });
  });

  it("shows process CPU and command on outside-container processes", () => {
    const n = byKey(build(full()));
    expect(n["proc:900"].badge).toEqual({ text: "98%", level: "ok" });
    expect(n["proc:900"].tip.rows).toEqual(expect.arrayContaining([
      { label: "Command", value: "python train.py --fold 3" },
      { label: "Usage", value: "CPU 98% · memory 1.8%" },
      { label: "GPU 3", value: "18.4 GB", tone: "gpu" }]));
    const s = full();
    s.processes = [];
    const unknown = byKey(build(s))["proc:900"];
    expect(unknown.badge).toBeUndefined();
    expect(unknown.tip.rows.some((r) => r.label === "Usage")).toBe(false);
  });

  it("gives stopped containers no badge", () => {
    const n = byKey(build(full()));
    expect(n["ct:batch"].badge).toBeUndefined();
    expect(n["ct:idle"].badge).toBeUndefined();
    expect(n["ct:api"].badge).toBeDefined();
  });
});
