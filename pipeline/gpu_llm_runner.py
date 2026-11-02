#!/usr/bin/env python3
"""GPU LLM Engine for Qwen 2.5 7B Instruct on NVIDIA A100 GPU.

Provides high-throughput, low-latency GPU inference for the advanced LLM tier,
optimized for complex multi-chunk synthesis, cross-paper comparison, and deep mathematical reasoning.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure root directory is on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import torch

LOCAL_7B_PATH = ROOT_DIR / "models/llm_7b/qwen2.5-7b-instruct"
DEFAULT_GPU_MODEL = (
    str(LOCAL_7B_PATH)
    if LOCAL_7B_PATH.exists()
    else os.getenv("VLLM_MODEL_NAME", "Qwen/Qwen2.5-7B-Instruct")
)
DEFAULT_GPU_DEVICE = os.getenv("GPU_DEVICE", "cuda:0")


class GPULLMEngine:
    """Singleton-style or on-demand GPU inference engine for Qwen 2.5 7B Instruct."""

    _instance: Optional["GPULLMEngine"] = None

    def __init__(
        self,
        model_name_or_path: str = DEFAULT_GPU_MODEL,
        device: str = DEFAULT_GPU_DEVICE,
        dtype: torch.dtype = torch.bfloat16,
    ):
        self.model_name = model_name_or_path
        self.device = torch.device(device if torch.cuda.is_available() else "cpu")
        self.dtype = dtype

        print(f"🚀 Initializing GPULLMEngine [{self.model_name}] on {self.device} ({self.dtype})...")
        from transformers import AutoModelForCausalLM, AutoTokenizer

        t0 = time.time()
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_name,
            torch_dtype=self.dtype,
            device_map={"": self.device} if self.device.type == "cuda" else None,
            low_cpu_mem_usage=True,
        )
        if self.device.type != "cuda":
            self.model.to(self.device)
            
        self.model.eval()
        self.load_duration = time.time() - t0
        print(f"✅ GPULLMEngine ready! Loaded onto {self.device} in {self.load_duration:.2f}s")

    @classmethod
    def get_instance(cls, **kwargs) -> "GPULLMEngine":
        if cls._instance is None:
            cls._instance = cls(**kwargs)
        return cls._instance

    def generate_chat(
        self,
        messages: List[Dict[str, str]],
        max_new_tokens: int = 512,
        temperature: float = 0.2,
        top_p: float = 0.8,
        do_sample: bool = False,
    ) -> Dict[str, Any]:
        """Generate response given a list of chat messages [{'role': ..., 'content': ...}]."""
        prompt_text = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = self.tokenizer([prompt_text], return_tensors="pt").to(self.device)
        input_ids = inputs["input_ids"]

        gen_kwargs: Dict[str, Any] = {
            "input_ids": inputs["input_ids"],
            "attention_mask": inputs.get("attention_mask"),
            "max_new_tokens": max_new_tokens,
            "pad_token_id": self.tokenizer.pad_token_id or self.tokenizer.eos_token_id,
        }

        if do_sample and temperature > 0:
            gen_kwargs["do_sample"] = True
            gen_kwargs["temperature"] = float(temperature)
            gen_kwargs["top_p"] = float(top_p)
        else:
            gen_kwargs["do_sample"] = False
            gen_kwargs["temperature"] = None
            gen_kwargs["top_p"] = None
            gen_kwargs["top_k"] = None

        t0 = time.time()
        with torch.no_grad():
            outputs = self.model.generate(**gen_kwargs)

        latency = time.time() - t0
        new_token_ids = outputs[0][input_ids.shape[1] :]
        generated_text = self.tokenizer.decode(new_token_ids, skip_special_tokens=True).strip()
        num_tokens = len(new_token_ids)
        tokens_per_sec = num_tokens / max(latency, 0.001)

        return {
            "text": generated_text,
            "num_tokens": num_tokens,
            "latency_seconds": round(latency, 3),
            "tokens_per_second": round(tokens_per_sec, 2),
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "device": str(self.device),
        }

    def generate_chat_stream(
        self,
        messages: List[Dict[str, str]],
        max_new_tokens: int = 512,
        temperature: float = 0.2,
        top_p: float = 0.8,
        do_sample: bool = False,
    ):
        """Generator yielding token text deltas in real-time as they generate on GPU."""
        from transformers import TextIteratorStreamer
        from threading import Thread

        prompt_text = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = self.tokenizer([prompt_text], return_tensors="pt").to(self.device)
        streamer = TextIteratorStreamer(self.tokenizer, skip_prompt=True, skip_special_tokens=True)

        gen_kwargs: Dict[str, Any] = {
            "input_ids": inputs["input_ids"],
            "attention_mask": inputs.get("attention_mask"),
            "max_new_tokens": max_new_tokens,
            "pad_token_id": self.tokenizer.pad_token_id or self.tokenizer.eos_token_id,
            "streamer": streamer,
        }

        if do_sample and temperature > 0:
            gen_kwargs["do_sample"] = True
            gen_kwargs["temperature"] = float(temperature)
            gen_kwargs["top_p"] = float(top_p)
        else:
            gen_kwargs["do_sample"] = False
            gen_kwargs["temperature"] = None
            gen_kwargs["top_p"] = None
            gen_kwargs["top_k"] = None

        thread = Thread(target=self.model.generate, kwargs=gen_kwargs)
        thread.start()

        for chunk_text in streamer:
            yield chunk_text

        thread.join()

    @staticmethod
    def build_rag_messages(
        question: str,
        context_chunks: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Constructs a synthesis-oriented, comprehensive chat prompt for complex hard queries."""
        if not system_prompt:
            system_prompt = (
                "You are an advanced institutional AI research assistant specialized in academic synthesis, "
                "comparative analysis, and mathematical proofs.\n\n"
                "Your objective is to provide comprehensive, accurate, and deeply reasoned answers using strictly "
                "the provided context chunks from research theses and academic literature.\n\n"
                "Strict Guidelines:\n"
                "1. Synthesize trade-offs, methodologies, and cross-cutting findings clearly across multiple documents.\n"
                "2. When comparing concepts, systematically evaluate advantages, limitations, and empirical results.\n"
                "3. Cite your sources clearly using the document Title or Chunk ID for each key finding (e.g., [Source: <Title>]).\n"
                "4. If the provided context is genuinely insufficient to complete a proof or comparison, explicitly specify what is missing.\n"
                "5. Maintain academic rigor, precision, and clarity."
            )

        context_blocks = []
        for i, chunk in enumerate(context_chunks, 1):
            title = chunk.get("title", "Untitled Document")
            chunk_id = chunk.get("chunk_key", f"chunk_{i}")
            content = chunk.get("content", "").strip()
            header = f"--- Document [{i}]: {title} ({chunk_id}) ---"
            context_blocks.append(f"{header}\n{content}")

        context_str = "\n\n".join(context_blocks)
        user_content = (
            f"### Retrieved Academic Context:\n{context_str}\n\n"
            f"### Complex Research Question:\n{question}\n\n"
            f"### In-Depth Synthesis & Grounded Answer:"
        )

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

    def answer_rag(
        self,
        question: str,
        context_chunks: List[Dict[str, Any]],
        max_new_tokens: int = 512,
        temperature: float = 0.2,
        system_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Convenience method combining prompt construction and GPU answer generation."""
        messages = self.build_rag_messages(question, context_chunks, system_prompt)
        return self.generate_chat(
            messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            do_sample=(temperature > 0),
        )


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Test Qwen 2.5 7B GPU Inference Engine")
    parser.add_argument("prompt", nargs="?", default="Explain the trade-offs of continuous batching in LLM serving.")
    parser.add_argument("--device", default=DEFAULT_GPU_DEVICE, help="GPU device (default: cuda:0)")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--temp", type=float, default=0.2)
    parser.add_argument("--stream", action="store_true", help="Stream response")
    args = parser.parse_args()

    engine = GPULLMEngine(device=args.device)

    messages = [
        {"role": "system", "content": "You are an expert AI systems researcher."},
        {"role": "user", "content": args.prompt},
    ]

    print("\n" + "=" * 60)
    print(f"Query: {args.prompt}")
    print(f"Device: {engine.device} | Model: {engine.model_name}")
    print("=" * 60)

    if args.stream:
        print("\nStreaming response from GPU...")
        for token in engine.generate_chat_stream(messages, max_new_tokens=args.max_tokens, temperature=args.temp):
            sys.stdout.write(token)
            sys.stdout.flush()
        print("\n")
    else:
        res = engine.generate_chat(messages, max_new_tokens=args.max_tokens, temperature=args.temp)
        print(f"\nResponse:\n{res['text']}\n")
        print(f"📊 Stats: {res['num_tokens']} tokens in {res['latency_seconds']}s ({res['tokens_per_second']} tok/s)")


if __name__ == "__main__":
    main()

