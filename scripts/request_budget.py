"""Shared conservative request estimate; never infers runtime slot capacity."""

REPLY_TOKENS = 4096


def estimate_messages(messages, reply_tokens=REPLY_TOKENS):
    """Character heuristic, not a tokenizer or a capacity guarantee."""
    return int(sum(len(m["content"]) for m in messages) / 2 * 1.2) + reply_tokens + 64


def check_messages(messages, context_per_request=0, reply_tokens=REPLY_TOKENS):
    if type(context_per_request) is not int or context_per_request < 0:
        raise ValueError("context_per_request must be a nonnegative integer")
    estimate = estimate_messages(messages, reply_tokens)
    if context_per_request and estimate > context_per_request:
        raise ValueError(f"request budget exceeded: estimated {estimate} tokens > "
                         f"confirmed per-request context {context_per_request}")
    return estimate
