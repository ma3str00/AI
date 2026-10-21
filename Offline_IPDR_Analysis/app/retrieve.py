"""Small BM25 index over the question bank. No extra service and no GPU."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class ExampleIndex:
    def __init__(self, examples: list[dict]):
        self.examples = examples
        self.k1 = 1.5
        self.b = 0.75
        self.docs = [tokenize(_indexed_text(item)) for item in examples]
        self.n = len(self.docs)
        self.df: Counter[str] = Counter()
        self.lengths = [len(doc) for doc in self.docs]
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 0.0
        for doc in self.docs:
            for token in set(doc):
                self.df[token] += 1

    def top(self, question: str, k: int = 8) -> list[dict]:
        if not self.examples:
            return []
        query = tokenize(question)
        scored = []
        for index in range(self.n):
            score = self._score(query, index)
            if score > 0 and self.examples[index].get("learned"):
                score *= 2
            scored.append((score, index))
        scored.sort(key=lambda item: item[0], reverse=True)
        chosen = []
        for score, index in scored[:k]:
            if score <= 0 and chosen:
                break
            chosen.append(self.examples[index])
        return chosen

    def _score(self, query: list[str], index: int) -> float:
        document = self.docs[index]
        if not document or self.avgdl == 0:
            return 0.0
        frequency = Counter(document)
        score = 0.0
        length = self.lengths[index]
        for token in query:
            freq = frequency.get(token, 0)
            if not freq:
                continue
            documents_with_term = self.df[token]
            idf = math.log(1 + (self.n - documents_with_term + 0.5) / (documents_with_term + 0.5))
            denom = freq + self.k1 * (1 - self.b + self.b * length / self.avgdl)
            score += idf * (freq * (self.k1 + 1)) / denom
        return score


def load_examples(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Training examples not found: {path}")
    examples = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                examples.append(json.loads(line))
    return examples


def _indexed_text(item: dict) -> str:
    tags = " ".join(item.get("tags") or [])
    return f"{item.get('question', '')} {tags}"
