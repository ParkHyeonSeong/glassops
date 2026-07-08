def test_harness_runs():
    assert True


def test_can_import_agent_and_backend_paths():
    import agent.config  # from agent/ on pythonpath
    import app.config     # from backend/ on pythonpath
    assert agent.config is not None
    assert app.config is not None
