import asyncio
import json
import logging
from gateway.router.complexity_scorer import ComplexityScorer
from gateway.router.dispatcher import ModelTier, RequestDispatcher

async def run_sweep():
    with open("evaluation/datasets/golden_eval_set.json") as f:
        records = json.load(f)
    scorer = ComplexityScorer()
    print("\nAlpha Sweep Results:")
    for a in [0.2, 0.4, 0.6, 0.8]:
        d = RequestDispatcher(alpha=a)
        cpu = 0
        for record in records:
            score = await scorer.score(record["query"])
            if d.decide(score).target_tier == ModelTier.CPU_SLM:
                cpu += 1
        print(f"Alpha: {a:.2f} -> SLM: {cpu}/{len(records)} queries")

if __name__ == "__main__":
    asyncio.run(run_sweep())
