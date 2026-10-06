"""Provider-neutral connection seam. No endpoint or HTTP schema is assumed."""

import os
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Thread

from src.llm import build_messages, parse_decision


class CallDeadlineExceeded(TimeoutError):
    pass


def bounded_call(callback, seconds):
    """Stop waiting at the deadline; the transport must also enforce its I/O timeout."""
    if seconds <= 0:
        raise CallDeadlineExceeded()
    result = Queue(maxsize=1)

    def worker():
        try:
            result.put((True, callback()))
        except BaseException as exc:
            result.put((False, exc))

    # ponytail: a daemon bounds waiting, not cancellation; real transports must honor timeout_seconds.
    Thread(target=worker, daemon=True).start()
    try:
        ok, value = result.get(timeout=seconds)
    except Empty:
        raise CallDeadlineExceeded() from None
    if not ok:
        raise value
    return value


@dataclass(frozen=True)
class ConnectionSettings:
    endpoint: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    model: str = "openai/gpt-oss-20b"

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("NOVA_API_URL", ""), os.environ.get("NOVA_API_KEY", ""))


@dataclass(frozen=True)
class ModelReply:
    text: str
    input_tokens: int
    output_tokens: int


class PreparedAPIAdapter:
    """One instance per server session; complete and count_input_tokens await official specs.

    complete(messages, *, model, timeout_seconds, max_output_tokens) -> ModelReply
    count_input_tokens(messages, model) must conservatively include all provider overhead.
    Secrets belong in the transport closure, never patient state or prompt messages.
    """

    def __init__(self, complete=None, count_input_tokens=None, *, competition=False):
        self.complete = complete
        self.count_input_tokens = count_input_tokens
        self.competition = competition
        self.calls = self.successful_calls = self.input_tokens = self.output_tokens = 0
        self.disabled = False

    def next_action(self, state):
        if self.complete is None or self.count_input_tokens is None:
            self.disabled = True
            raise RuntimeError("Official transport/token counter is not connected")
        if self.disabled or self.calls >= 200:
            self.disabled = True
            raise RuntimeError("Session call budget exhausted or usage uncertain")
        if state.remaining_seconds <= 5:
            raise CallDeadlineExceeded()
        messages = build_messages(state)
        model = "openai/gpt-oss-20b"
        estimated = self.count_input_tokens(messages, model)
        if type(estimated) is not int or estimated < 0:
            raise ValueError("Invalid input token estimate")
        if self.input_tokens + estimated > 500000 or self.output_tokens >= 100000:
            self.disabled = True
            raise RuntimeError("Session token budget exhausted")
        output_limit = min(4096, 100000 - self.output_tokens)
        self.calls += 1
        try:
            reply = self.complete(messages, model=model,
                                  timeout_seconds=max(0.001, min(60, state.remaining_seconds - 5)),
                                  max_output_tokens=output_limit)
            if (not isinstance(reply, ModelReply) or not isinstance(reply.text, str)
                    or any(type(n) is not int or n < 0 for n in (reply.input_tokens, reply.output_tokens))):
                raise ValueError("Transport must return text and token usage")
            self.input_tokens += reply.input_tokens
            self.output_tokens += reply.output_tokens
            if reply.input_tokens > estimated or reply.output_tokens > output_limit:
                raise ValueError("Transport exceeded reserved token budget")
        except Exception:
            # Failed requests may still have been billed. Do not silently reset their budget.
            self.disabled = True
            raise
        self.successful_calls += 1
        return parse_decision(reply.text)

    def usage(self):
        return {"calls": self.calls, "successful_calls": self.successful_calls,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "competition_transport": self.competition, "disabled": self.disabled}
