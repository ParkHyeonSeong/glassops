export type TimeRange = "live" | "5m" | "1h" | "6h" | "24h" | "7d";

export interface SampleOrigin {
  sample_id?: string;
  arrival_seq?: number;
  persisted?: boolean;
  /** Ephemeral only: the last row id the backend had assigned when this
      sample failed to store — a server-issued arrival anchor. The frontend
      never guesses this from its own buffer (it may be empty mid-fetch). */
  after_seq?: number;
}

export interface ContainerSample extends SampleOrigin {
  t: number;
  cpu: number;
  mem: number;
  mem_limit: number;
  vram: number;
  gpu_util: number;
  gpu_present: boolean;
}

const EPHEMERAL_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * Validated identity metadata from an untrusted wire object. A pre-identity
 * backend relays agent dicts verbatim, so every field here is forgeable in
 * that rolling combination: a raw identity must be internally consistent
 * ("raw:<n>" with n === arrival_seq, positive safe integer, persisted
 * true/absent, no stray after_seq) and an ephemeral one fully formed
 * (8-4-4-4-12 hex UUID suffix, no seq, persisted false, non-negative
 * safe-integer after_seq). ANY violation drops the whole metadata set —
 * the sample degrades to legacy semantics instead of poisoning dedup or
 * arrival ordering.
 */
export function sampleOrigin(raw: {
  sample_id?: unknown;
  arrival_seq?: unknown;
  persisted?: unknown;
  after_seq?: unknown;
}): SampleOrigin {
  const { sample_id, arrival_seq, persisted, after_seq } = raw;
  if (typeof sample_id !== "string") return {};
  if (
    typeof arrival_seq === "number" &&
    Number.isSafeInteger(arrival_seq) &&
    arrival_seq > 0 &&
    sample_id === `raw:${arrival_seq}` &&
    (persisted === undefined || persisted === true) &&
    after_seq === undefined
  ) {
    return { sample_id, arrival_seq, persisted: true };
  }
  if (
    sample_id.startsWith("ephemeral:") &&
    EPHEMERAL_UUID.test(sample_id.slice("ephemeral:".length)) &&
    arrival_seq === undefined &&
    persisted === false &&
    typeof after_seq === "number" &&
    Number.isSafeInteger(after_seq) &&
    after_seq >= 0
  ) {
    return { sample_id, persisted: false, after_seq };
  }
  return {};
}

/**
 * Dedup identity. Distinct prefixes ("id:" vs "t:") keep the server-issued
 * namespace and the legacy raw-t namespace collision-free by construction.
 */
export function sampleIdentity(sample: ContainerSample): string {
  return sample.sample_id !== undefined ? `id:${sample.sample_id}` : `t:${sample.t}`;
}

export const RANGE_WINDOW_SEC: Record<Exclude<TimeRange, "live">, number> = {
  "5m": 300,
  "1h": 3600,
  "6h": 21600,
  "24h": 86400,
  "7d": 604800,
};
const LIVE_MAX_SAMPLES = 120;
const RANGED_MAX_SAMPLES = 600;

export function containerMetricsKey(
  agentId: string,
  containerName: string,
  range: TimeRange,
): string {
  return `${agentId}\u0000${containerName}\u0000${range}`;
}

/** Total arrival order for identity-bearing samples: durable samples rank
    by backend id, an ephemeral ranks right after the id it followed —
    (after_seq, 1) beats (after_seq, 0) and loses to (after_seq + 1, 0).
    Legacy samples (no identity) have no rank. The single source of arrival
    truth: mergeSamplesByIdentity orders by it, boundContainerSamples
    re-seats ranked samples by it before capping (array position is not
    reliable — REST and WS race), and same-X collapse compares by it. */
function arrivalRank(sample: ContainerSample): readonly [number, number] | undefined {
  if (sample.arrival_seq !== undefined) return [sample.arrival_seq, 0];
  if (sample.after_seq !== undefined) return [sample.after_seq, 1];
  return undefined;
}

function compareArrival(a: ContainerSample, b: ContainerSample): number {
  const [seqA, kindA] = arrivalRank(a) as readonly [number, number];
  const [seqB, kindB] = arrivalRank(b) as readonly [number, number];
  return seqA !== seqB ? seqA - seqB : kindA - kindB;
}

/**
 * Identity-dedup merge. A fetched sample wins over an existing one with the
 * same sample_id (same DB row — values identical, fetched authoritative).
 * An existing sample WITHOUT identity keeps the legacy rule: dropped when
 * any fetched sample shares its raw t. Output order: identity-less fetched
 * history (downsampled/legacy, fetch order) first, then every
 * identity-bearing sample — durable and ephemeral together — sorted by
 * arrival rank (stable, so equal-rank ephemerals keep session reception
 * order), then identity-less survivors (upgrade-transition legacy live).
 * With no metadata anywhere the legacy fetched-first pseudo-order is
 * preserved verbatim.
 */
export function mergeSamplesByIdentity(
  fetched: ContainerSample[],
  existing: ContainerSample[],
): ContainerSample[] {
  const fetchedIds = new Set(
    fetched.flatMap((s) => (s.sample_id !== undefined ? [s.sample_id] : [])),
  );
  const fetchedTimestamps = new Set(fetched.map((s) => s.t));
  const survivors = existing.filter((s) =>
    s.sample_id !== undefined
      ? !fetchedIds.has(s.sample_id)
      : !fetchedTimestamps.has(s.t),
  );
  const combined = [...fetched, ...survivors];
  if (!combined.some((s) => arrivalRank(s) !== undefined)) return combined;

  const ranked = combined
    .filter((s) => arrivalRank(s) !== undefined)
    .sort(compareArrival);
  const fetchedLegacy = fetched.filter((s) => arrivalRank(s) === undefined);
  const survivorLegacy = survivors.filter((s) => arrivalRank(s) === undefined);
  return [...fetchedLegacy, ...ranked, ...survivorLegacy];
}

export function effectiveSampleTime(t: number, serverNow: number): number {
  return Math.min(t, serverNow);
}

/**
 * Storage bound: first-wins dedup on sample identity (server id when
 * present, legacy raw t otherwise), then cap by arrival.
 *
 * Identity-bearing samples are re-ordered among themselves by arrival rank
 * before the cap: array position is NOT a reliable arrival record, because
 * REST and WS are independent channels — a fetch can deliver a higher seq
 * (raw:11, a current timestamp inside the window) before a delayed WS
 * message delivers a lower one (raw:10, a future timestamp the fetch's
 * range excluded), which lands at the tail. Capping by raw position there
 * would evict the NEWER raw:11 and keep the stale raw:10 (r3.6 #2).
 * Legacy samples (no identity) keep their positions, so a metadata-free
 * buffer is returned untouched and the pre-identity contract is preserved.
 * Time-window membership is a render concern (constrainContainerSamples).
 */
export function boundContainerSamples(
  samples: ContainerSample[],
  range: TimeRange,
): ContainerSample[] {
  const seen = new Set<string>();
  const deduped: ContainerSample[] = [];
  for (const sample of samples) {
    const key = sampleIdentity(sample);
    if (seen.has(key)) continue;
    seen.add(key);
    deduped.push(sample);
  }
  // Re-seat ranked samples into their own slots in rank order; legacy
  // samples stay exactly where they are (mixed Mode B keeps its layout).
  const ranked = deduped.filter((sample) => arrivalRank(sample) !== undefined);
  if (ranked.length > 1) {
    const ordered = [...ranked].sort(compareArrival);
    let next = 0;
    for (let i = 0; i < deduped.length; i++) {
      if (arrivalRank(deduped[i]) !== undefined) deduped[i] = ordered[next++];
    }
  }
  const cap = range === "live" ? LIVE_MAX_SAMPLES : RANGED_MAX_SAMPLES;
  return deduped.slice(-cap);
}

/**
 * Display window: membership uses effective time (min(t, serverNow)); the
 * raw canonical t stays untouched. Output preserves the storage pseudo-order
 * (see mergeSamplesByIdentity) so the chart layer can pick latest-arrival
 * representatives — drawing order is the chart layer's concern. Ingest allows
 * up to +300s of future skew as canonical data, so the window is defined in
 * canonical time and may hold up to W+300s of reception-basis data (spec r6,
 * explicit choice).
 */
export function constrainContainerSamples(
  samples: ContainerSample[],
  range: TimeRange,
  serverNow: number,
): ContainerSample[] {
  if (range === "live") return samples.slice(-LIVE_MAX_SAMPLES);

  const cutoff = serverNow - RANGE_WINDOW_SEC[range];
  return samples
    .filter((sample) => effectiveSampleTime(sample.t, serverNow) >= cutoff)
    .slice(-RANGED_MAX_SAMPLES);
}

/**
 * Chart-only view: samples whose effective time collapses to the same X
 * position are represented by the latest ARRIVAL. When both candidates
 * carry an arrival rank ((seq, 0) durable / (after_seq, 1) ephemeral) the
 * greater rank wins explicitly — input order is irrelevant; when either
 * side is a legacy sample the later input wins, preserving the
 * pre-identity contract. Under a clock rollback the largest raw t is an
 * OLDER reading, not the newest — hence arrival, never t.
 * Recharts' axis tooltip picks the first payload matching a label, so
 * overlaps must collapse to a single point. Output is sorted by effective
 * time (ascending X) for path drawing. Statistics must keep using the
 * full window data, not this view.
 */
export function collapseByEffectiveTime(
  samples: ContainerSample[],
  serverNow: number,
): ContainerSample[] {
  const byEffectiveTime = new Map<number, ContainerSample>();
  for (const sample of samples) {
    const x = effectiveSampleTime(sample.t, serverNow);
    const current = byEffectiveTime.get(x);
    if (current !== undefined) {
      const currentRank = arrivalRank(current);
      const sampleRank = arrivalRank(sample);
      if (
        currentRank !== undefined &&
        sampleRank !== undefined &&
        compareArrival(sample, current) < 0
      ) {
        continue;
      }
    }
    byEffectiveTime.set(x, sample);
  }
  return [...byEffectiveTime.values()].sort(
    (left, right) =>
      effectiveSampleTime(left.t, serverNow) - effectiveSampleTime(right.t, serverNow),
  );
}
