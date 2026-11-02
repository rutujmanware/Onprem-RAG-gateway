import asyncio
try:
    import pytest
    asyncio_test = pytest.mark.asyncio
except ImportError:
    def asyncio_test(f):
        return f

from gateway.router.complexity_scorer import ComplexityScorer, extract_query_scalars

def test_extract_query_scalars():
    query = "Compare the optimization of polymer membranes versus conventional sensors?"
    scalars = extract_query_scalars(query)
    assert len(scalars) == 6
    # Should detect compare / versus
    assert scalars[4] == 1.0
    # Words and chars
    assert scalars[0] > 0.0

@asyncio_test
async def test_complexity_scorer_heuristic_fallback():
    scorer = ComplexityScorer(model_path="non_existent_model.onnx")
    # Simple query
    score_simple = await scorer.score("What is an algorithm?")
    assert 0.01 <= score_simple <= 0.99

    # Complex query
    score_hard = await scorer.score("Compare and contrast the distributed architecture and tradeoff")
    assert score_hard > score_simple


if __name__ == "__main__":
    test_extract_query_scalars()
    asyncio.run(test_complexity_scorer_heuristic_fallback())
    print("Complexity scorer tests passed.")


