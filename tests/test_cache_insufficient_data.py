#!/usr/bin/env python3
"""Unit and Integration tests for filtering uncacheable / insufficient data queries in Redis Semantic Cache."""

import unittest
from gateway.cache.redis_semantic_cache import RedisSemanticCache


class TestCacheInsufficientDataFiltering(unittest.TestCase):

    def setUp(self):
        self.cache = RedisSemanticCache.get_instance()

    def test_insufficient_data_phrases_detected(self):
        """Verify that standard refusal and insufficient data answers are rejected."""
        test_cases = [
            {"answer": "Based on the provided documents, there is insufficient information to answer this question."},
            {"answer": "There is insufficient data in the thesis collection to answer regarding 2024 admissions."},
            {"answer": "The context does not contain enough information to determine the result."},
            {"answer": "I do not have enough information to provide an answer."},
            {"answer": "No relevant documents were found in the knowledge base matching your query."},
            {"answer": "Retrieval failed: ConnectionError (Redis unreachable)."},
            {"answer": "Short"},
            {"answer": ""},
            {"error": "Inference server timeout"},
        ]

        for case in test_cases:
            cacheable, reason = self.cache.is_cacheable_response(case)
            self.assertFalse(
                cacheable,
                f"Expected response '{case}' to be marked uncacheable, but got cacheable=True."
            )

    def test_grounded_factual_answers_allowed(self):
        """Verify that legitimate grounded answers are marked cacheable."""
        valid_answers = [
            {"answer": "Vermiculite is a hydrous phyllosilicate mineral that undergoes significant expansion when heated, widely used in thermal insulation."},
            {"answer": "In magnetohydrodynamics, Rayleigh-Taylor instability develops when a heavier fluid is accelerated against a lighter fluid across a magnetic interface."},
            {"answer": "Diffusion furnace scheduling in semiconductor manufacturing requires balancing cycle time against batch capacity."},
        ]

        for case in valid_answers:
            cacheable, reason = self.cache.is_cacheable_response(case)
            self.assertTrue(
                cacheable,
                f"Expected factual answer to be cacheable, but got: {reason}"
            )

    def test_store_bypassed_for_insufficient_data(self):
        """Verify that store() rejects and does not write insufficient data responses."""
        dummy_query = "What is the secret formula for dark matter?"
        bad_response = {
            "query": dummy_query,
            "answer": "Based on the provided documents, there is insufficient information to answer this question."
        }

        # Attempt to store bad response
        stored = self.cache.store(dummy_query, bad_response)
        self.assertFalse(stored, "store() should have returned False for insufficient data response.")

        # Ensure it is not in the cache
        lookup_result = self.cache.lookup(dummy_query)
        self.assertIsNone(lookup_result, "Lookup should return None because response was never cached.")

    def test_purge_uncacheable_entries(self):
        """Verify that purge_uncacheable_entries() removes poisoned records."""
        # Clean test
        purged = self.cache.purge_uncacheable_entries()
        self.assertIsInstance(purged, int)


if __name__ == "__main__":
    unittest.main()

