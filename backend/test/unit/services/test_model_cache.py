from starring.models.providers.cache import ModelCache


def test_model_cache_prefers_model_base_url_override(monkeypatch):
    saved_cache = {}

    class Provider:
        is_enabled = True
        provider_id = "alibaba"
        api_key = "sk-test"
        api_key_env = None
        provider_type = "openai"
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        embedding_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
        rerank_base_url = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
        headers_json = {}
        extra_json = {}
        enabled_models = [
            {
                "id": "qwen3-rerank",
                "type": "rerank",
                "display_name": "Qwen3 Rerank",
                "base_url_override": "https://invalid.example/rerank",
            }
        ]

    cache = ModelCache()
    monkeypatch.setattr(cache, "_save_cache", lambda data: saved_cache.update(data))

    cache.rebuild([Provider()])

    assert saved_cache["alibaba:qwen3-rerank"].base_url == "https://invalid.example/rerank"
