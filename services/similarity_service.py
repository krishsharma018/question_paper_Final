"""
services/similarity_service.py
Layered duplicate and near-duplicate detection.

Layer 1: Normalized exact comparison
Layer 2: TF-IDF cosine similarity (scikit-learn)
Layer 3: Sentence-transformer embedding similarity (local model)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from config.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class SimilarityResult:
    """Result of a pairwise similarity check."""
    question_a: str
    question_b: str
    exact_match: bool
    tfidf_score: float
    embedding_score: float
    max_score: float
    status: str  # "DUPLICATE" | "WARNING" | "OK"
    explanation: str


class SimilarityService:
    """
    Provides layered text similarity checks.
    The sentence-transformer model is loaded lazily and cached.
    """

    def __init__(self):
        self._settings = get_settings()
        self._embedding_model = None
        self._tfidf_vectorizer = None
        self._exact_threshold = 1.0
        self._dup_threshold = self._settings.semantic_duplicate_threshold
        self._warn_threshold = self._settings.semantic_warning_threshold

    def _normalize(self, text: str) -> str:
        """Lowercase, strip extra whitespace, remove punctuation."""
        text = text.lower().strip()
        text = re.sub(r"[^\w\s]", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _load_embedding_model(self):
        """Lazily load the sentence-transformer model."""
        if self._embedding_model is not None:
            return
        if self._settings.is_mock_mode:
            self._embedding_model = None
            return
        try:
            from sentence_transformers import SentenceTransformer
            model_name = self._settings.embedding_model
            logger.info(f"Loading sentence-transformer: {model_name}")
            self._embedding_model = SentenceTransformer(model_name)
            logger.info("Sentence-transformer loaded")
        except Exception as exc:
            logger.warning(f"Could not load sentence-transformer: {exc}. Embedding check disabled.")
            self._embedding_model = None

    def _embed(self, texts: list[str]) -> Optional[np.ndarray]:
        """Return embedding matrix or None if model unavailable."""
        self._load_embedding_model()
        if self._embedding_model is None:
            return None
        try:
            return self._embedding_model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        except Exception as exc:
            logger.warning(f"Embedding failed: {exc}")
            return None

    def _tfidf_score(self, a: str, b: str) -> float:
        """Compute TF-IDF cosine similarity between two texts."""
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity
            vect = TfidfVectorizer()
            matrix = vect.fit_transform([a, b])
            score = float(cosine_similarity(matrix[0], matrix[1])[0][0])
            return score
        except Exception as exc:
            logger.warning(f"TF-IDF computation failed: {exc}")
            return 0.0

    def _cosine(self, a: np.ndarray, b: np.ndarray) -> float:
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    def check_pair(self, text_a: str, text_b: str) -> SimilarityResult:
        """
        Full layered check between two question texts.
        """
        norm_a = self._normalize(text_a)
        norm_b = self._normalize(text_b)

        # Layer 1: Exact
        exact = norm_a == norm_b

        if exact:
            return SimilarityResult(
                question_a=text_a[:80],
                question_b=text_b[:80],
                exact_match=True,
                tfidf_score=1.0,
                embedding_score=1.0,
                max_score=1.0,
                status="DUPLICATE",
                explanation="Exact normalized match",
            )

        # Layer 2: TF-IDF
        tfidf = self._tfidf_score(norm_a, norm_b)

        # Layer 3: Embeddings
        embs = self._embed([text_a, text_b])
        if embs is not None and len(embs) == 2:
            emb_score = self._cosine(embs[0], embs[1])
        else:
            emb_score = tfidf  # fallback

        max_score = max(tfidf, emb_score)
        dup_thresh = self._dup_threshold
        warn_thresh = self._warn_threshold

        if max_score >= dup_thresh:
            status = "DUPLICATE"
            explanation = f"High semantic similarity ({max_score:.3f} ≥ {dup_thresh})"
        elif max_score >= warn_thresh:
            status = "WARNING"
            explanation = f"Moderate similarity ({max_score:.3f}) — review recommended"
        else:
            status = "OK"
            explanation = f"Similarity {max_score:.3f} below threshold"

        return SimilarityResult(
            question_a=text_a[:80],
            question_b=text_b[:80],
            exact_match=False,
            tfidf_score=round(tfidf, 4),
            embedding_score=round(emb_score, 4),
            max_score=round(max_score, 4),
            status=status,
            explanation=explanation,
        )

    def find_duplicates_in_list(
        self, texts: list[str]
    ) -> list[tuple[int, int, SimilarityResult]]:
        """
        Check all pairs in a list. Returns (i, j, result) for each pair
        where status != OK.
        """
        findings = []
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                result = self.check_pair(texts[i], texts[j])
                if result.status != "OK":
                    findings.append((i, j, result))
        return findings

    def check_against_corpus(
        self, new_text: str, corpus_texts: list[str]
    ) -> list[tuple[int, SimilarityResult]]:
        """
        Check new_text against a corpus.
        Returns [(corpus_index, result)] for non-OK similarities.
        """
        findings = []
        for i, ct in enumerate(corpus_texts):
            result = self.check_pair(new_text, ct)
            if result.status != "OK":
                findings.append((i, result))
        return findings
