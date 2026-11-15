"""Shared conservative request estimate; never infers runtime slot capacity.

LM Studio accounts for the actual tokenizer plus the requested output reserve,
while the old character heuristic was too optimistic for masked Markdown and
Cyrillic/Unicode-heavy prompts.  Keep the estimate deliberately conservative
and compute a smaller output reserve for short translation blocks.
"""

REPLY_TOKENS = 4096
CHAR_TOKEN_FACTOR = 0.90
SAFETY_TOKENS = 256


class RequestBudgetError(ValueError):
    """Deterministic preflight refusal; retrying the same prompt cannot help."""


def estimate_input_tokens(messages):
    return int(sum(len(m["content"]) for m in messages) * CHAR_TOKEN_FACTOR) + 64


def estimate_messages(messages, reply_tokens=REPLY_TOKENS):
    """Conservative character heuristic, not a tokenizer or a guarantee."""
    return estimate_input_tokens(messages) + int(reply_tokens)


def output_tokens_for(messages, context_per_request, requested=REPLY_TOKENS):
    """Reserve only the output space the current prompt can safely afford."""
    user_chars = sum(len(m["content"]) for m in messages
                     if m.get("role") == "user")
    # group_chars=900 makes the vast majority of requests short.  A large
    # output reserve makes llama.cpp/LM Studio spend time exploring an
    # impossible tail even when the translation itself is only a few hundred
    # tokens.  Keep more room for genuinely long indivisible paragraphs.
    if user_chars <= 1200:
        requested = min(int(requested), 1536)
    elif user_chars <= 2400:
        requested = min(int(requested), 2048)
    else:
        requested = min(int(requested), 3072)
    if not context_per_request:
        return int(requested)
    available = int(context_per_request) - estimate_input_tokens(messages) - SAFETY_TOKENS
    return max(256, min(int(requested), available))


def check_messages(messages, context_per_request=0, reply_tokens=REPLY_TOKENS):
    if type(context_per_request) is not int or context_per_request < 0:
        raise ValueError("context_per_request must be a nonnegative integer")
    estimate = estimate_messages(messages, reply_tokens)
    if context_per_request and estimate > context_per_request:
        raise RequestBudgetError(f"request budget exceeded: estimated {estimate} tokens > "
                         f"confirmed per-request context {context_per_request}")
    return estimate
