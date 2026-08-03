from app.providers.base import Provider
from app.services.candidate_builder import CandidateBuilder
from app.services.provider_manager import ProviderManager
from app.services.routing import RoutingEngine


def make_provider(
    name,
    models,
    priority=None,
    priority_models=None,
    api_key="test-key",
    enabled=True,
):
    provider = Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key=api_key,
        enabled=enabled,
        priority=priority,
        models=list(models),
        priority_models=list(priority_models or []),
    )
    return provider


class TestProviderManager:
    def test_ranked_filters_disabled(self):
        manager = ProviderManager()
        manager.register(
            make_provider("A", ["a-1"], priority=10, enabled=False)
        )
        manager.register(
            make_provider("B", ["b-1"], priority=5, enabled=True)
        )

        ranked = manager.ranked()

        assert [p.name for p in ranked] == ["B"]

    def test_ranked_filters_missing_api_key(self):
        manager = ProviderManager()
        manager.register(
            make_provider("A", ["a-1"], priority=10, api_key="  ")
        )
        manager.register(
            make_provider("B", ["b-1"], priority=5, api_key="key")
        )

        ranked = manager.ranked()

        assert [p.name for p in ranked] == ["B"]

    def test_ranked_sorts_by_priority_desc(self):
        manager = ProviderManager()
        manager.register(
            make_provider("Low", ["l-1"], priority=1)
        )
        manager.register(
            make_provider("High", ["h-1"], priority=10)
        )
        manager.register(
            make_provider("Mid", ["m-1"], priority=5)
        )

        ranked = manager.ranked()

        assert [p.name for p in ranked] == ["High", "Mid", "Low"]

    def test_best_returns_highest_priority(self):
        manager = ProviderManager()
        manager.register(
            make_provider("A", ["a-1"], priority=3)
        )
        manager.register(
            make_provider("B", ["b-1"], priority=9)
        )

        assert manager.best().name == "B"

    def test_best_returns_none_when_empty(self):
        assert ProviderManager().best() is None

    def test_enabled_returns_only_enabled(self):
        manager = ProviderManager()
        manager.register(
            make_provider("A", ["a-1"], priority=10, enabled=False)
        )
        manager.register(
            make_provider("B", ["b-1"], priority=5, enabled=True)
        )

        assert [p.name for p in manager.enabled()] == ["B"]


class TestCandidateBuilderFallback:
    """
    Candidate ordering when routing is disabled or yields nothing:
    ranked providers in order, models in provider.models order,
    only chat-testable models.
    """

    def test_fallback_orders_ranked_providers(self):
        builder = CandidateBuilder()
        high = make_provider("High", ["h-1"], priority=10)
        low = make_provider("Low", ["l-1"], priority=1)

        candidates = builder.build([high, low])

        assert [(p.name, m) for p, m in candidates] == [
            ("High", "h-1"),
            ("Low", "l-1"),
        ]

    def test_fallback_preserves_model_order(self):
        builder = CandidateBuilder()
        provider = make_provider(
            "A",
            ["a-1", "a-2", "a-3"],
            priority=5,
        )

        candidates = builder.build([provider])

        assert [m for _, m in candidates] == ["a-1", "a-2", "a-3"]

    def test_fallback_excludes_non_chat_models(self):
        builder = CandidateBuilder()
        provider = make_provider(
            "A",
            ["meta/llama-3-70b", "nvidia/nim-embedding", "llava-vl"],
            priority=5,
        )

        candidates = builder.build([provider])

        assert [m for _, m in candidates] == ["meta/llama-3-70b", "llava-vl"]

    def test_fallback_empty_when_no_chat_models(self):
        builder = CandidateBuilder()
        provider = make_provider(
            "A",
            ["nvidia/nim-embedding", "meta-llama-guard-2"],
            priority=5,
        )

        assert builder.build([provider]) == []

    def test_fallback_preserves_priority_applied_model_order(self):
        """
        Priority reordering is applied at provider creation time; the
        builder must preserve the resulting provider.models order.
        """
        builder = CandidateBuilder()
        provider = make_provider(
            "A",
            ["a-3", "a-1", "a-2"],
            priority=5,
            priority_models=["a-3", "a-1"],
        )

        candidates = builder.build([provider])

        assert [m for _, m in candidates] == ["a-3", "a-1", "a-2"]

    def test_no_routing_uses_all_chat_models_across_providers(self):
        builder = CandidateBuilder()
        p1 = make_provider("A", ["a-1", "nvidia/nim-embedding"], priority=10)
        p2 = make_provider("B", ["b-1"], priority=1)

        candidates = builder.build([p1, p2])

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "a-1"),
            ("B", "b-1"),
        ]


class TestCandidateBuilderRouting:
    def test_routing_enabled_uses_task_refs(self):
        settings = _FakeSettings(
            task_routing_enabled=True,
            task_coding=["a-1", "b-1"],
            task_vision=[],
            task_reasoning=[],
            task_general=[],
            task_creative=[],
            task_translation=[],
        )
        routing = RoutingEngine(config=settings)
        builder = CandidateBuilder(routing=routing)

        p1 = make_provider("A", ["a-1", "a-2"], priority=10)
        p2 = make_provider("B", ["b-1"], priority=1)

        candidates = builder.build([p1, p2], task="coding")

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "a-1"),
            ("B", "b-1"),
        ]

    def test_routing_disabled_ignores_task_refs(self):
        settings = _FakeSettings(
            task_routing_enabled=False,
            task_coding=["a-1"],
            task_vision=[],
            task_reasoning=[],
            task_general=[],
            task_creative=[],
            task_translation=[],
        )
        routing = RoutingEngine(config=settings)
        builder = CandidateBuilder(routing=routing)

        p1 = make_provider("A", ["a-1", "a-2"], priority=10)

        candidates = builder.build([p1], task="coding")

        assert [m for _, m in candidates] == ["a-1", "a-2"]

    def test_routing_prefers_specific_provider_ref(self):
        settings = _FakeSettings(
            task_routing_enabled=True,
            task_coding=["B:b-1"],
            task_vision=[],
            task_reasoning=[],
            task_general=[],
            task_creative=[],
            task_translation=[],
        )
        routing = RoutingEngine(config=settings)
        builder = CandidateBuilder(routing=routing)

        p1 = make_provider("A", ["a-1"], priority=10)
        p2 = make_provider("B", ["b-1"], priority=1)

        candidates = builder.build([p1, p2], task="coding")

        assert [(p.name, m) for p, m in candidates] == [("B", "b-1")]

    def test_routing_filters_non_chat_refs(self):
        settings = _FakeSettings(
            task_routing_enabled=True,
            task_coding=["nvidia/nim-embedding", "a-1"],
            task_vision=[],
            task_reasoning=[],
            task_general=[],
            task_creative=[],
            task_translation=[],
        )
        routing = RoutingEngine(config=settings)
        builder = CandidateBuilder(routing=routing)

        p1 = make_provider("A", ["a-1"], priority=10)

        candidates = builder.build([p1], task="coding")

        assert [(p.name, m) for p, m in candidates] == [("A", "a-1")]

    def test_no_task_falls_back(self):
        settings = _FakeSettings(
            task_routing_enabled=True,
            task_coding=["a-1"],
            task_vision=[],
            task_reasoning=[],
            task_general=[],
            task_creative=[],
            task_translation=[],
        )
        routing = RoutingEngine(config=settings)
        builder = CandidateBuilder(routing=routing)

        p1 = make_provider("A", ["a-1", "a-2"], priority=10)

        candidates = builder.build([p1])

        assert [m for _, m in candidates] == ["a-1", "a-2"]

    def test_unknown_task_falls_back(self):
        settings = _FakeSettings(
            task_routing_enabled=True,
            task_coding=["a-1"],
            task_vision=[],
            task_reasoning=[],
            task_general=[],
            task_creative=[],
            task_translation=[],
        )
        routing = RoutingEngine(config=settings)
        builder = CandidateBuilder(routing=routing)

        p1 = make_provider("A", ["a-1", "a-2"], priority=10)

        candidates = builder.build([p1], task="nonsense")

        assert [m for _, m in candidates] == ["a-1", "a-2"]


class _FakeSettings:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)
