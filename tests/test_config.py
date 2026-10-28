import os


def test_env_loaded():
    import ole.config  # noqa: F401

    assert os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-")
