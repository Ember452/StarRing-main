import sys


def test_import_STARRING_does_not_eagerly_import_knowledge(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    sys.modules.pop("starring", None)
    sys.modules.pop("starring.knowledge", None)

    import starring

    assert starring.get_version() == starring.__version__
    assert "starring.knowledge" not in sys.modules
