from agent.collectors import docker_collector as dc
import agent.main as m

NONE = {"cpus": None, "cpuset": "", "memory": 0, "memory_reservation": 0}
GPUS = [{"index": 0, "uuid": "GPU-aaa"}, {"index": 1, "uuid": "GPU-bbb"}]

def _dev(**kw):
    return {"HostConfig": {"DeviceRequests": [dict({"Driver": "nvidia", "Capabilities": [["gpu"]]}, **kw)]}}

def test_limits_from_nano_cpus():
    attrs = {"HostConfig": {"NanoCpus": 2_000_000_000, "CpusetCpus": "0-15",
                            "Memory": 4294967296, "MemoryReservation": 2147483648}}
    assert dc.container_limits(attrs) == {"cpus": 2.0, "cpuset": "0-15",
                                          "memory": 4294967296, "memory_reservation": 2147483648}

def test_limits_from_quota_period():
    assert dc.container_limits({"HostConfig": {"CpuQuota": 150000, "CpuPeriod": 100000}})["cpus"] == 1.5
    assert dc.container_limits({"HostConfig": {"CpuQuota": 50000, "CpuPeriod": 25000}})["cpus"] == 2.0

def test_limits_from_quota_without_period():
    # `--cpu-quota` alone stores CpuPeriod 0: the kernel default period of 100 000 µs applies.
    assert dc.container_limits({"HostConfig": {"CpuQuota": 150000, "CpuPeriod": 0}})["cpus"] == 1.5
    assert dc.container_limits({"HostConfig": {"CpuQuota": 50000}})["cpus"] == 0.5
    both = {"NanoCpus": 2_000_000_000, "CpuQuota": 150000, "CpuPeriod": 0}
    assert dc.container_limits({"HostConfig": both})["cpus"] == 2.0     # NanoCpus still wins

def test_limits_unset_and_malformed():
    assert dc.container_limits({"HostConfig": {}}) == NONE
    assert dc.container_limits({"HostConfig": None}) == NONE
    assert dc.container_limits({"HostConfig": {"NanoCpus": "x", "Memory": 1024}}) == {**NONE, "memory": 1024}

def test_times_zero_time_becomes_empty():
    attrs = {"State": {"StartedAt": "2026-11-03T09:12:44.123456789Z",
                       "FinishedAt": "0001-01-01T00:00:00Z", "ExitCode": 0}}
    assert dc.container_times(attrs) == {"started_at": "2026-11-03T09:12:44.123456789Z",
                                         "finished_at": "", "exit_code": 0}
    assert dc.container_times({}) == {"started_at": "", "finished_at": "", "exit_code": None}

def test_gpu_request_ids_count_all():
    assert dc.gpu_request(_dev(DeviceIDs=["1", "GPU-abc"], Count=0)) == (False, ("1", "GPU-abc"))
    assert dc.gpu_request(_dev(DeviceIDs=None, Count=2)) == (False, ("0", "1"))
    assert dc.gpu_request(_dev(DeviceIDs=None, Count=-1)) == (True, ())
    other = {"HostConfig": {"DeviceRequests": [{"Driver": "x", "Capabilities": [["foo"]], "Count": -1}]}}
    assert dc.gpu_request(other) == (False, ())

def test_gpu_env_counts_only_with_nvidia_runtime():
    env = lambda v, rt: {"HostConfig": {"Runtime": rt}, "Config": {"Env": [f"NVIDIA_VISIBLE_DEVICES={v}"]}}
    assert dc.gpu_request(env("all", "runc")) == (False, ())
    assert dc.gpu_request(env("all", "nvidia")) == (True, ())
    assert dc.gpu_request(env("0,2", "nvidia")) == (False, ("0", "2"))
    assert dc.gpu_request(env("void", "nvidia")) == (False, ())

def test_resolve_indices():
    assert dc.resolve_gpu_indices((False, ("1", "GPU-aaa", "7", "GPU-zzz")), GPUS) == [0, 1]
    assert dc.resolve_gpu_indices((True, ()), GPUS) == [0, 1]
    assert dc.resolve_gpu_indices((True, ()), None) == []

class _Img: tags = ["img:1"]
class _C:
    def __init__(self, sid, name, attrs):
        self.short_id, self.id, self.name, self.attrs = sid, sid * 5, name, attrs
        self.status, self.labels, self.image = "exited", {}, _Img()

def test_collect_containers_adds_fields_and_skips_own_reservation(monkeypatch):
    every_gpu = _dev(DeviceIDs=None, Count=-1)
    cs = [_C("aaaaaaaaaaaa", "glassops-agent", every_gpu), _C("bbbbbbbbbbbb", "jupyter", every_gpu)]
    monkeypatch.setattr(dc, "_client", type("Cl", (), {"containers": type("L", (), {"list": lambda s, all: cs})()})())
    out = {c["name"]: c for c in dc.collect_containers(GPUS, own_container_id="aaaaaaaaaaaa")}
    assert out["glassops-agent"]["gpu_reserved_indices"] == []
    assert out["jupyter"]["gpu_reserved_indices"] == [0, 1]
    assert {"limits", "started_at", "finished_at", "exit_code", "gpu_reserved"} <= set(out["jupyter"])

def test_failed_topology_fields_are_omitted_not_defaulted(monkeypatch):
    # A field that could not be read is unknown: it is left out, never sent as "nothing set".
    real_limits, real_times = dc.container_limits, dc.container_times
    def failing(real, marker):
        def f(attrs):
            if attrs.get("fail") == marker:
                raise RuntimeError("boom")
            return real(attrs)
        return f
    monkeypatch.setattr(dc, "container_limits", failing(real_limits, "limits"))
    monkeypatch.setattr(dc, "container_times", failing(real_times, "times"))
    every_gpu = _dev(DeviceIDs=None, Count=-1)
    cs = [_C("aaaaaaaaaaaa", "a", {**every_gpu, "fail": "limits"}),
          _C("bbbbbbbbbbbb", "b", {"HostConfig": {"NanoCpus": 2_000_000_000}, "fail": "times"}),
          _C("cccccccccccc", "c", every_gpu)]
    monkeypatch.setattr(dc, "_client", type("Cl", (), {"containers": type("L", (), {"list": lambda s, all: cs})()})())
    out = {c["name"]: c for c in dc.collect_containers(GPUS)}
    new = {"limits", "started_at", "finished_at", "exit_code", "gpu_reserved_indices"}
    assert new.isdisjoint(out["a"]) and out["a"]["gpu_reserved"] is True
    assert out["b"]["limits"]["cpus"] == 2.0 and (new - {"limits"}).isdisjoint(out["b"])
    assert new <= set(out["c"]) and out["c"]["gpu_reserved_indices"] == [0, 1]

async def test_collect_metrics_passes_gpus_and_own_id(monkeypatch):
    seen = {}
    monkeypatch.setattr(m, "ENABLE_GPU", True)
    monkeypatch.setattr(m, "ENABLE_DOCKER", True)
    monkeypatch.setattr(m, "collect_gpu", lambda: GPUS)
    monkeypatch.setattr(m, "OWN_CONTAINER_ID", "aaaaaaaaaaaa")
    monkeypatch.setattr(m, "collect_containers",
                        lambda gpus=None, own_container_id=None: seen.update(g=gpus, o=own_container_id) or [])
    monkeypatch.setattr(m.pm2, "collect", lambda: [])   # no real /proc discovery thread from this test
    await m.collect_metrics()
    assert seen == {"g": GPUS, "o": "aaaaaaaaaaaa"}
