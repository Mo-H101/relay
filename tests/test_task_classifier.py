import pytest

from app.services.routing import TASK_CATEGORIES
from app.services.task_classifier import (
    DEFAULT_THRESHOLD,
    GENERAL,
    classify_task,
    classify_task_with_confidence,
)


class TestDeterminism:
    def test_same_input_same_result(self):
        first = classify_task("write a python function to sort a list")
        second = classify_task("write a python function to sort a list")

        assert first == second == "coding"

    def test_case_insensitive(self):
        assert classify_task("TRANSLATE this") == "translation"
        assert classify_task("translate this") == "translation"

    @pytest.mark.parametrize(
        "message",
        [
            "hello world",
            "how are you today",
            "this is a simple question",
            "",
            None,
        ],
    )
    def test_no_match_falls_back_to_general(self, message):
        assert classify_task(message) == GENERAL
        task, confidence = classify_task_with_confidence(message)
        assert task == GENERAL
        assert confidence == 0.0


class TestCategoryClassification:
    @pytest.mark.parametrize(
        "message,category",
        [
            ("write a python function to sort a list", "coding"),
            ("fix this bug in my javascript", "coding"),
            ("explain this stack trace", "coding"),
            ("translate this text into french", "translation"),
            ("translate to spanish for me", "translation"),
            ("describe the diagram in this image", "vision"),
            ("add a caption to the photo", "vision"),
            ("solve this puzzle step by step", "reasoning"),
            ("prove this math statement", "reasoning"),
            ("write a story about a robot", "creative"),
            ("imagine a poem about the sea", "creative"),
        ],
    )
    def test_clear_signal_classifies(self, message, category):
        assert classify_task(message) == category


class TestConfidenceThreshold:
    def test_ambiguous_message_falls_back_below_threshold(self):
        task, confidence = classify_task_with_confidence("translate a picture")

        assert confidence == pytest.approx(0.5)
        assert confidence < DEFAULT_THRESHOLD
        assert task == GENERAL

    def test_dominant_category_wins_above_threshold(self):
        task, confidence = classify_task_with_confidence(
            "translate an image caption"
        )

        assert task == "vision"
        assert confidence == pytest.approx(2 / 3)

    def test_lowered_threshold_accepts_ambiguous(self):
        task = classify_task("translate my code", threshold=0.4)

        assert task == "coding"

    def test_raised_threshold_rejects_single_signal(self):
        task = classify_task("translate a picture", threshold=0.9)

        assert task == GENERAL

    def test_confidence_in_unit_range(self):
        _, confidence = classify_task_with_confidence(
            "translate this python code into french"
        )

        assert 0.0 <= confidence <= 1.0


class TestResultInvariant:
    def test_never_returns_unknown_category(self):
        for message in [
            "write a python function",
            "translate a picture",
            "describe this image",
            "nothing here",
            "",
        ]:
            task = classify_task(message)
            assert task in TASK_CATEGORIES

    def test_general_is_a_valid_category(self):
        assert GENERAL in TASK_CATEGORIES

    def test_classifier_is_stateless(self):
        classify_task("write a python function")

        assert classify_task("hello world") == GENERAL
