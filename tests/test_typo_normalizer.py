#!/usr/bin/env python3
"""Unit tests for Ultra-Fast Institutional Typo Normalizer."""

import time
import pytest
from pipeline.text_processing.typo_normalizer import TypoNormalizer
from pipeline.query_metadata_extractor import QueryMetadataExtractor


@pytest.fixture(scope="module")
def normalizer():
    return TypoNormalizer.get_instance()


class TestTypoNormalizer:
    def test_domain_typo_corrections(self, normalizer):
        cases = [
            ("what is neroscience", "what is neuroscience"),
            ("tell me about vermiculitte", "tell me about vermiculite"),
            ("theses in aerospce from 2021", "theses in aerospace from 2021"),
            ("show papers on magnatohydrodynamics", "show papers on magnetohydrodynamics"),
            ("research in nanoscienc and biophisics", "research in nanoscience and biophysics"),
            ("what is superconductiviti", "what is superconductivity"),
        ]
        for query, expected in cases:
            assert normalizer.normalize(query) == expected, f"Failed for {query}"

    def test_english_words_protection(self, normalizer):
        # Ensure valid English words like 'tell' are never mutated to 'cell'
        cases = [
            "tell me about vermiculite",
            "tell me more about cell biology",
            "show all works from last year",
            "how does this work",
            "can you give me an example",
            "what is the definition of energy",
        ]
        for query in cases:
            assert normalizer.normalize(query) == query, f"Mutated valid query: {query}"

    def test_case_preservation(self, normalizer):
        assert normalizer.normalize("NEROSCIENCE") == "NEUROSCIENCE"
        assert normalizer.normalize("Neroscience") == "Neuroscience"
        assert normalizer.normalize("neroscience") == "neuroscience"

    def test_short_and_alphanumeric_tokens_preserved(self, normalizer):
        cases = [
            "cns", "mbu", "ipc", "csa", "phd", "msc",
            "2005/12345", "thesis 2021", "room 101"
        ]
        for query in cases:
            assert normalizer.normalize(query) == query

    def test_out_of_domain_query_preserved(self, normalizer):
        ood = "Who was the prime minister of the United Kingdom in 1912?"
        assert normalizer.normalize(ood) == ood

    def test_ultra_low_latency_guarantee(self, normalizer):
        query = "what is neroscience in modern brain research?"
        # Warm-up
        normalizer.normalize(query)

        # Benchmark 1000 runs
        t0 = time.perf_counter()
        N = 1000
        for _ in range(N):
            normalizer.normalize(query)
        elapsed_total_ms = (time.perf_counter() - t0) * 1000.0
        avg_latency_ms = elapsed_total_ms / N

        print(f"\nTypo Normalizer average latency: {avg_latency_ms * 1000:.2f} microseconds ({avg_latency_ms:.5f} ms)")
        assert avg_latency_ms < 0.05, f"Typo normalizer latency {avg_latency_ms:.4f}ms exceeds 0.05ms limit!"

    def test_integration_with_metadata_extractor(self):
        extractor = QueryMetadataExtractor()
        res = extractor.extract("theses from neroscience in 2021")
        assert res.filters.get("department") == "Centre for Neuroscience"
        assert res.filters.get("date") == "2021"

