"""
P9b tests: summary verifier accept/reject matrix, monotonic
``up_to_seq``, unknown-version rejection, token-consistency checks, and
the redaction hard guard.
"""

from dataclasses import replace

from app.models.continuity import SUMMARY_VERSION
from app.services.summarizer import extractive_summarize
from app.services.summary_verifier import verify


def _turns(count):
    return [
        {
            "conversation_id": "c1",
            "seq": i,
            "provider": "p1",
            "model": "m1",
            "outcome": "ok",
            "task": "probe",
            "tokens_in": 100,
            "tokens_out": 50,
            "latency_ms": 10,
            "ts": 1000.0 + i,
        }
        for i in range(1, count + 1)
    ]


def _valid_summary():
    return extractive_summarize(_turns(5), params={}, now=1.0)


def _conversation():
    return {"id": "c1"}


class TestVerifyAccept:
    def test_structurally_valid_summary_accepted(self):
        assert verify(_valid_summary(), _conversation(), _turns(5)) is True

    def test_accepted_with_strictly_newer_latest_up_to_seq(self):
        summary = _valid_summary()
        assert verify(
            summary, _conversation(), _turns(5), latest_up_to_seq=summary.up_to_seq - 1
        ) is True


class TestVerifyReject:
    def test_none_summary_rejected(self):
        assert verify(None, _conversation(), _turns(5)) is False

    def test_missing_conversation_rejected(self):
        assert verify(_valid_summary(), None, _turns(5)) is False

    def test_mismatched_conversation_rejected(self):
        assert verify(_valid_summary(), {"id": "other"}, _turns(5)) is False

    def test_unknown_up_to_seq_rejected(self):
        summary = replace(_valid_summary(), up_to_seq=99)
        assert verify(summary, _conversation(), _turns(5)) is False

    def test_zero_up_to_seq_rejected(self):
        summary = replace(_valid_summary(), up_to_seq=0)
        assert verify(summary, _conversation(), _turns(5)) is False

    def test_non_monotonic_up_to_seq_rejected(self):
        summary = _valid_summary()
        assert (
            verify(
                summary,
                _conversation(),
                _turns(5),
                latest_up_to_seq=summary.up_to_seq,
            )
            is False
        )
        assert (
            verify(
                summary,
                _conversation(),
                _turns(5),
                latest_up_to_seq=summary.up_to_seq + 10,
            )
            is False
        )

    def test_unknown_version_rejected(self):
        summary = replace(_valid_summary(), version=SUMMARY_VERSION + 1)
        assert verify(summary, _conversation(), _turns(5)) is False

    def test_inconsistent_tokens_in_rejected(self):
        summary = replace(_valid_summary(), tokens_in=501)
        assert verify(summary, _conversation(), _turns(5)) is False

    def test_negative_tokens_out_rejected(self):
        summary = replace(_valid_summary(), tokens_out=-1)
        assert verify(summary, _conversation(), _turns(5)) is False

    def test_forbidden_key_export_rejected(self):
        data = {
            "conversation_id": "c1",
            "up_to_seq": 5,
            "version": SUMMARY_VERSION,
            "method": "extractive",
            "content": "leaked payload",
            "tokens_in": None,
            "tokens_out": None,
        }
        assert verify(data, _conversation(), _turns(5)) is False

    def test_summary_with_forbidden_nested_key_rejected(self):
        data = {
            "conversation_id": "c1",
            "up_to_seq": 5,
            "version": SUMMARY_VERSION,
            "method": "extractive",
            "summary_text": "clean",
            "tokens_in": None,
            "tokens_out": None,
            "meta": {"prompt": "leaked"},
        }
        assert verify(data, _conversation(), _turns(5)) is False

    def test_non_list_turns_rejected(self):
        assert verify(_valid_summary(), _conversation(), "not-a-list") is False

    def test_turns_without_referenced_seq_rejected(self):
        summary = _valid_summary()
        fewer = _turns(3)
        assert verify(summary, _conversation(), fewer) is False
