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

    def test_zero_chunks_unretrieved_documents_rejected(self):
        """Verify that responses with 0 retrieved documents or fallback phrases are strictly rejected."""
        unretrieved_cases = [
            {"answer": "No specific institutional repository theses were found on this topic.", "chunks": []},
            {"answer": "No specific institutional repository theses were found on this topic."},
            {"answer": "A detailed 100 character explanation of how algorithms work in computer science.", "chunks": []},
            {"answer": "No specific institutional repository theses were found on this topic. Based on general technical knowledge:\n\nHere is an explanation.", "chunks": []},
            {"answer": "No specific institutional repository theses were found on this topic. Based on general academic knowledge:\n\nHere is an explanation.", "chunks": []},
            {"answer": "No specific institutional repository theses were found on this topic. Based on general academic knowledge:\n\nHere is an explanation."},
            {"answer": "No relevant documents were found in the knowledge base matching your query."},
            {"answer": "A detailed 100 character explanation of how algorithms work in computer science.", "num_chunks": 0},
            {"answer": "A detailed 100 character explanation of how algorithms work in computer science.", "citations": []},
        ]

        for case in unretrieved_cases:
            cacheable, reason = self.cache.is_cacheable_response(case)
            self.assertFalse(
                cacheable,
                f"Expected unretrieved document response '{case}' to be uncacheable, but got cacheable=True (reason: {reason})"
            )

    def test_grounded_factual_answers_allowed(self):
        """Verify that legitimate grounded answers with non-empty chunks are marked cacheable."""
        valid_answers = [
            {
                "answer": "Vermiculite is a hydrous phyllosilicate mineral that undergoes significant expansion when heated, widely used in thermal insulation.",
                "chunks": [{"doc_id": "thesis_101", "text": "Vermiculite mineral analysis"}],
            },
            {
                "answer": "In magnetohydrodynamics, Rayleigh-Taylor instability develops when a heavier fluid is accelerated against a lighter fluid across a magnetic interface.",
                "chunks": [{"doc_id": "thesis_202", "text": "MHD Rayleigh-Taylor experiments"}],
            },
            {
                "answer": "Diffusion furnace scheduling in semiconductor manufacturing requires balancing cycle time against batch capacity.",
                "chunks": [{"doc_id": "thesis_303", "text": "Semiconductor manufacturing dispatch rules"}],
            },
        ]

        for case in valid_answers:
            cacheable, reason = self.cache.is_cacheable_response(case)
            self.assertTrue(
                cacheable,
                f"Expected factual answer with retrieved chunks to be cacheable, but got: {reason}"
            )

    def test_store_bypassed_for_zero_chunks(self):
        """Verify that store() rejects and does not write answers when 0 document chunks were retrieved."""
        dummy_query = "What is the secret formula for dark matter?"
        zero_chunk_response = {
            "query": dummy_query,
            "answer": "No specific institutional repository theses were found on this topic. Based on general academic knowledge:\n\nDark matter is hypothetical.",
            "chunks": [],
        }

        # Attempt to store zero-chunk response
        stored = self.cache.store(dummy_query, zero_chunk_response)
        self.assertFalse(stored, "store() should have returned False for zero-chunk unretrieved response.")

        # Ensure it is not in the cache
        lookup_result = self.cache.lookup(dummy_query)
        self.assertIsNone(lookup_result, "Lookup should return None because response was never cached.")

    def test_exact_fast_path_purges_poisoned_zero_chunk_entries(self):
        """Verify that fast exact lookup purges poisoned zero-chunk entries upon encounter."""
        import hashlib, json
        dummy_query = "explain quantum teleportation protocols in detail"
        norm_q = dummy_query.strip().lower()
        exact_key = f"{self.cache.key_prefix}exact:{hashlib.sha256(norm_q.encode('utf-8')).hexdigest()}"

        # Manually seed a stale/poisoned entry with zero chunks
        poison_payload = {
            "prompt": dummy_query,
            "response": {
                "answer": "No specific institutional repository theses were found on this topic. Based on general technical knowledge:\n\nQuantum teleportation is...",
                "chunks": [],
            },
        }
        self.cache.sync_client.set(exact_key, json.dumps(poison_payload))

        # Lookup should detect uncacheable payload, reject it, and eradicate key from Redis
        res = self.cache.lookup(dummy_query)
        self.assertIsNone(res, "Lookup should reject exact match with zero chunks.")
        self.assertIsNone(self.cache.sync_client.get(exact_key), "Poisoned exact key should be deleted from Redis.")

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

