import pytest

from app.services.model_catalog import (
    MODEL_CATALOG_SEED,
    NEUTRAL_COMPATIBILITY,
    ModelCatalog,
)


class TestModelCatalogLookup:
    def test_exact_id_match(self):
        catalog = ModelCatalog()

        profile = catalog.lookup("gpt-4o-2024-05-13")

        assert profile is not None
        assert profile.model == "gpt-4o"

    def test_family_prefix_match(self):
        catalog = ModelCatalog()

        profile = catalog.lookup("gpt-4o-mini-2024-07-18")

        assert profile is not None
        assert profile.model == "gpt-4o-mini"

    def test_longest_family_prefix_wins(self):
        catalog = ModelCatalog()

        mini = catalog.lookup("gpt-4o-mini-2024-07-18")
        full = catalog.lookup("gpt-4o-2024-05-13")

        assert mini.model == "gpt-4o-mini"
        assert full.model == "gpt-4o"

    def test_keyword_fallback(self):
        catalog = ModelCatalog()

        vision = catalog.lookup("meta/llama-vision")
        translate = catalog.lookup("nvidia/nemotron-translate")

        assert vision is not None
        assert vision.compatibility["vision"] == 0.9
        assert translate is not None
        assert translate.compatibility["translation"] == 0.9

    def test_unknown_model_returns_none(self):
        catalog = ModelCatalog()

        assert catalog.lookup("totally-unknown-xyz") is None

    def test_empty_model_returns_none(self):
        catalog = ModelCatalog()

        assert catalog.lookup("") is None
        assert catalog.lookup(None) is None

    def test_lookup_is_case_insensitive(self):
        catalog = ModelCatalog()

        assert catalog.lookup("GPT-4O-2024-05-13").model == "gpt-4o"


class TestModelCatalogScoring:
    def test_unknown_model_scores_neutral(self):
        catalog = ModelCatalog()

        assert catalog.score("totally-unknown-xyz", "coding") == 0.5

    def test_unknown_task_scores_neutral(self):
        catalog = ModelCatalog()

        assert catalog.score("gpt-4o-2024-05-13", "not-a-task") == 0.5

    def test_no_task_defaults_to_general(self):
        catalog = ModelCatalog()

        assert catalog.score("gpt-4o-2024-05-13") == catalog.score(
            "gpt-4o-2024-05-13", "general"
        )

    def test_vision_model_scores_higher_for_vision(self):
        catalog = ModelCatalog()

        vision_score = catalog.score("gpt-4o-2024-05-13", "vision")
        coding_score = catalog.score("gpt-4o-2024-05-13", "coding")

        assert vision_score > coding_score

    def test_scores_stay_in_unit_range(self):
        catalog = ModelCatalog()

        for profile in MODEL_CATALOG_SEED:
            for task, value in profile.compatibility.items():
                assert 0.0 <= value <= 1.0


class TestCatalogSeedCoverage:
    def test_seed_covers_configured_openai_models(self):
        catalog = ModelCatalog()

        configured = [
            "gpt-3.5-turbo",
            "gpt-3.5-turbo-16k",
            "gpt-3.5-turbo-instruct",
            "gpt-4o-2024-05-13",
            "gpt-4o-mini-2024-07-18",
            "gpt-5.5",
            "gpt-5.5-2026-04-23",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
        ]

        for model in configured:
            assert catalog.lookup(model) is not None, model
            assert catalog.score(model, "general") != NEUTRAL_COMPATIBILITY

    def test_catalog_is_read_only_and_stateless(self):
        catalog = ModelCatalog()
        before = catalog.score("gpt-4o-2024-05-13", "vision")

        catalog.lookup("gpt-4o-2024-05-13")
        catalog.score("gpt-4o-2024-05-13", "coding")

        assert catalog.score("gpt-4o-2024-05-13", "vision") == before
