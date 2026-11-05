#!/usr/bin/env python3
"""Unit tests for Query Metadata Extractor and Hybrid Fallback Retrieval."""

import unittest
from pipeline.query_metadata_extractor import QueryMetadataExtractor
from pipeline.retriever import RedisChunkRetriever


class TestQueryMetadataExtractor(unittest.TestCase):

    def setUp(self):
        self.extractor = QueryMetadataExtractor()
        self.retriever = RedisChunkRetriever()

    def test_extract_advisor_and_department(self):
        """Verify extraction of professor/advisor name and canonical department."""
        q = "What research did Prof. Ramamurthy publish on vermiculite in Materials Engineering?"
        meta = self.extractor.extract(q)
        self.assertTrue(meta.has_filters)
        self.assertEqual(meta.filters.get("advisor"), "Ramamurthy")
        self.assertEqual(meta.filters.get("department"), "Materials Engineering")
        self.assertIn("vermiculite", meta.clean_query.lower())
        self.assertNotIn("ramamurthy", meta.clean_query.lower())
        self.assertNotIn("materials engineering", meta.clean_query.lower())

    def test_extract_alias_csa_and_degree(self):
        """Verify abbreviation alias 'CSA' canonicalizes to 'Computer Science and Automation'."""
        q = "Show me PhD theses on deep learning for vision in CSA department"
        meta = self.extractor.extract(q)
        self.assertTrue(meta.has_filters)
        self.assertEqual(meta.filters.get("department"), "Computer Science and Automation")
        self.assertEqual(meta.filters.get("degree"), "PhD")
        self.assertIn("deep learning for vision", meta.clean_query.lower())

    def test_extract_date_and_aerospace(self):
        """Verify year extraction and 'Aerospace' canonicalization."""
        q = "Aerospace Engineering theses from 2021 on turbulent boundary layer"
        meta = self.extractor.extract(q)
        self.assertTrue(meta.has_filters)
        self.assertEqual(meta.filters.get("department"), "Aerospace Engineering")
        self.assertEqual(meta.filters.get("date"), "2021")
        self.assertIn("turbulent boundary layer", meta.clean_query.lower())

    def test_author_extraction(self):
        """Verify author attribution when phrased as 'by Author'."""
        q = "Studies on barium titanate by Ramamurthy"
        meta = self.extractor.extract(q)
        self.assertTrue(meta.has_filters)
        self.assertEqual(meta.filters.get("author"), "Ramamurthy")
        self.assertIn("barium titanate", meta.clean_query.lower())

    def test_no_metadata_preserves_query(self):
        """Verify that general conceptual queries without metadata remain untouched."""
        q = "What is vermiculite and what are its primary uses?"
        meta = self.extractor.extract(q)
        self.assertFalse(meta.has_filters)
        self.assertEqual(meta.filters, {})
        self.assertEqual(meta.clean_query, q)

    def test_filter_expression_builder(self):
        """Verify RediSearch filter clause construction with tokenized terms."""
        expr = self.retriever._build_filter_expression({
            "department": "Materials Engineering",
            "advisor": "Ramamurthy",
            "date": 2021,
        })
        self.assertIn("@department:(Materials Engineering)", expr)
        self.assertIn("@advisor:(Ramamurthy)", expr)
        self.assertIn("@date:[2021 2021]", expr)

    def test_filter_expression_sanitizes_punctuation(self):
        """Verify RediSearch special characters do not break filter syntax."""
        expr = self.retriever._build_filter_expression({
            "department": "Materials Engineering (MatEng) / View Item"
        })
        self.assertNotIn("(", expr.split("@department:")[1].split(")")[1])
        self.assertIn("@department:(Materials Engineering MatEng View Item)", expr)


if __name__ == "__main__":
    unittest.main()

