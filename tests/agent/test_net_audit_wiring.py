import importlib
import agent.config as cfg


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("GLASSOPS_ENABLE_NET_AUDIT", raising=False)
    importlib.reload(cfg)
    assert cfg.ENABLE_NET_AUDIT is False
    assert cfg.NET_AUDIT_MAX_EVENTS == 200
    assert cfg.NET_AUDIT_TOP_TALKERS == 20


def test_flag_on(monkeypatch):
    monkeypatch.setenv("GLASSOPS_ENABLE_NET_AUDIT", "true")
    importlib.reload(cfg)
    assert cfg.ENABLE_NET_AUDIT is True
    importlib.reload(cfg)  # leave module in default state for others


def test_module_collect_returns_shape(tmp_path):
    # Point the singleton at a fake host_proc with no connections.
    (tmp_path / "1" / "net").mkdir(parents=True)
    for n in ("tcp", "tcp6", "udp", "udp6", "dev"):
        (tmp_path / "1" / "net" / n).write_text("header\n")
    import agent.collectors.net_audit as na
    na.reset_collector(host_proc=str(tmp_path))
    out = na.collect()
    assert set(out.keys()) == {"events", "rollups", "dropped"}
