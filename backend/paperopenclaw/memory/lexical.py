"""Offline lexical retrieval: BM25 ranking and hashed n-gram vectors.

Two jobs, both serving NFR-6 (the app must stay useful without network):

* :class:`BM25Index` supplies the exact-term half of hybrid retrieval. Vector
  search alone handles paraphrase poorly on identifiers, metric names and
  numbers ("BLEU-4", "表 3", "ResNet-50"), which is exactly what people type.
* :func:`hashed_embedding` is the last-resort embedding provider: a pure-Python
  feature-hashing vectoriser requiring no model download at all. It is weaker
  than a real model, but it means a fresh install with no network still gets a
  working semantic-ish index instead of a hard failure.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.text import tokenize

# Feature-hashing dimensions. 384 matches the small BGE family so switching
# between the lexical fallback and a real model keeps vector sizes comparable
# in debugging tools, though the two are never mixed in one collection.
DEFAULT_DIM = 384


class BM25Index:
    """Okapi BM25 over an in-memory document set.

    Parameters follow the usual defaults (k1=1.5, b=0.75). The index is cheap to
    rebuild — a few thousand units cost milliseconds — so the retriever builds a
    fresh one per query restricted to the candidate set rather than maintaining a
    persistent inverted index that could drift out of sync with the database.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._ids: List[str] = []
        self._freqs: List[Counter] = []
        self._lengths: List[int] = []
        self._df: Counter = Counter()
        self._avgdl = 0.0

    def add(self, doc_id: str, text: str) -> None:
        tokens = tokenize(text)
        counts = Counter(tokens)
        self._ids.append(doc_id)
        self._freqs.append(counts)
        self._lengths.append(len(tokens))
        for term in counts:
            self._df[term] += 1

    def extend(self, documents: Iterable[Tuple[str, str]]) -> None:
        for doc_id, text in documents:
            self.add(doc_id, text)
        self._finalise()

    def _finalise(self) -> None:
        total = sum(self._lengths)
        self._avgdl = (total / len(self._lengths)) if self._lengths else 0.0

    @property
    def size(self) -> int:
        return len(self._ids)

    def _idf(self, term: str) -> float:
        n = len(self._ids)
        df = self._df.get(term, 0)
        # BM25+ style floor keeps very common terms from going negative.
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def score(self, query: str) -> Dict[str, float]:
        """Return BM25 scores for every document containing a query term."""
        query_terms = tokenize(query)
        if not query_terms or not self._ids:
            return {}
        scores: Dict[str, float] = {}
        avgdl = self._avgdl or 1.0
        for index, doc_id in enumerate(self._ids):
            freq = self._freqs[index]
            length = self._lengths[index] or 1
            total = 0.0
            for term in query_terms:
                tf = freq.get(term)
                if not tf:
                    continue
                denominator = tf + self.k1 * (1 - self.b + self.b * length / avgdl)
                total += self._idf(term) * (tf * (self.k1 + 1)) / denominator
            if total > 0:
                scores[doc_id] = total
        return scores

    def rank(self, query: str, top_k: int = 0) -> List[Tuple[str, float]]:
        scored = sorted(self.score(query).items(), key=lambda kv: kv[1], reverse=True)
        return scored[:top_k] if top_k else scored


# ---------------------------------------------------------------------------
# hashed n-gram embeddings (no model required)
# ---------------------------------------------------------------------------

_LATIN_RUN = re.compile(r"[a-z0-9]+")


def _features(text: str) -> List[str]:
    """Feature set: word tokens plus character n-grams for CJK substrings."""
    lowered = (text or "").lower()
    if not lowered.strip():
        return []
    features: List[str] = []

    tokens = tokenize(lowered, keep_stopwords=False)
    features.extend(f"w:{token}" for token in tokens)

    # Character 2-3 grams capture Chinese morphology and stem-like English
    # prefixes, which lifts recall when the tokeniser splits differently.
    compact = re.sub(r"\s+", "", lowered)
    for n in (2, 3):
        for start in range(0, max(0, len(compact) - n + 1)):
            gram = compact[start : start + n]
            if gram.strip():
                features.append(f"g{n}:{gram}")
    return features


def _bucket(feature: str, dim: int) -> int:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dim


def hashed_embedding(text: str, dim: int = DEFAULT_DIM, normalize: bool = True) -> List[float]:
    """Deterministic feature-hashed bag-of-features vector.

    Signed hashing (the sign bit decides +1/-1) keeps unrelated collisions from
    systematically inflating similarity, which is the usual failure mode of
    unsigned feature hashing at small dimensions.
    """
    vector = [0.0] * dim
    features = _features(text)
    if not features:
        return vector
    counts = Counter(features)
    for feature, count in counts.items():
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        index = value % dim
        sign = 1.0 if (value >> 63) & 1 else -1.0
        # Sublinear term frequency damps repeated boilerplate.
        vector[index] += sign * (1.0 + math.log(count))
    if normalize:
        norm = math.sqrt(sum(component * component for component in vector))
        if norm > 0:
            vector = [component / norm for component in vector]
    return vector


def hashed_embeddings(texts: Sequence[str], dim: int = DEFAULT_DIM,
                      normalize: bool = True) -> List[List[float]]:
    return [hashed_embedding(text, dim=dim, normalize=normalize) for text in texts]


# ---------------------------------------------------------------------------
# rank fusion
# ---------------------------------------------------------------------------

def reciprocal_rank_fusion(
    rankings: Sequence[Dict[str, float]],
    weights: Optional[Sequence[float]] = None,
    k: int = 60,
) -> Dict[str, float]:
    """Fuse several ranked lists into one score map.

    RRF is used instead of weighted score averaging because cosine similarity
    and BM25 live on incomparable scales; fusing *ranks* sidesteps the need to
    calibrate them against each other.
    """
    if not rankings:
        return {}
    if weights is None:
        weights = [1.0] * len(rankings)
    fused: Dict[str, float] = {}
    for ranking, weight in zip(rankings, weights):
        ordered = sorted(ranking.items(), key=lambda kv: kv[1], reverse=True)
        for position, (doc_id, _score) in enumerate(ordered):
            fused[doc_id] = fused.get(doc_id, 0.0) + weight / (k + position + 1)
    return fused


def normalize_scores(scores: Dict[str, float]) -> Dict[str, float]:
    """Scale a score map into ``[0, 1]`` for display and thresholding."""
    if not scores:
        return {}
    values = list(scores.values())
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return {key: 1.0 for key in scores}
    return {key: (value - low) / (high - low) for key, value in scores.items()}


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity that tolerates mismatched or empty vectors."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / math.sqrt(left_norm * right_norm)
