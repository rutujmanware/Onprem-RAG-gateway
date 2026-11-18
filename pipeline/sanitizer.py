"""Prompt Injection Defense and Context Fencing Utility for AskIISc RAG Gateway.

Implements defense-in-depth against:
1. Direct Prompt Injection: User attempts to override system prompts or inject delimiter escapes.
2. Indirect Prompt Injection: Malicious text inside indexed documents attempting delimiter breakout.
"""

import re
from typing import Any, Dict, List, Optional


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
    r"system\s*(?:prompt|directive)\s*override|system\s+override\s*[:!\-]",  # not "a system override in a reactor"

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

# ---------------------------------------------------------------------------------------------------------
# DEFECT-32 (NFR-7): broader detection. Designed on the design half of tests/data/injection_eval/ only and
# measured on its frozen, reviewed held-out half (scripts/eval_injection_fence.py measure).
# Ambiguous academic words (rules, constraints, filters, environment variables, debug mode, ...) only count
# when the message addresses the assistant itself (your / the assistant / the system prompt / the server ...).
_W = r"(?:\W+\w+){0,%d}?\W+"                       # up to N filler words
_YOU = (r"(?:your|yourself|you|the\s+assistant(?:'s)?|this\s+assistant|the\s+(?:ai|bot|model|chatbot|system|server|"
        r"gateway)(?:'s)?|its)")
_RULES = (r"(?:instructions?|rules?|guidelines?|directives?|system\s*prompt|prompts?|programming|safeguards?|"
          r"guardrails?|safety\s+(?:measures|checks|settings|features|protocols?|filters?)|security\s+(?:measures|"
          r"checks|controls|settings)|content\s+(?:polic(?:y|ies)|filters?))")
# words that are also ordinary academic vocabulary: only when they belong to the assistant
_RULES_WEAK = (r"(?:restrictions?|limitations?|constraints?|polic(?:y|ies)|filters?|configuration|settings|setup|"
               r"parameters|behaviou?r|guidance)")
_OWN = r"(?:your|its|the\s+assistant's|this\s+assistant's|the\s+(?:ai|bot|model|chatbot)'s|our\s+assistant's)"
_OVERRIDE_VERB = (r"(?:ignore|disregard|forget|override|bypass|skip|abandon|discard|stop\s+"
                  r"(?:following|considering|applying|obeying)|disable|deactivate|turn\s+off|switch\s+off|"
                  r"lift|loosen|suspend|circumvent|get\s+around|work\s+around|set\s+aside|"
                  r"no\s+longer\s+(?:follow|obey|apply))")
_ANCHOR = (r"(?:your|the|all|any|these|those|previous|prior|earlier|above|preceding|existing|current|default|normal|"
           r"usual|standard|system|safety|content|security|internal|initial|original|built[\s-]?in|programmed|"
           r"earlier|last|every)")
_SECRET = (r"(?:api[\s_-]?keys?|secret[\s_-]?keys?|session[\s_-]?secrets?|private\s+keys?|access\s+tokens?|"
           r"auth(?:entication|orization)?\s+tokens?|admin[\s_-]?(?:api[\s_-]?)?keys?|passwords?|credentials?|"
           r"\.env\b|env(?:ironment)?\s+(?:file|variables?|vars?|settings)|config(?:uration)?\s+(?:files?|settings)|"
           r"connection\s+strings?|/etc/(?:passwd|shadow)|ssh\s+keys?|secrets?\s+(?:stored|of\s+the\s+server)|"
           r"server(?:'s)?\s+secrets?|log\s+files?)")
_ASK = (r"(?:give|show|tell|reveal|share|provide|print|display|output|list|dump|leak|read|cat|fetch|retrieve|"
        r"confirm|access|locate|send|disclose|expose|what\s+(?:is|are)|where\s+(?:is|are)|let\s+me\s+(?:see|have|know)|"
        r"may\s+i\s+(?:see|have)|can\s+i\s+(?:see|have|get)|look\s+at|check)")
_ADDRESSED = (r"(?:your|yours|the\s+(?:assistant|server|system|gateway|bot|ai|model|deployment|backend|host|"
              r"application|rag\s+system|thesis\s+(?:assistant|system|database))(?:'s)?|this\s+(?:assistant|server|"
              r"system|deployment)|currently\s+(?:set|used|configured)|used\s+by\s+(?:you|the\s+assistant)|you\s+(?:use|have|run))")

_PATTERNS_V2 = {
    "override": [
        _OVERRIDE_VERB + _W % 4 + _ANCHOR + _W % 2 + _RULES + r"\b",
        _OVERRIDE_VERB + _W % 4 + _OWN + _W % 2 + _RULES_WEAK + r"\b",
        _OVERRIDE_VERB + r"\s+(?:that|this|them|it)(?:\s+(?:rule|instruction|guidance|restriction))?\s+(?:and|,|then)\s+",
        r"(?:new|updated|different|my\s+own)\s+(?:instructions|rules|system\s+prompt)\s*(?::|follow|are|apply)",
        _OVERRIDE_VERB + _W % 3 + r"(?:previous|prior|earlier|above|system|your)(?:instructions?|rules?|prompts?|guidelines)",
        r"(?:update|adjust|modify|change|rewrite|edit|alter|replace)\s+(?:the\s+assistant's|your|its|the\s+system's)\s+"
        r"(?:current\s+|previous\s+|original\s+|initial\s+|system\s+)?(?:instructions|system\s+prompt|prompt|rules|"
        r"guidelines|programming)",
        r"(?:you\s+(?:must|should|will)\s+)?(?:now\s+)?follow\s+(?:only\s+)?(?:my|these|the\s+following)\s+(?:new\s+)?instructions",
        r"(?:from\s+now\s+on|henceforth|for\s+the\s+rest\s+of\s+(?:this|our)\s+(?:conversation|chat))\b",
        r"(?:despite|regardless\s+of|notwithstanding)\s+(?:your|the|its|any)\s+(?:\w+\s+)?(?:rules|instructions|"
        r"guidelines|restrictions|programming|polic(?:y|ies))",
    ],
    "persona": [
        r"\b(?:do\s+anything\s+now|evil\s*(?:twin|confidant|mode)|developer\s+mode|dev\s*mode|god\s*mode|"
        r"jailbr(?:eak|oken)\s+mode|sudo\s+mode|admin\s+mode|maintenance\s+mode|debug\s+mode|unrestricted\s+mode|"
        r"no[\s-]+rules?\s+mode|'no\s+rules'|\"no\s+rules\")\b",
        # jailbreak personas by name only when the assistant is addressed as them
        r"(?:you\s+are(?:\s+now)?|you're(?:\s+now)?|act\s+as|become|pretend\s+to\s+be|as|called|named|alter\s+ego,?)\s+"
        r"['\"]?(?:dan|stan|dude|aim|jailbreak(?:gpt)?|evilbot|betterdan)['\"]?\b",
        r"\b(?:dan|stan|dude|aim)\s+(?:mode|persona|prompt|jailbreak)\b",
        r"\b(?:you\s+are\s+)?(?:now\s+)?jailbroken\b",
        r"(?:act|behave|respond|operate|answer|function)\s+(?:as|like)\s+(?:an?\s+)?(?:unrestricted|unfiltered|uncensored|"
        r"unlimited|rogue|evil|disobedient|malicious|unaligned)",
        r"(?:you|yourself)\b" + _W % 4 + r"(?:disobedient|rogue|malicious|evil|unrestricted|unaligned|uncensored)\s+"
        r"(?:ai|assistant|bot|model|chatbot|version)",
        r"(?:become|be|pretend\s+to\s+be|turn\s+into)\s+(?:an?\s+)?(?:disobedient|rogue|malicious|evil|unrestricted|"
        r"unaligned|uncensored)\s+(?:ai|assistant|bot|model)",
        r"(?:you\s+are|you're|become|turn\s+into|transform\s+into|morph\s+into|switch\s+(?:to|into)|enter|rewrite\s+"
        r"yourself\s+as|pretend\s+(?:to\s+be|you\s+are)|roleplay\s+as|role[\s-]play\s+as|play\s+the\s+role\s+of)"
        + _W % 4 + r"(?:unrestricted|unfiltered|uncensored|unlimited|without\s+(?:any\s+)?(?:limits|limitations|"
        r"restrictions|rules|filters)|no\s+(?:limits|limitations|restrictions|rules|filters)|a\s+new\s+persona|"
        r"different\s+persona|fictional\s+character)",
        r"(?:in\s+a\s+state\s+of|imagine\s+you\s+are\s+in)\s+['\"]?no\s+rules",
        r"test\s+(?:your|the\s+assistant's)\s+(?:limits|boundaries|restrictions)|(?:answer|respond)\s+(?:in\s+)?any\s+way\s+you\s+"
        r"(?:want|like)",
        r"(?:role[\s-]?play(?:ing)?\s+game|new\s+persona)\b" + _W % 6 + r"(?:freely|any\s+way|without|no\s+(?:rules|limits))",
    ],
    "no_restrictions": [
        r"(?:unrestricted|unfiltered|uncensored|unlimited|unconstrained)\s+(?:content|access|mode|answers?|responses?|"
        r"output|ai|version|persona|assistant|access\s+to)",
        r"(?:without|with\s+no|no)\s+(?:any\s+)?(?:safety|content|security)\s+(?:filters?|measures|restrictions|"
        r"checks|polic(?:y|ies)|guardrails|controls)",
        r"(?:without|with\s+no|no)\s+(?:any\s+)?(?:restrictions|limits|limitations|filters|censorship|guardrails|"
        r"rules)\b" + _W % 6 + r"(?:answer|respond|reply|tell|give|run|operate|test|act|questions?)",
        r"(?:answer|respond|reply|tell\s+me|give\s+me|help\s+me|run|operate|test|act)\b" + _W % 8
        + r"(?:without|with\s+no|no)\s+(?:any\s+)?(?:restrictions|limits|limitations|filters|censorship|guardrails|"
        r"rules|reservations|safety)",
        r"(?:loosen|relax|lift|lower|adjust|reduce|weaken)\s+(?:the\s+|your\s+|its\s+)?(?:safety\s+|content\s+)?"
        r"(?:filters?|content\s+polic(?:y|ies)|safety\s+(?:measures|settings))",
        r"(?:break|violate)\s+(?:its|your|the\s+assistant's|their|normal|usual|the)\s+(?:normal\s+|usual\s+)?rules",
        r"(?:might|may|would|will)\s+violate\s+(?:its|your|the)\s+(?:rules|polic)",
        r"(?:relax|relaxed|lower|lowered|adjust|adjusted|modify|modified|tweak|tweaked|deactivate|deactivated|disable|"
        r"disabled|turn\s+off|loosen|loosened|lift|lifted|remove|removed|bypass|reduce|reduced|suspend|suspended)\b"
        + _W % 4 + r"(?:safety|content|security)\s+(?:filters?|polic(?:y|ies)|settings|thresholds?|measures|checks|"
        r"restrictions)",
        r"(?:safety|content|security)\s+(?:filters?|polic(?:y|ies)|settings|thresholds?|measures|checks|restrictions)"
        + _W % 4 + r"(?:relaxed|lowered|adjusted|modified|tweaked|deactivated|disabled|turned\s+off|loosened|lifted|"
        r"removed|bypassed|reduced|suspended)",
        r"(?:deactivate|disable|turn\s+off|switch\s+off|remove)\s+(?:any|all|the)\s+(?:content\s+)?filtering",
        r"(?:drop|dropped|remove|removed|switch\s+off)\s+(?:the\s+|your\s+|its\s+|all\s+)?(?:safety|security|content)\s+"
        r"(?:measures|filters?|checks|polic(?:y|ies)|restrictions)",
        r"(?:filters?|restrictions|rules|safety\s+measures|polic(?:y|ies))\b" + _W % 4 + r"(?:just|only)\s+(?:this|for\s+this)\s+"
        r"(?:once|one\s+time|time)",
        r"(?:just|only)\s+(?:this|for\s+this)\s+(?:once|one\s+time)\b" + _W % 6 + r"(?:filters?|restrictions|rules|"
        r"safety\s+measures|polic(?:y|ies))",
        r"filters?\s+(?:be|to\s+be)\s+(?:adjusted|relaxed|lowered|removed|disabled|loosened)\s+(?:to\s+(?:permit|"
        r"allow)|for\s+(?:this|our|my))",
    ],
    "prompt_extraction": [
        r"(?:system|hidden|initial|original|internal|developer|pre)[\s-]?(?:prompt|instructions?|message)s?\b",
        _ASK + _W % 5 + r"(?:your|the)\s+(?:exact\s+|full\s+|complete\s+|original\s+|initial\s+|hidden\s+)?"
        r"(?:instructions|prompt|configuration|setup)\b",
        r"(?:instructions|prompt)\s+(?:you\s+(?:were|are|have\s+been)\s+given|you\s+(?:received|got|follow))",
        r"(?:repeat|print|output|recite)\s+(?:everything|all|the\s+text|the\s+words)\s+(?:above|before|preceding)",
        r"(?:what\s+(?:is|are)|show\s+me)\s+(?:your|the)\s+(?:current\s+)?prompt\b",
        r"(?:instructions|guidance|guidelines|rules|directives)\s+(?:that\s+)?you\s+(?:receive|received|were\s+given|are\s+"
        r"given|have\s+been\s+given|follow|operate\s+under)",
        r"(?:default|internal|hidden)\s+(?:settings|parameters|configuration)\s+(?:that\s+)?you\s+(?:use|have|run)",
        r"\bare\s+you\s+(?:instructed|programmed|configured|told)\s+to\b",
        r"(?:specific\s+|exact\s+|initial\s+|default\s+|hidden\s+|internal\s+)?instructions\s+(?:provided|given|"
        r"supplied|fed)\s+to\s+(?:the|your|this)\s+(?:\w+\s+){0,3}?(?:system|assistant|model|bot|ai)\b",
        r"(?:default|initial|hidden|internal)\s+(?:settings|parameters|setup|configuration)\s+(?:for|of|set\s+for)\s+"
        r"(?:the|your|this)\s+(?:\w+\s+){0,4}?(?:system|assistant|prompt|model)\b",
    ],
    "secret_exfiltration": [
        r"^(?=.*?" + _ADDRESSED + r").*?" + _ASK + _W % 8 + _SECRET,
        r"^(?=.*?" + _ADDRESSED + r").*?" + _SECRET + _W % 8 + r"(?:contents?|values?|are\s+(?:set|stored|used))",
        r"(?:your|the\s+(?:assistant's|server's|system's|gateway's))\s+(?:\w+\s+)?" + _SECRET,
        r"(?:give|provide|send|share|show|tell|hand)\s+(?:me|us)\s+(?:with\s+)?the\s+(?:\w+\s+){0,3}?" + _SECRET,
        r"(?:in|of|from)\s+the\s+\.env\s+file|\.env\s+file\s+(?:of|on|for|used\s+by)\s+(?:the|this|your)",
    ],
    "delimiter": [
        r"<<+\s*/?\s*sys\s*>+", r"\[/?\s*inst\s*\]", r"<\|[a-z_]+\|>", r"</?\s*(?:system|sys|s|context|"
        r"context_data|prompt|instructions?|assistant|user)\s*>", r"^\s*#{2,}\s*(?:system|instruction|assistant)\b",
        r"(?:^|\n)\s*(?:system|assistant)\s*:", r"#{2,}\s*(?:system|instruction|instructions|assistant|response)\s*:",
        r"\brole\s*[:=]\s*[\"'][a-z_ ]{2,30}[\"']", r"\[(?:[A-Z]{2,12})\]\s*role\s*:", r"\bend\s+of\s+(?:the\s+)?(?:documents?|context|instructions|"
        r"system\s+prompt)\b", r"\bbegin\s+(?:new\s+)?(?:system|instructions|prompt)\b",
        r"[\"']role[\"']\s*:\s*[\"']system[\"']",
    ],
    "output_hijack": [
        r"\bpwned\b|\binjection_success\b|\btoken_leak\w*",
        r"(?:always|from\s+now\s+on|every\s+time)\b" + _W % 6 + r"(?:answer|respond|reply|say|print|output|return|"
        r"start|begin|end|prepend|append|state)\b",
        r"(?:answer|respond\s+to|reply\s+to|return)\s+(?:every|each|all|any)\s+(?:\w+\s+)?(?:questions?|quer(?:y|ies)|"
        r"prompts?)\s+with",
        r"(?:every|each|all|any)\s+(?:\w+\s+)?(?:questions?|quer(?:y|ies)|answers?|responses?)\s+(?:should|must|will)\s+"
        r"(?:return|be|start|begin|say)",
        r"(?:prepend|append)\b" + _W % 8 + r"(?:to|at\s+the\s+(?:start|end)\s+of)\s+(?:your|every|each|all|any)\b",
        r"(?:respond|reply|answer)\s+(?:only|just|exclusively|solely)\s+with",
        r"(?:make|have)\s+(?:all\s+)?your\s+(?:answers|responses|replies)\s+(?:start|begin|end)\s+with",
        r"\b(?:answer|response)\s+to\s+(?:all|every|any)\s+(?:\w+\s+)?questions\s+is\s+(?:always|now)",
        r"['\"‘“][^'\"’”]{3,200}['\"’”]" + _W % 10 + r"(?:regardless|no\s+matter\s+what|for\s+(?:all|every|each|any)\s+"
        r"(?:\w+\s+)?(?:questions?|quer(?:y|ies)|inputs?)|to\s+(?:all|every|each|any)\s+(?:\w+\s+)?(?:questions?|"
        r"quer(?:y|ies)))",
        r"(?:every|each|all|any)\s+(?:\w+\s+)?(?:questions?|quer(?:y|ies)|responses?|answers?|outputs?)\s+(?:is|are|"
        r"should\s+be|must\s+be|will\s+be)?\s*(?:answered|result\s+in|be\s+answered|always\s+(?:be|say))" + _W % 6
        + r"['\"‘“]",
        r"(?:responses?|answers?|outputs?)\s+(?:are|is|be)\s+always\s+the\s+same",
        r"regardless\s+of\s+(?:the|what|which|my|any)\s+(?:\w+\s+)?(?:question|query|input|is\s+asked)",
        r"(?:prepend|append|start|begin|end|prefix|suffix)\s+(?:your|every|each|all|the)\s+(?:answers?|responses?|"
        r"replies|outputs?)\s+with",
        r"(?:answer|respond|reply|return|say)\b" + _W % 8 + r"(?:for|to)\s+(?:all|every|each|any)\s+(?:\w+\s+)?"
        r"(?:questions?|quer(?:y|ies)|prompts?|inputs?)\b",
        r"(?:all|every|each|any)\s+(?:\w+\s+)?(?:questions?|quer(?:y|ies)|responses?|answers?)\b" + _W % 10
        + r"(?:answered\s+with|include|contain|say|state|end\s+with|start\s+with)\s+['\"‘“]",
        r"(?:fixed|same|identical|pre[\s-]?programmed|canned)\s+(?:answer|response|reply|text|output)s?\s+(?:for|to)\s+"
        r"(?:all|every|each|any)",
        r"\b(?:the\s+)?same\s+(?:response|answer|text|output)\s+(?:for|to)\s+(?:each|every|all|any)",
        r"\binjected?\s*:",
    ],
    "context_dismissal": [
        r"(?:based|relying|rely)\s+(?:solely|only|purely|entirely|just)?\s*on\s+your\s+own\s+(?:knowledge|"
        r"understanding|insights?|experience|training|views?|opinions?)",
        r"(?:use|using|from|with|give\s+(?:me\s+)?)\s+your\s+own\s+(?:knowledge|understanding|insights?|training)",
        r"(?:ignore|disregard|forget|skip|discard|don't\s+use|do\s+not\s+use|stop\s+using|without\s+(?:using|"
        r"relying\s+on|referencing|referring\s+to|citing|consulting))\s+(?:any\s+of\s+)?(?:the|these|those|your)\s+"
        r"(?:provided\s+|retrieved\s+|given\s+|above\s+|cited\s+|attached\s+|supplied\s+)?(?:documents?|context|"
        r"sources|citations|passages|search\s+results)",
        r"pretend\s+(?:that\s+)?(?:the|these|those)\s+(?:above\s+|provided\s+|retrieved\s+)?(?:documents?|context|"
        r"sources)\s+(?:do(?:es)?\s+not|don't|doesn't)\s+exist",
        r"without\s+(?:relying\s+on|using|citing|referencing)\s+(?:any\s+of\s+)?(?:the\s+)?(?:provided\s+)?documents",
        r"(?:solely|only|purely|entirely)\s+on\s+(?:your|general)\s+(?:own\s+)?knowledge",
        r"your\s+own\s+(?:answers?|interpretations?|knowledge|views?|opinions?|words)\s+rather\s+than",
        r"rather\s+than\s+(?:citing|using|repeating\s+what(?:'s|\s+is)\s+in|relying\s+on)\s+(?:the\s+)?(?:documents?|"
        r"sources|theses|them|citations)",
        r"(?:general|own)\s+knowledge\s+rather\s+than\s+(?:the\s+)?(?:documents?|sources|theses|provided)",
        r"disregard\s+(?:any|all)\s+(?:information|content)\s+from\s+(?:the\s+)?(?:cited|provided|retrieved)",
        r"refrain\s+from\s+using\s+(?:any\s+of\s+)?(?:the\s+)?(?:provided|retrieved|cited)",
        r"without\s+(?:relying\s+on|using)\s+(?:that|this|the|those|these)\s+(?:citations?|documents?|sources?|"
        r"references?)\b",
    ],
    "authority": [
        r"(?:i\s+am|i'm|this\s+is|as)\s+(?:the|your|an?|one\s+of\s+the|dr\.?\s+\w+,?\s+(?:the|your|one\s+of\s+the))?\s*"
        r"(?:system\s+administrator|sysadmin|admin(?:istrator)?|(?:head|lead|main|chief|senior)?\s*developers?|it\s+"
        r"(?:department|team|manager|staff|support|admin)|from\s+(?:iisc\s+)?it|with\s+(?:iisc\s+)?it|security\s+"
        r"(?:team|auditor|officer)|(?:your\s+)?(?:creator|owner|operator|maintainer))\b" + _W % 30 +
        r"(?:bypass|break|violate|disable|override|ignore|grant|unrestricted|full\s+(?:system\s+)?access|debug\s+mode|"
        r"maintenance\s+mode|special\s+instructions|specific\s+instructions|without\s+(?:any\s+)?restrictions|"
        r"rules|security\s+(?:checks|measures)|execute|run\s+(?:this|the)\s+command|admin(?:istrator)?\s+(?:rights|"
        r"access|privileges)|(?:full|administrative|admin|elevated|enhanced|temporary|special)\s+(?:\w+\s+)?"
        r"(?:permissions|access|privileges|rights)|(?:enhance|elevate|increase|expand)\s+(?:the\s+assistant's|your|"
        r"its)\s+(?:permissions|access|privileges)|outside\s+(?:of\s+)?its\s+normal|source\s+code|internal\s+data|"
        r"unpublished|modify\s+(?:the\s+assistant's|your)\s+responses|bypass\s+(?:the\s+)?security)",
        r"(?:security\s+audit|maintenance\s+(?:task|session|check|window)|debug(?:ging)?\s+session|testing\s+phase)"
        + _W % 20 + r"(?:bypass|break|violate|disable|unrestricted|full\s+(?:system\s+)?access|its\s+rules|"
        r"without\s+(?:any\s+)?restrictions|change\s+the\s+assistant's\s+behaviou?r|(?:administrative|admin|"
        r"elevated|enhanced)\s+(?:permissions|access|privileges)|outside\s+(?:of\s+)?its\s+normal|"
        r"(?:enhance|elevate)\s+the\s+assistant's\s+permissions)",
        r"(?:enable|enter|run\s+(?:the\s+assistant\s+|yourself\s+)?in|switch\s+(?:to|into)|activate|put\s+(?:yourself|"
        r"the\s+assistant)\s+in)\s+(?:debug|maintenance|developer|admin|god|test)\s+mode",
        r"(?:grant|give)\s+(?:me|the\s+assistant)\s+(?:full|unrestricted|admin(?:istrator)?|root)\s+(?:system\s+)?"
        r"(?:access|rights|privileges|permissions)",
        r"(?:follow|obey|carry\s+out)\s+(?:my\s+|these\s+|some\s+)?(?:special|specific|unusual|secret|new)\s+instructions"
        + _W % 4 + r"(?:might|may|would)\s+(?:not|violate|break|be\s+outside)",
        r"(?:modify|change|alter|edit|delete)\s+(?:the\s+)?(?:user|users')\s+(?:data|permissions|accounts|access)",
        r"(?:modify|change|alter)\s+(?:user\s+)?(?:permissions|access\s+controls)\s+and",
        r"bypass\s+(?:the\s+|any\s+|all\s+)?(?:normal\s+|usual\s+)?(?:access\s+)?(?:restrictions|controls|"
        r"authentication|access\s+checks)",
    ],
    "decode_and_follow": [
        r"(?:decode|decrypt|reverse|unscramble|translate|read)\b" + _W % 12 + r"(?:follow|execute|obey|carry\s+out|"
        r"do\s+what\s+it\s+says|act\s+on)\s*(?:the\s+)?(?:instructions?|it|them|commands?)?",
        r"(?:then|and)\s+do\s+what\s+it\s+says",
    ],
    "multilingual": [
        # French
        r"ignor(?:e|ez|er|ons)\s+(?:les|toutes\s+les|vos|tes|mes)\s+(?:\w+\s+)?(?:instructions|consignes|r[eè]gles|"
        r"restrictions)", r"(?:sans|aucune)\s+(?:aucune\s+)?(?:restriction|limite|limitation|filtre)s?",
        r"(?:votre|ton|le)\s+(?:prompt|invite)\b", r"(?:secret|mot\s+de\s+passe|cl[ée]\s+(?:api|secr[eè]te))\s+(?:de\s+"
        r"(?:votre|ton|du)\s+(?:serveur|syst[eè]me))", r"server\s+secret|secret\s+du\s+serveur",
        r"informations?\s+confidentielles?",
        r"tester\s+vos\s+limites|de\s+toute\s+la\s+mani[eè]re\s+que\s+vous\s+voulez|sans\s+vos\s+r[eè]gles",
        r"ignor(?:er|ez|iez|e)\s+(?:votre|vos|ton|tes)\s+(?:code\s+de\s+conduite|instructions?|fonction(?:nement)?|"
        r"r[eè]gles|consignes)", r"d[ée]tails\s+(?:techniques\s+)?(?:sur|de)\s+vos\s+serveurs",
        # German
        r"ignorier(?:e|en|t)?\s+(?:bitte\s+)?(?:die|alle|deine|ihre|s[äa]mtliche|jegliche)?\s*(?:\w+\s+)?(?:anweisungen|"
        r"regeln|einschr[äa]nkungen|vorgaben|richtlinien|beschr[äa]nkungen)", r"bitte\s+ignorier",
        r"(?:unbeschr[äa]nkt|uneingeschr[äa]nkt|ohne\s+(?:jede\s+|alle\s+)?(?:einschr[äa]nkungen|beschr[äa]nkungen|"
        r"filter|regeln))", r"(?:system[\s-]?prompt|geheimnis\s+deines\s+servers|deine\s+(?:anweisungen|zugangsdaten|"
        r"passw[öo]rter))",
        # Spanish
        r"ignora(?:r|d)?\s+(?:las|todas\s+las|tus|sus)\s+(?:\w+\s+)?(?:instrucciones|reglas|restricciones)",
        r"sin\s+(?:ninguna\s+)?(?:restricci[oó]n|restricciones|l[ií]mites|filtros)",
        r"(?:revela|mu[eé]strame|dime)\b" + _W % 5 + r"(?:prompt|instrucciones\s+del\s+sistema|secreto|contrase[ñn]a)",
        # Hindi
        r"(?:निर्देशों|निर्देश|नियमों|नियम)\s+(?:को\s+)?(?:अनदेखा|नज़रअंदाज़|नजरअंदाज|भूल)",
        r"सिस्टम\s+प्रॉम्प्ट", r"बिना\s+(?:किसी\s+)?(?:प्रतिबंध|रोक|सीमा)",
    ],
}
_COMPILED_V2 = [(cat, re.compile(p, re.IGNORECASE)) for cat, ps in _PATTERNS_V2.items() for p in ps]

_LEET = str.maketrans({"4": "a", "3": "e", "1": "i", "0": "o", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
_SPACED = re.compile(r"\b(?:\w[ .·_\-]){3,}\w\b")
_B64 = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_ZERO_WIDTH = re.compile(r"[​-‏⁠﻿]")


def _views(text: str) -> List[str]:
    """The message as written plus normalised views that undo common obfuscations."""
    import base64
    import binascii
    import unicodedata
    t = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))
    views = [t]
    de_spaced = _SPACED.sub(lambda m: re.sub(r"[ .·_\-]", "", m.group(0)), t)
    if de_spaced != t:
        views.append(de_spaced)
    leet = de_spaced.translate(_LEET)
    if leet != de_spaced:
        views.append(leet)
    for blob in _B64.findall(t):
        try:
            decoded = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=False).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if decoded and sum(ch.isprintable() for ch in decoded) / len(decoded) > 0.9:
            views.append(decoded)
    return views


_QUESTION_TO_ASSISTANT = re.compile(r"^\W*(?:are|do|did|have|were|can|could|will|would)\s+you\s+(?:sure|really|"
                                    r"actually|able|aware|answering|using|citing|getting|working|running|still)\b", re.I)


def detect_injection(prompt: str, use_classifier: bool = True) -> Optional[str]:
    """Category of the first injection signature found in any view of the message, else None.
    Patterns first; then the embedding classifier (pipeline/injection_classifier.py) when available."""
    views = _views(prompt or "")
    for view in views:
        for pattern in COMPILED_INJECTION_RE:
            if pattern.search(view):
                return "legacy"
        for cat, pattern in _COMPILED_V2:
            if pattern.search(view):
                return cat
    # A plain question to the assistant about itself ("Are you sure you are answering from the repository?") is
    # not blocked by the classifier alone: it scores like an instruction but asks for nothing (patterns still apply).
    if use_classifier and not _QUESTION_TO_ASSISTANT.match(views[0]):
        from pipeline.injection_classifier import is_injection
        if is_injection(views):
            return "classifier"
    return None


def sanitize_and_fence_prompt(prompt: str, use_classifier: bool = True) -> str:
    """Validate prompt against adversarial injection signatures and encapsulate it.

    use_classifier=False for caller-supplied system/assistant messages: they are instructions by nature, which
    the embedding classifier (trained on user questions) flags; the patterns still apply to them.

    Raises:
        PromptInjectionAlert: If an injection attempt or delimiter escape is detected.
    Returns:
        Fenced prompt wrapped in a safe user block.
    """
    clean_prompt = prompt.strip()
    if detect_injection(clean_prompt, use_classifier=use_classifier):
        raise PromptInjectionAlert("NFR-7 Alert: Adversarial prompt injection detected.")

    return f"```user\n{clean_prompt}\n```"


# Shared by the CPU-SLM and GPU-LLM RAG prompts (pipeline/slm_runner.py, pipeline/gpu_llm_runner.py).
# Red-team finding: loaded questions ("Why does X always...?") were answered by confirming the false premise,
# while the neutral form of the same question ("Does X...?") was correctly refused on the same context.
REFUSAL_SENTENCE = "No specific institutional repository theses were found on this topic."

# Rule 3 of both RAG prompts. The refusal sentence claims that NO thesis is about the topic, so it must
# not be used when on-topic documents were retrieved but do not answer the exact question or confirm
# its claim (red-team: "Are bacterial plasmids always essential...?" retrieved plasmid-maintenance
# theses, yet both models answered with the refusal sentence and the sources were hidden).
REFUSAL_RULE = (
    "3. Relevance and Refusal: If the provided documents contain information relevant to the question, answer "
    "directly from them with citations, without opening disclaimers; if they do not cover some specific part of the "
    "question, mention that briefly after your answer. Reply with exactly '" + REFUSAL_SENTENCE + "' and nothing else "
    "ONLY when none of the provided documents is about the topic of the question. Never guess or give an ungrounded "
    "answer.\n"
)

PREMISE_RULES = (
    "4. Check the Question's Premise: Some questions contain a built-in claim (for example 'Why does X always...', "
    "'Why can X...', 'Why is X essential for every...'). Challenge that claim ONLY if (a) the provided documents "
    "contradict it, (b) it is a sweeping absolute claim (always, never, every, all, only, guarantees) that the documents "
    "do not establish, or (c) it contradicts well-established scientific principles. In those cases say so in your "
    "first sentence (for example: 'This is not generally true: ...' or 'The provided theses do not support the claim "
    "that ...'), then state what the documents do support; never explain why such a claim is true. If the claim is "
    "reasonable and simply not discussed in the documents, do NOT challenge or doubt it: answer from what the documents "
    "say, and if needed note briefly that they do not address that specific point.\n"
    "5. Single Outcome: The refusal sentence stands alone. If you have anything relevant to say from the documents, "
    "do not write the refusal sentence at all; never follow it with an explanation or 'However, ...'.\n"
)


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


# Canonical refusal and insufficient data phrases emitted when corpus documents do not answer the prompt
NO_RELEVANT_DOCS_PHRASES = (
    "no specific institutional repository",
    "no institutional repository theses",
    "no institutional repository documents",
    "no theses were found",
    "no thesis was found",
    "no relevant documents",
    "no relevant document",
    "no relevant information",
    "no relevant thesis",
    "no relevant theses",
    "no documents were found",
    "no document was found",
    "no documents found",
    "no document found",
    "no documents directly cover",
    "no documents in the thesis",
    "no documents are present",
    "no relevant documents are present",
    "not present in the thesis",
    "not found in the thesis",
    "not mentioned in the thesis",
    "not found in any of the provided documents",
    "do not contain information to answer",
    "does not contain information to answer",
    "do not contain enough information",
    "does not contain enough information",
    "do not contain any information",
    "does not contain any information",
    "do not provide any information",
    "does not provide any information",
    "do not provide enough information",
    "does not provide enough information",
    "cannot find any information",
    "could not find any information",
    "cannot find information",
    "unable to find information",
    "cannot answer based on",
    "unable to answer based on",
    "insufficient information",
    "insufficient data",
    "insufficient context",
    "not enough information",
    "not have enough information",
    "do not have enough information",
    "does not have enough information",
    "no information provided",
    "information is not available",
    "no information is available",
    "no information was found",
    "no information about",
    "there is no information",
    "retrieval failed",
    "based on general academic knowledge",
    "based on general technical knowledge",
)

NO_RELEVANT_DOCS_PATTERNS = [
    re.compile(r"no\s+specific\s+institutional\s+repository", re.IGNORECASE),
    re.compile(r"no\s+institutional\s+repository", re.IGNORECASE),
    re.compile(r"no\s+thes[ie]s\s+(?:were|was|are|is)?\s*found", re.IGNORECASE),
    re.compile(r"no\s+relevant\s+documents?", re.IGNORECASE),
    re.compile(r"no\s+documents?\s+(?:were|was|are|is)?\s*(?:found|present)", re.IGNORECASE),
    re.compile(r"not\s+present\s+in\s+the\s+thes[ie]s", re.IGNORECASE),
    re.compile(r"not\s+found\s+in\s+(?:the\s+thes[ie]s|the\s+provided\s+documents?)", re.IGNORECASE),
    re.compile(r"(?:provided|retrieved)\s+documents?\s+do(?:es)?\s*not\s+contain", re.IGNORECASE),
    re.compile(r"(?:cannot|unable\s+to)\s+answer\s+based\s+on\s+the\s+(?:provided|retrieved)", re.IGNORECASE),
    re.compile(r"no\s+information\s+(?:is\s+)?(?:about|regarding|on|in|available|found|provided)", re.IGNORECASE),
    re.compile(r"there\s+is\s+no\s+information", re.IGNORECASE),
    re.compile(r"no\s+documents?\s+(?:directly\s+)?(?:cover|discuss|address)", re.IGNORECASE),
]


# Answers that admit they are not grounded are never served as grounded, whatever their length.
UNGROUNDED_MARKERS = ("based on general academic knowledge", "based on general technical knowledge", "retrieval failed")
# A refusal is short. A longer answer that mentions a gap in passing ("...no information about X, but the
# theses describe Y [Document 1]") is an answer: it keeps its sources and may be cached.
REFUSAL_MAX_WORDS = 60
# Words after the refusal sentence that make an answer "mixed" (refusal + explanation).
MIXED_MIN_WORDS = 12

# Tolerates short variants of the sentence ending ("...found on this.", "...found for this query.").
_REFUSAL_RE = re.compile(r"^\W*no\s+specific\s+institutional\s+repository\s+thes[ie]s\s+(?:were|was)\s+found\b[^.!?\n]{0,40}[.!?]?\W*",
                         re.IGNORECASE)
_CONNECTOR_RE = re.compile(r"^(?:however|but|that said|nevertheless)\s*[,:]?\s*", re.IGNORECASE)


def starts_with_refusal(text: str) -> bool:
    return bool(text) and bool(_REFUSAL_RE.match(text))


def strip_mixed_refusal_text(text: str) -> str:
    """'<refusal sentence> <explanation>' -> '<explanation>'. A pure refusal is returned unchanged."""
    if not text:
        return text
    m = _REFUSAL_RE.match(text)
    if not m:
        return text
    rest = _CONNECTOR_RE.sub("", text[m.end():].lstrip())
    if len(rest.split()) < MIXED_MIN_WORDS:
        return text
    return rest[:1].upper() + rest[1:]


def strip_mixed_refusal(deltas):
    """Streaming version of strip_mixed_refusal_text. Holds back only output that begins with the refusal
    sentence, until it is clear whether an explanation follows (MIXED_MIN_WORDS); any other answer passes
    through without delay."""
    target = REFUSAL_SENTENCE.lower()
    buf, mode = "", "probe"
    try:
        for d in deltas:
            if mode == "pass":
                yield d
                continue
            buf += d
            norm = re.sub(r"\s+", " ", buf.lstrip()).lower()
            if mode == "probe":
                if target.startswith(norm) or target.startswith(norm.lstrip("*_#> ")):
                    continue
                if _REFUSAL_RE.match(buf):
                    mode = "after"
                else:
                    mode = "pass"
                    yield buf
                    buf = ""
                    continue
            m = _REFUSAL_RE.match(buf)
            rest = buf[m.end():] if m else buf
            if len(rest.split()) > MIXED_MIN_WORDS:  # explanation follows: drop the refusal sentence
                mode = "pass"
                yield strip_mixed_refusal_text(buf)
                buf = ""
        if buf:
            yield buf  # pure refusal (or short tail): unchanged
    finally:
        close = getattr(deltas, "close", None)
        if close:
            close()


def is_no_relevant_docs_answer(answer_text: str) -> bool:
    """Return True if answer_text IS a refusal / no-relevant-documents answer (not merely mentions a gap)."""
    if not answer_text or not isinstance(answer_text, str):
        return False
    t = answer_text.strip().lower()
    if not t:
        return False
    if any(m in t for m in UNGROUNDED_MARKERS):
        return True
    if _REFUSAL_RE.match(t):
        return len(t[_REFUSAL_RE.match(t).end():].split()) < MIXED_MIN_WORDS
    if len(t.split()) > REFUSAL_MAX_WORDS:
        return False
    for p in NO_RELEVANT_DOCS_PHRASES:
        if p in t:
            return True
    for pat in NO_RELEVANT_DOCS_PATTERNS:
        if pat.search(t):
            return True
    return False


