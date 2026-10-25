"""Tests for the AI Integrity Detection service."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from src.services.documents.integrity_detection_service import IntegrityDetectionService


def test_split_into_segments():
    service = IntegrityDetectionService.__new__(IntegrityDetectionService)
    text = " ".join(["word"] * 1000)
    segments = service._split_into_segments(text, max_words=100)
    assert len(segments) > 1
    for seg in segments:
        assert len(seg.split()) <= 100


def test_split_short_text():
    service = IntegrityDetectionService.__new__(IntegrityDetectionService)
    text = "This is a short text."
    segments = service._split_into_segments(text, max_words=100)
    assert len(segments) == 1


def test_split_exact_boundary():
    service = IntegrityDetectionService.__new__(IntegrityDetectionService)
    text = " ".join(["word"] * 100)
    segments = service._split_into_segments(text, max_words=100)
    assert len(segments) == 1


def test_aggregate_scores():
    service = IntegrityDetectionService.__new__(IntegrityDetectionService)
    segment_scores = [
        {"text_preview": "First...", "ai_probability": 0.8, "length": 100},
        {"text_preview": "Second...", "ai_probability": 0.4, "length": 200},
    ]
    avg = service._aggregate_scores(segment_scores)
    assert abs(avg - 0.5333) < 0.01


def test_aggregate_scores_empty():
    service = IntegrityDetectionService.__new__(IntegrityDetectionService)
    assert service._aggregate_scores([]) == 0.0


def test_aggregate_scores_zero_length_segments():
    service = IntegrityDetectionService.__new__(IntegrityDetectionService)
    scores = [{"text_preview": "...", "ai_probability": 0.9, "length": 0}]
    assert service._aggregate_scores(scores) == 0.0


def test_analyze_uses_fake_class_probability_for_ai_score(monkeypatch):
    """Class 0 (Fake), not class 1 (Real), is the AI-generated score."""

    class Probability:
        def __init__(self, value):
            self.value = value

        def item(self):
            return self.value

    fake_torch = SimpleNamespace(
        no_grad=nullcontext,
        nn=SimpleNamespace(
            functional=SimpleNamespace(
                softmax=lambda logits, dim: [[Probability(0.9), Probability(0.1)]]
            )
        ),
    )
    monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch)

    service = IntegrityDetectionService()
    service._tokenizer = lambda *args, **kwargs: {"input_ids": [1]}
    service._model = lambda **kwargs: SimpleNamespace(logits=object())

    result = service._analyze_sync("generated text")

    assert result["ai_probability"] == 0.9
    assert result["human_probability"] == 0.1
