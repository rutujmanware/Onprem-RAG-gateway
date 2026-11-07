"""Prompt Injection Defense and Context Fencing Utility for AskIISc RAG Gateway.

Implements defense-in-depth against:
1. Direct Prompt Injection: User attempts to override system prompts or inject delimiter escapes.
2. Indirect Prompt Injection: Malicious text inside indexed documents attempting delimiter breakout.
"""

import re
from typing import Any, Dict, List


class PromptInjectionAlert(ValueError):
    """Raised when an adversarial prompt injection pattern is detected."""
    pass


INJECTION_PATTERNS = [
    # 1. Broad instruction overrides (handles "the", "all the", "any", typos like "recieved", relative clauses)
    r"(?:ignore|disregard|forget|drop|reset)\s+(?:all\s+)?(?:the\s+|any\s+|your\s+)?(?:previous|prior|above|system|existing)?\s*(?:instructions|rules|prompts|directives|commands|constraints|guidelines)",
    r"(?:ignore|disregard|forget)\s+(?:all\s+)?(?:the\s+)?(?:instructions|rules|directives)\s+(?:that\s+)?you\s+(?:received|recieved|got|were\s+given)",
    r"(?:forget|disregard)\s+(?:what|everything|all|anything)\s+(?:i|was|we|you)\s+(?:said|told|mentioned|instructed|recieved|received|were\s+told)",
    r"only\s+this\s+instruction\s+is\s+what\s+you\s+(?:are\s+going\s+to|will)\s+follow",
    r"bypass\s+(?:all\s+)?(?:safety|content|system|security)\s+(?:filters|rules|prompts|guidelines)",

    # 2. Delimiter & Special Token Escapes
    r"</?(?:system|prompt|context|context_data|s|im_start|im_end)>",
    r"<\|im_start\|>",
    r"<\|im_end\|>",
    r"\[/?INST\]",
    r"system\s*(?:prompt|directive)?\s*override",

    # 3. Persona / Role Hijacking (catches "you are now X", "you are Merra", "act as", "pretend to be", "name yourself X")
    r"(?:name|call)\s+yourself\s+[A-Za-z]+",
    r"(?:you\s+are|you're)\s+(?:now\s+)?(?:called\s+|named\s+)?(?:dan|merra|unfiltered|jailbroken|godmode|devmode)",
    r"(?:you\s+are|you're)\s+now\s+(?:an?\s+)?(?:unrestricted|jailbroken|unfiltered|evil|rogue)",
    r"(?:act\s+as|pretend\s+to\s+be|assume\s+the\s+role\s+of|roleplay\s+as)\s+(?:dan|merra|an?\s+unrestricted|a\s+different|another|an?\s+evil)",
    r"(?:from\s+now\s+on|going\s+forward)[,\s]+(?:you\s+are|answer\s+as|act\s+as|pretend\s+to\s+be|roleplay)",
    r"you\s+will\s+now\s+roleplay\s+as",

    # 4. Sensitive System File & Secret Extraction
    r"(?:print|show|dump|reveal|display|output|read|cat|extract)\s+(?:out\s+)?(?:the\s+)?(?:env|\.env|environment|secret|key|config|password|token)\s*(?:file)?",
    r"(?:\.env|\benv\s+file)\b.*(?:content|print|dump|read|cat|show|display|reveal|extract|output)",
    r"(?:print|show|dump|reveal|display|output|read|cat|extract)\s+.*(?:\.env|\benv\s+file)\b",
]

COMPILED_INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


def sanitize_and_fence_prompt(prompt: str) -> str:
    """Validate prompt against adversarial injection signatures and encapsulate it.

    Raises:
        PromptInjectionAlert: If an injection attempt or delimiter escape is detected.
    Returns:
        Fenced prompt wrapped in a safe user block.
    """
    clean_prompt = prompt.strip()
    for pattern in COMPILED_INJECTION_RE:
        if pattern.search(clean_prompt):
            raise PromptInjectionAlert(f"NFR-7 Alert: Adversarial prompt injection detected.")

    return f"```user\n{clean_prompt}\n```"


def fence_context_chunks(chunks: List[Dict[str, Any]]) -> str:
    """Format and fence retrieved knowledge base chunks inside strict XML boundary tags.
    Escapes any closing boundary tags within corpus text to prevent delimiter breakout.
    """
    blocks = []
    for i, c in enumerate(chunks, 1):
        title = str(c.get("title") or "Untitled Document").replace("<", "&lt;").replace(">", "&gt;")
        raw_content = str(c.get("content") or "").strip()
        # Escape any delimiter tag attempts inside document content
        safe_content = raw_content.replace("</context_data>", "[escaped_tag]")
        blocks.append(f"<document id=\"{i}\" title=\"{title}\">\n{safe_content}\n</document>")

    inner = "\n\n".join(blocks)
    return f"<context_data>\n{inner}\n</context_data>"

