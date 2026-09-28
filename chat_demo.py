"""A barebones chat agent that keeps its memory in Redis.

Everything is in this one file so you can read it top to bottom:

  1. ConversationStore  how conversations are stored in Redis
  2. Redis trace        prints every Redis command (--verbose)
  3. The model          an offline echo bot, a local Ollama model, or Claude,
                        plus timing and token counts (--verbose, --telemetry)
  4. The chat loop      ties it all together

Optional: --summarize adds rolling summaries from summarize.py.

Run it:
    docker compose up -d
    python chat_demo.py --verbose                               # offline echo bot
    python chat_demo.py --verbose --model ollama:llama3.2:3b    # local model
    python chat_demo.py --verbose --telemetry telemetry.jsonl   # also log each model call

In-chat commands: /history  /end  /quit  (and /summary with --summarize)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field

import redis


# =============================================================================
# 1. ConversationStore
#
# Each chat session uses two Redis keys:
#
#   chat:{session_id}:messages   List of JSON messages, oldest first
#   chat:{session_id}:meta       Hash: created_at, updated_at
#
# Both keys expire after `ttl_seconds` of inactivity, so abandoned sessions
# clean themselves up.
# =============================================================================

def approx_tokens(text: str) -> int:
    """Rough token count (~4 characters per token). Real apps use the model's tokenizer."""
    return max(1, len(text) // 4)


@dataclass
class Message:
    role: str      # "user" or "assistant"
    content: str
    tokens: int    # counted once, when stored, so old messages are never re-counted
    ts: float = field(default_factory=time.time)   # Unix time in seconds


class ConversationStore:
    def __init__(self, client: redis.Redis, ttl_seconds=1800, verbose=False):
        self.r = client
        self.ttl = ttl_seconds
        self.verbose = verbose

    def keys(self, session_id: str) -> tuple[str, str]:
        return f"chat:{session_id}:messages", f"chat:{session_id}:meta"

    def append(self, session_id: str, role: str, content: str) -> Message:
        """Store one message. All five commands run as one transaction (MULTI ... EXEC)."""
        msg = Message(role=role, content=content, tokens=approx_tokens(content))
        messages_key, meta_key = self.keys(session_id)

        pipe = self.r.pipeline(transaction=True)
        pipe.rpush(messages_key, json.dumps(asdict(msg)))   # add to the end (newest last)
        pipe.hsetnx(meta_key, "created_at", msg.ts)         # only set the first time
        pipe.hset(meta_key, "updated_at", msg.ts)           # overwritten every time
        pipe.expire(messages_key, self.ttl)                 # restart the idle timer
        pipe.expire(meta_key, self.ttl)                     # ...on both keys
        pipe.execute()
        return msg

    def get_history(self, session_id: str) -> list[Message]:
        messages_key, _ = self.keys(session_id)
        return [Message(**json.loads(raw)) for raw in self.r.lrange(messages_key, 0, -1)]

    def count(self, session_id: str) -> int:
        messages_key, _ = self.keys(session_id)
        return self.r.llen(messages_key)

    def pinned(self, session_id: str, system_prompt: str, max_tokens: int) -> list[tuple[str, str]]:
        """(label, text) pairs that are always sent first. Here, just the system prompt.

        summarize.py overrides this to add the conversation summary.
        """
        return [("system", system_prompt)]

    def get_context(self, session_id: str, max_tokens: int, system_prompt: str) -> list[dict]:
        """Build what the model sees, within a token budget:

        1. the pinned messages (always included),
        2. as many recent messages as still fit, oldest first.
        """
        budget = max_tokens
        context, parts = [], []
        for label, text in self.pinned(session_id, system_prompt, max_tokens):
            tokens = approx_tokens(text)
            context.append({"role": "system", "content": text})
            parts.append(f"{label} {tokens} tok")
            budget -= tokens

        # Walk backward from the newest message. Stop at the first one that doesn't
        # fit: skipping it to grab an older, shorter one would leave a gap.
        history = self.get_history(session_id)
        selected = []
        for msg in reversed(history):
            if msg.tokens > budget:
                break
            selected.append(msg)
            budget -= msg.tokens
        selected.reverse()

        # Chat APIs expect the conversation to start with a user message.
        while selected and selected[0].role != "user":
            selected.pop(0)

        if self.verbose:
            message_tokens = sum(m.tokens for m in selected)
            parts.append(f"{len(selected)} of {len(history)} messages {message_tokens} tok")
            used = sum(approx_tokens(c["content"]) for c in context) + message_tokens
            left_out = len(history) - len(selected)
            note = f" ({left_out} older left out)" if left_out else ""
            print(f"  [context] {' + '.join(parts)} = {used} / {max_tokens} budget{note}")

        return context + [{"role": m.role, "content": m.content} for m in selected]

    def end_session(self, session_id: str) -> None:
        self.r.delete(*self.keys(session_id))


# =============================================================================
# 2. Redis trace (--verbose)
#
# redis-py sends single commands through send_command() and pipelines through
# pack_commands(). We subclass the connection to print each command first.
# =============================================================================

HIDDEN = {"AUTH", "HELLO", "CLIENT", "SELECT"}   # connection setup; AUTH has the password


def _show(args):
    name = str(args[0]).split()[0].upper()
    if name in HIDDEN:
        return
    words = []
    for arg in args:
        s = str(arg)
        if len(s) > 60:
            s = s[:57] + "..."
        words.append(f"'{s}'" if " " in s or '"' in s else s)
    print("  [redis] " + " ".join(words))


def trace_redis(client: redis.Redis) -> None:
    """Print every command this client sends. Call before the first command."""
    base = client.connection_pool.connection_class

    class TracedConnection(base):
        def send_command(self, *args, **kwargs):
            _show(args)
            super().send_command(*args, **kwargs)

        def pack_commands(self, commands):
            for cmd in commands:
                _show(cmd)
            return super().pack_commands(commands)

    client.connection_pool.connection_class = TracedConnection


# =============================================================================
# 3. The model
# =============================================================================

SYSTEM_PROMPT = "You are a concise, friendly assistant."
CLAUDE_MODEL = os.environ.get("CHAT_MODEL", "claude-sonnet-5")
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
if "://" not in OLLAMA_URL:
    OLLAMA_URL = "http://" + OLLAMA_URL
# Ollama has its own context limit, and silently drops the start of a longer prompt.
# Set it well above our own budget so only our budget decides what the model sees.
OLLAMA_NUM_CTX = 8192

# Each provider is a function: call(system, messages) -> (reply text, usage dict).
# usage holds whatever real counts the provider reports; the offline bot has none.


def offline_provider():
    def call(system, messages):
        return f"You said: {messages[-1]['content']} (echo; context size: {len(messages)})", {}
    return call


def ollama_provider(name: str):
    import urllib.error
    import urllib.request

    def call(system, messages):
        body = {
            "model": name,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream": False,
            "options": {"num_ctx": OLLAMA_NUM_CTX},
        }
        request = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as resp:
                r = json.load(resp)
        except urllib.error.HTTPError as e:
            error = json.loads(e.read() or b"{}").get("error", str(e))
            sys.exit(f"Ollama error: {error}\nIs the model downloaded? Try: ollama pull {name}")
        except urllib.error.URLError:
            sys.exit(f"Cannot reach Ollama at {OLLAMA_URL}. Is Ollama running?")
        return r["message"]["content"], {
            "input_tokens": r.get("prompt_eval_count"),
            "cached_input_tokens": r.get("prompt_eval_cached_count"),
            "output_tokens": r.get("eval_count"),
            "load_s": round(r.get("load_duration", 0) / 1e9, 3),   # loading the model into memory
        }
    return call


def claude_provider(name: str):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY to use Claude, or pick another --model.")
    import anthropic   # only needed for Claude

    client = anthropic.Anthropic()

    def call(system, messages):
        resp = client.messages.create(model=name, max_tokens=1024, system=system, messages=messages)
        text = "".join(b.text for b in resp.content if b.type == "text")
        return text, {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return call


class Model:
    """The chosen model. Times every call, and reports it with --verbose and --telemetry.

    spec is "offline", "ollama:<model>" (e.g. ollama:llama3.2:3b), or "claude[:<model id>]".
    """

    def __init__(self, spec: str, verbose=False, telemetry_path=None):
        provider, _, name = spec.partition(":")
        if provider == "offline":
            self.call = offline_provider()
        elif provider == "ollama" and name:
            self.call = ollama_provider(name)
        elif provider == "claude":
            name = name or CLAUDE_MODEL
            self.call = claude_provider(name)
        else:
            sys.exit(f"Unknown --model {spec!r}. Use offline, ollama:<model>, or claude[:<model id>].")
        self.name = f"{provider}:{name}" if name else provider
        self.verbose = verbose
        self.telemetry_path = telemetry_path
        self.session_id = None

    def reply(self, system: str, messages: list[dict], call: str = "reply") -> str:
        start = time.perf_counter()
        text, usage = self.call(system, messages)
        record = {
            "ts": round(time.time(), 3),
            "session": self.session_id,
            "call": call,                 # "reply", or "summary" from summarize.py
            "model": self.name,
            "latency_s": round(time.perf_counter() - start, 3),
            "context_messages": len(messages),
            "estimated_input_tokens": approx_tokens(system) + sum(approx_tokens(m["content"]) for m in messages),
            **usage,
        }
        if self.verbose:
            print(describe_call(record))
        if self.telemetry_path:
            # Numbers only: never the message text.
            with open(self.telemetry_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        return text


def describe_call(rec: dict) -> str:
    line = f"  [model] {rec['call']} via {rec['model']}: {rec['latency_s']:.2f} s"
    if rec.get("load_s", 0) >= 0.1:   # mostly the first call, while Ollama loads the model
        line += f" (incl. {rec['load_s']:.2f} s loading the model)"
    if rec.get("input_tokens") is None:
        return line + f", input ~{rec['estimated_input_tokens']} tok (estimated; offline has no real counts)"
    line += f", input {rec['input_tokens']} tok (estimated {rec['estimated_input_tokens']})"
    if rec.get("cached_input_tokens"):
        line += f", {rec['cached_input_tokens']} cached"
    return line + f", output {rec['output_tokens']} tok"


# =============================================================================
# 4. The chat loop
# =============================================================================

def main():
    p = argparse.ArgumentParser(description="A barebones chat agent with Redis memory.")
    p.add_argument("--session", help="session id to resume")
    p.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    p.add_argument("--ttl", type=int, default=1800, help="seconds of inactivity before a session expires")
    p.add_argument("--max-context-tokens", type=int, default=2000, help="token budget for the model's context")
    p.add_argument("--model", default="offline",
                   help="offline (echo bot, the default), ollama:<model> such as ollama:llama3.2:3b, "
                        "or claude[:<model id>]")
    p.add_argument("--offline", action="store_true", help="same as --model offline")
    p.add_argument("--verbose", action="store_true",
                   help="show Redis commands, what goes into the context, and each model call")
    p.add_argument("--telemetry", metavar="FILE",
                   help="append one JSON line per model call to FILE (timing and token counts, no message text)")
    s = p.add_argument_group("summarization (optional, see summarize.py)")
    s.add_argument("-s", "--summarize", action="store_true", help="fold older messages into a rolling summary")
    s.add_argument("--summarize-after", type=int, default=10, help="summarize once a session has more messages than this")
    s.add_argument("--keep-last", type=int, default=4, help="messages to keep when summarizing")
    args = p.parse_args()

    client = redis.Redis.from_url(args.redis_url, decode_responses=True)
    if args.verbose:
        trace_redis(client)
    try:
        client.ping()
    except redis.ConnectionError:
        where = client.connection_pool.connection_kwargs   # host and port only: the URL may hold a password
        sys.exit(f"Cannot reach Redis at {where.get('host')}:{where.get('port')}. Did you run `docker compose up -d`?")

    model = Model("offline" if args.offline else args.model, args.verbose, args.telemetry)
    if args.summarize:
        from summarize import SummarizingStore, make_summarizer
        summarizer = make_summarizer(model.reply, offline=model.name == "offline")
        store = SummarizingStore(client, args.ttl, summarizer, args.verbose)
    else:
        store = ConversationStore(client, args.ttl, args.verbose)
    session_id = args.session or uuid.uuid4().hex[:8]
    model.session_id = session_id

    existing = store.count(session_id)
    print(f"Session {session_id} ({f'resumed, {existing} messages' if existing else 'new'}), model: {model.name}")
    print(f"Resume later with: python chat_demo.py --session {session_id}\n")

    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/history":
            for m in store.get_history(session_id):
                print(f"  [{m.role}, {m.tokens} tok] {m.content}")
            continue
        if text == "/summary":
            if args.summarize:
                print(f"  {store.get_summary(session_id) or '(no summary yet)'}")
            else:
                print("  (summarization is off; start with --summarize)")
            continue
        if text == "/end":
            store.end_session(session_id)
            print("Session deleted.")
            break

        store.append(session_id, "user", text)
        context = store.get_context(session_id, args.max_context_tokens, SYSTEM_PROMPT)
        system = "\n\n".join(m["content"] for m in context if m["role"] == "system")
        messages = [m for m in context if m["role"] != "system"]
        answer = model.reply(system, messages)
        store.append(session_id, "assistant", answer)
        print(f"bot> {answer}\n")

        if args.summarize and store.count(session_id) > args.summarize_after:
            store.summarize_and_trim(session_id, args.keep_last)
            print("  (older messages folded into the summary)\n")


if __name__ == "__main__":
    main()
