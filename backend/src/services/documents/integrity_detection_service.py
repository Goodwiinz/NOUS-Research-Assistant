"""AI Integrity Detection service using RoBERTa classifier."""

import asyncio
import threading
from typing import Any, Dict, List

import structlog

logger = structlog.get_logger()


class IntegrityDetectionService:
    MODEL_NAME = "roberta-base-openai-detector"

    def __init__(self) -> None:
        self._model = None
        self._tokenizer = None
        self._load_lock = threading.Lock()

    def _load_model(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            logger.info("loading_integrity_model", model=self.MODEL_NAME)
            self._tokenizer = AutoTokenizer.from_pretrained(self.MODEL_NAME)
            self._model = AutoModelForSequenceClassification.from_pretrained(
                self.MODEL_NAME
            )
            self._model.requires_grad_(False)

    def _split_into_segments(self, text: str, max_words: int = 380) -> List[str]:
        """Split text into segments by word count.

        Uses max_words (not tokens) as a conservative approximation
        for the 512-token RoBERTa context window.
        """
        words = text.split()
        if len(words) <= max_words:
            return [text]
        segments = []
        for i in range(0, len(words), max_words):
            segments.append(" ".join(words[i : i + max_words]))
        return segments

    def _aggregate_scores(self, segment_scores: List[Dict[str, Any]]) -> float:
        total_weight = sum(s["length"] for s in segment_scores)
        if total_weight == 0:
            return 0.0
        return (
            sum(s["ai_probability"] * s["length"] for s in segment_scores)
            / total_weight
        )

    def _analyze_sync(self, text: str) -> Dict[str, Any]:
        """Run synchronous inference (CPU-bound). Called via run_in_executor."""
        import torch

        self._load_model()
        segments = self._split_into_segments(text)
        segment_scores = []
        for segment in segments:
            inputs = self._tokenizer(
                segment, return_tensors="pt", truncation=True, max_length=512
            )
            with torch.no_grad():
                outputs = self._model(**inputs)
            probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
            # roberta-base-openai-detector maps class 0 to Fake (AI-generated)
            # and class 1 to Real (human-authored).
            ai_prob = probs[0][0].item()
            segment_scores.append(
                {
                    "text_preview": segment[:200],
                    "ai_probability": round(ai_prob, 4),
                    "length": len(segment.split()),
                }
            )
        ai_probability = self._aggregate_scores(segment_scores)
        return {
            "ai_probability": round(ai_probability, 4),
            "human_probability": round(1.0 - ai_probability, 4),
            "method": self.MODEL_NAME,
            "segment_scores": [
                {
                    "text_preview": s["text_preview"],
                    "ai_probability": s["ai_probability"],
                }
                for s in segment_scores
            ],
        }

    async def analyze(self, text: str) -> Dict[str, Any]:
        """Analyze text for AI authorship. Offloads to thread pool to avoid blocking."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._analyze_sync, text)
