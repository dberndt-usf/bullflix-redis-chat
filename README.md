# Redis Chat Memory Demo

A barebones chat agent that stores its conversation in Redis. It remembers conversations across restarts, forgets idle sessions automatically, and keeps what it sends to the model within a token budget.

All the core code is in one file, [chat_demo.py](chat_demo.py), about 390 lines including comments. Run it with `--verbose` to see every Redis command it sends, then read the code to see why.

Your exercise is to finish [summarize.py](summarize.py), which adds rolling summaries; see [Exercise: summarization](#exercise-summarization).

## Setup

You need:

- **Python 3.10 or newer**
- **Docker Desktop**, running (it runs Redis for you). No Docker? See [Using a hosted Redis](#using-a-hosted-redis).
- **[Ollama](https://ollama.com)**, only if you want a real model instead of the offline echo bot

### Get the code

With git:

```bash
git clone https://github.com/dberndt-usf/bullflix-redis-chat.git
cd bullflix-redis-chat
```

Without git: open [github.com/dberndt-usf/bullflix-redis-chat](https://github.com/dberndt-usf/bullflix-redis-chat), click **Code**, then **Download ZIP**. Unzip it (the folder is usually called `bullflix-redis-chat-main`) and open a terminal in that folder.

### Install the Python packages and start Redis

macOS / Linux:

```bash
docker compose up -d                  # starts Redis 7 on localhost:6379
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
docker compose up -d                  # starts Redis 7 on localhost:6379
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell says "running scripts is disabled on this system", you can skip activating: run the virtual environment's Python directly, as `.venv\Scripts\python chat_demo.py ...` instead of `python chat_demo.py ...`. Or allow local scripts once with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` and try again.

If another Redis is already using port 6379 (for example, from the Redis lab), `docker compose up -d` fails with a port error. Either stop the other one with `docker compose down` in its folder, or skip this step and use the Redis that's already running.

### Optional: a local model

To chat with a real model, install Ollama and download a small one (about 1.3 GB):

```bash
ollama pull llama3.2:1b
```

Then add `--model ollama:llama3.2:1b` when you run the demo (see [Choosing a model](#choosing-a-model)). Without a graphics card, expect a few seconds per reply, and a slower first reply while the model loads.

## Run it

```bash
python chat_demo.py --verbose
```

In the chat, type messages, or one of: `/history` (stored messages), `/end` (delete the session), `/quit` (leave it saved).

### Choosing a model

`--model` picks what answers you:

| `--model` | What it is | Needs |
|---|---|---|
| `offline` (the default) | An echo bot that repeats your message. Instant, and shows the mechanics. | Nothing |
| `ollama:<model>`, e.g. `ollama:llama3.2:3b` | A model running on your own computer. | [Ollama](https://ollama.com) and the model downloaded |
| `claude` | Claude, through the Anthropic API. `claude:<model id>` picks a specific model. | An API key in `ANTHROPIC_API_KEY` |

`--offline` still works and means the same as `--model offline`.

To use a local model, install Ollama, then download a small model once:

```bash
ollama pull llama3.2:3b
python chat_demo.py --verbose --model ollama:llama3.2:3b
```

`llama3.2:1b` is smaller and faster on laptops without a graphics card; `llama3.2:3b` gives better answers. The first message takes a few seconds longer while Ollama loads the model into memory. If Ollama runs on another machine, set `OLLAMA_HOST` (for example `OLLAMA_HOST=http://192.168.1.20:11434`).

Ollama has its own context limit (`num_ctx`), and it silently drops the start of any prompt that's longer. The demo sets it to 8192 tokens, well above its own budget, so only the demo's budget decides what the model sees.

## Reading the verbose output

Each message you send produces something like this:

```text
[redis] MULTI
[redis] RPUSH chat:3f9a1c2e:messages '{"role": "user", "content": "Hi, I am Ada", "tokens": 3, ...'
[redis] HSETNX chat:3f9a1c2e:meta created_at 1790613432.9314704
[redis] HSET chat:3f9a1c2e:meta updated_at 1790613432.9314704
[redis] EXPIRE chat:3f9a1c2e:messages 1800
[redis] EXPIRE chat:3f9a1c2e:meta 1800
[redis] EXEC
[redis] LRANGE chat:3f9a1c2e:messages 0 -1
[context] system 9 tok + 1 of 1 messages 3 tok = 12 / 2000 budget
[model] reply via ollama:llama3.2:3b: 3.31 s (incl. 2.83 s loading the model), input 46 tok (estimated 22), output 50 tok
```

- **`[redis]` lines** are the exact commands sent to Redis. `MULTI` ... `EXEC` is a transaction: the message, its timestamps, and both expiry timers are written together or not at all. Long values are cut to 60 characters.
- **`[context]` lines** show what the model will see: the system prompt, which is always included, and how many of the stored messages fit in the rest of the token budget.
- **`[model]` lines** show each model call: how long it took, and the real token counts reported by the model next to the demo's estimate. The real count is higher because the model wraps every message in extra formatting tokens (its chat template), which the demo's rough "4 characters per token" estimate doesn't know about. With Ollama you may also see "cached": tokens it could reuse from the previous call instead of processing again.

## Telemetry log

Add `--telemetry FILE` to record every model call, one JSON object per line:

```bash
python chat_demo.py --verbose --model ollama:llama3.2:3b --telemetry telemetry.jsonl
```

```json
{"ts": 1790625791.3, "session": "6add0442", "call": "reply", "model": "ollama:llama3.2:3b", "latency_s": 0.71, "context_messages": 1, "estimated_input_tokens": 22, "input_tokens": 46, "cached_input_tokens": 30, "output_tokens": 59, "load_s": 0.029}
```

| Field | Meaning |
|---|---|
| `ts`, `session` | When, and which conversation |
| `call` | `reply` for a normal answer, `summary` for a summarization call |
| `model` | Which model answered |
| `latency_s` | Seconds the call took, including `load_s` (Ollama loading the model, mostly on the first call) |
| `context_messages` | How many messages were sent |
| `estimated_input_tokens` | The demo's estimate of the input size |
| `input_tokens`, `cached_input_tokens`, `output_tokens` | Real counts from the model (not recorded offline) |

The log never contains message text, only timing and counts, so it's safe to share. New lines are added to the end of the file, so one file can collect many sessions. To load it into a table:

```python
import pandas as pd
df = pd.read_json("telemetry.jsonl", lines=True)
```

(pandas isn't in `requirements.txt`; install it with `pip install pandas`.)

## Things to try

1. **Memory across restarts.** Chat a little, `/quit`, then resume with `python chat_demo.py --offline --session <id>`. Your history is still there, because it lives in Redis, not in Python.
2. **The token budget.** Start with `--max-context-tokens 40` and send five or six messages. Watch the `[context]` line: every message stays in Redis, but older ones are left out of what the model sees. Once your first message drops out, the model can no longer see it.
3. **Watch a real model forget.** With a real model, try:

   ```bash
   python chat_demo.py --verbose --model ollama:llama3.2:3b --max-context-tokens 300
   ```

   Tell it your name, then ask other questions while watching the `[context]` line. Once it says "older left out", ask "What is my name?". Your first message is no longer in the context, so the model can't answer, even though the message is still in Redis. (How many questions that takes depends on how long the replies are; if nothing is left out after five or six questions, restart with a smaller budget.) The model itself remembers nothing between calls; it only knows what's sent each time.
4. **Expiry.** Start with `--ttl 30`, send a message, `/quit`, wait 30 seconds, and resume. The session is gone.
5. **Look inside Redis.** In another terminal, run `docker compose exec redis redis-cli`, then:

   ```
   KEYS chat:*
   LRANGE chat:<id>:messages 0 -1
   HGETALL chat:<id>:meta
   TTL chat:<id>:messages
   ```

## A tour of the code

[chat_demo.py](chat_demo.py) has four sections, in reading order. (The summarization add-on is covered separately below.)

1. **ConversationStore:** everything that touches Redis.
   - `append`: one transaction per message; the expiry restarts on every message (a *sliding* expiry).
   - `pinned`: messages always sent first. Here that's just the system prompt.
   - `get_context`: sends the pinned messages, then walks backward from the newest message until the budget runs out.
   - `end_session`: deletes both keys.
2. **Redis trace:** how `--verbose` prints each command. You can skip this on a first read.
3. **The model:** one small function per provider (offline, Ollama, Claude), each returning the reply and any token counts. The `Model` class wraps them, timing every call and writing the `[model]` line and the telemetry log.
4. **The chat loop:** reads your input and calls the store.

Questions to think about while reading:

- Why store a token count with each message instead of counting when building the context?
- In `get_context`, why stop at the first message that doesn't fit, rather than skipping it and trying older, shorter ones?
- What would go wrong if only the messages key had an expiry, and not the meta key?
- Once the conversation outgrows the budget, the model forgets the oldest messages. How could it keep the important facts? (The optional add-on below is one answer.)

## Exercise: summarization

Without summaries, the oldest messages drop out of the context once the budget is full, and the model forgets them. Try it: start with `--max-context-tokens 40`, tell the bot your name, and keep chatting until your first message is left out.

Your job is to fix that with a *rolling summary*: every so often, older messages are folded into a short summary that is always sent to the model, and then removed from the list. The summary is stored as a `summary` field in the session's meta hash.

Open [summarize.py](summarize.py). `SummarizingStore` extends `ConversationStore` without changing it. Everything is provided except two methods:

1. **`pinned`:** put the summary right after the system prompt, if it fits in the budget.
2. **`summarize_and_trim`:** build the new summary, then save it and trim the list in one transaction.

Each method's docstring walks through the steps. Check your work as you go:

```bash
pytest -k pinned -v          # TODO 1
pytest -k trim -v            # TODO 2
pytest -v                    # everything
```

The tests use fakeredis, so they don't need the Redis server.

Once all 12 tests pass, try it for real:

```bash
python chat_demo.py --offline --verbose --summarize --summarize-after 6 --keep-last 2
```

After the fourth reply, find your summary transaction in the verbose output. Then type `/summary` and `/history`, and send one more message to see the summary in the `[context]` line. (Offline, the "summary" just lists what you said; with a real model such as `--model ollama:llama3.2:3b` it's a real summary.)

Questions to think about:

- Why does `summarize_and_trim` save the summary and trim the list in a single transaction?
- One test adds a message *while* the summarizer is running. Why does the obvious `LTRIM` fail it, and what does that tell you about real servers handling many requests?
- What does a summary lose, and what happens to early details after several rounds of re-summarizing?
- What sensitive information could end up in the summary, and how long does it live?

## Using a hosted Redis

Set `REDIS_URL` to your database's connection string (for example from Redis Cloud) and run as usual:

```bash
export REDIS_URL="redis://default:PASSWORD@HOST:PORT"      # PowerShell: $env:REDIS_URL = "..."
python chat_demo.py --offline --verbose
```

Setting it beforehand keeps the password out of your command line. `--verbose` never prints the password.

