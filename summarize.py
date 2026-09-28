"""Exercise: add rolling summaries to chat_demo.py.

Without summaries, old messages simply drop out of the context once the
token budget is full, and the model forgets them. Your job is to fold older
messages into a short summary that stays in the context, and then remove
them from the list.

The summary goes in one new field of the meta hash:

  chat:{session_id}:meta       Hash: created_at, updated_at, summary

SummarizingStore extends ConversationStore without changing it. You write
two methods, marked TODO below:

  1. pinned()              put the summary right after the system prompt
  2. summarize_and_trim()  create the summary and trim the list

Check your work:        pytest -v
Then try it for real:   python chat_demo.py --offline --verbose --summarize --summarize-after 6 --keep-last 2
"""
from __future__ import annotations

from chat_demo import ConversationStore, approx_tokens


class SummarizingStore(ConversationStore):
    def __init__(self, client, ttl_seconds, summarizer, verbose=False):
        super().__init__(client, ttl_seconds, verbose)
        self.summarizer = summarizer   # summarizer(previous_summary, messages) -> new summary

    def get_summary(self, session_id: str) -> str:
        """Provided: the stored summary, or "" if there isn't one yet."""
        _, meta_key = self.keys(session_id)
        return self.r.hget(meta_key, "summary") or ""

    def pinned(self, session_id, system_prompt, max_tokens):
        """TODO 1: the system prompt, then the summary if it fits.

        Return a list of (label, text) pairs, like ConversationStore.pinned does.

        - Start from super().pinned(...), which gives [("system", system_prompt)].
        - If the session has a summary, add ("summary", text), where text is
          exactly:  "Summary of the earlier conversation:\\n" + summary
        - Only add it if it fits: approx_tokens(text) must be no more than the
          budget left after the pinned messages you already have.
        - Optional: if self.verbose and it doesn't fit, print a note saying so.

        Test with:  pytest -k pinned -v
        """
        raise NotImplementedError("TODO 1: pinned")

    def summarize_and_trim(self, session_id: str, keep_last: int) -> None:
        """TODO 2: fold all but the newest `keep_last` messages into the summary.

        - If the session has keep_last messages or fewer, do nothing.
        - Otherwise, the "old" messages are everything except the newest keep_last.
        - Build the new summary with self.summarizer(previous_summary, old_messages).
          It's a rolling summary: pass in the current summary so the new one builds on it.
        - Then, in ONE transaction (self.r.pipeline(transaction=True)):
            save the new summary in the meta hash,
            remove the old messages from the list (LTRIM),
            and refresh the expiry on both keys, as append does.

        Careful with LTRIM. The summarizer can be slow (it calls a model), and a
        new message may be appended while it runs. Which LTRIM start and stop
        remove exactly the messages you summarized, and nothing else?

        Test with:  pytest -k trim -v
        """
        raise NotImplementedError("TODO 2: summarize_and_trim")


def make_summarizer(reply, offline: bool):
    """Provided: return summarize(previous_summary, messages) -> new summary."""
    if offline:
        def summarize(previous, messages):
            # Stand-in for a real summary: just remember what the user said.
            said = "; ".join(m.content for m in messages if m.role == "user")
            return f"{previous}; {said}" if previous else f"The user said: {said}"
        return summarize

    def summarize(previous, messages):
        transcript = "\n".join(f"{m.role}: {m.content}" for m in messages)
        prompt = (
            f"Existing summary:\n{previous or '(none)'}\n\n"
            f"New messages to fold in:\n{transcript}\n\n"
            "Write an updated summary under 150 words. Preserve names, numbers, "
            "decisions, and stated user preferences exactly."
        )
        return reply("You summarize conversations accurately.", [{"role": "user", "content": prompt}],
                     call="summary")

    return summarize
