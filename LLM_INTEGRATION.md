# Tribes × Claude — LLM agent integration

An `LLM_PLAYER` agent for the GAIGResearch **Tribes** (Polytopia) simulator. Every decision is made by
Claude (default `claude-sonnet-5`, adaptive thinking) through a small local FastAPI bridge.

> The plan originally targeted `claude-3-7-sonnet-20250219` with `budget_tokens: 2048`. That model has since
> been retired from the API (404), and current models use adaptive thinking
> (`thinking: {"type": "adaptive"}` + `output_config.effort`) — the legacy `budget_tokens` form returns 400 on
> them. The bridge defaults to Sonnet 5 / adaptive / effort=high; `LLM_BRIDGE_THINKING_MODE=enabled`
> restores the legacy form for older models such as `claude-haiku-4-5-20251001`.

```
Tribes (Java)                                   llm_bridge/bridge.py (Python)
┌─────────────────────────┐  POST /act (JSON)   ┌──────────────────────────────┐   messages.create
│ Game loop → LLMAgent.act│ ──────────────────► │ FastAPI  →  prompt builder   │ ─────────────────► Claude Sonnet 5
│  · legal actions        │ ◄────────────────── │ JSON parse + safe fallback   │ ◄───────────────── (adaptive thinking)
│  · state → context text │ {action_index,      └──────────────────────────────┘
│  · 60 s timeout/fallback│  reasoning}
└─────────────────────────┘
```

## What was added / changed

| File | Change |
|---|---|
| `llm_bridge/requirements.txt` | fastapi, uvicorn, anthropic, pydantic, python-dotenv |
| `llm_bridge/bridge.py` | FastAPI app on `127.0.0.1:8000`, `POST /act`, `GET /health`. Calls `claude-sonnet-5` with `thinking={"type":"adaptive"}` and `output_config.effort` (env-configurable), prints the model's thinking + reasoning, enforces `{"action_index": int, "reasoning": str}` with fallback |
| `llm_bridge/mock_bridge.py` | Dependency-free stand-in (same contract) for offline smoke tests — no API key needed |
| `llm_bridge/.env.example` | Template for `ANTHROPIC_API_KEY` |
| `src/players/LLMAgent.java` | The agent: serialises state, POSTs via `java.net.http.HttpClient` (60 s timeout, `-Dllm.bridge.timeout`), returns the chosen `Action`, falls back safely |
| `src/Run.java` | `PlayerType.LLM_PLAYER` (+ `RULE_BASED` alias of the built-in `SIMPLE` agent); `getAgent()` instantiates `new LLMAgent(seed)`; `"LLM_PLAYER"` accepted in `play.json` |
| `src/Play.java` | Built-in default match `LLM_PLAYER` vs `RULE_BASED` on the 11×11 `levels/SampleLevel2p.csv` (Capitals mode). Flags: `--llm`, `--nogui`, `--max-turns=N` |
| `play.json` | Now configures the same LLM vs Rule Based match (original kept as `play.original.json`) |
| `run_llm_match.sh` | Compile + run helper (finds a JDK on macOS, uses precompiled `out/` if no `javac`) |
| `start_llm_game.sh` | One-shot launcher: venv + deps, (re)start bridge, run game |

Note: this repo keeps `PlayerType` as a nested enum inside `Run.java` — there is no separate
`src/players/PlayerType.java` — so that is where `LLM_PLAYER` was added.

## Requirements

* JDK 11+ (`java.net.http.HttpClient` is used; tested on JDK 21)
* Python 3.10+
* An Anthropic API key

## 1. Start the bridge

```bash
cd Tribes/llm_bridge
python3 -m venv .venv && source .venv/bin/activate        # optional
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...                        # or: cp .env.example .env && edit it
uvicorn bridge:app --host 127.0.0.1 --port 8000
#   (equivalently:  python bridge.py)
curl http://127.0.0.1:8000/health                          # {"ok":true,"llm_available":true,...}
```

No key / offline? Use the mock instead — same endpoint, tiny heuristic, zero dependencies:

```bash
python3 llm_bridge/mock_bridge.py
```

## 2. Launch the game

Command line (from the `Tribes` directory):

```bash
./run_llm_match.sh                 # GUI window + LLM log in the terminal
./run_llm_match.sh --nogui         # headless (no Swing window), fastest
./run_llm_match.sh --nogui --max-turns=10
```

or by hand:

```bash
mkdir -p out && find src -name '*.java' > out/sources.txt
javac -nowarn -d out -cp lib/json.jar @out/sources.txt
java -cp out:lib/json.jar Play --llm            # add --nogui for headless
java -cp out:lib/json.jar Play                  # reads play.json (also set to LLM vs Rule Based)
```

IDE (IntelliJ): mark `src` as Sources Root, add `lib/json.jar` as a library, set the working directory
to the repo root, then run `Play.main()` — with program arguments `--llm` (add `--nogui` for headless).

Useful JVM properties: `-Dllm.bridge.url=http://127.0.0.1:8000/act`, `-Dllm.bridge.timeout=60`,
`-Dllm.bridge.log=false`.

## 3. What you see

**Bridge terminal** — the model's extended thinking, its pick, and its reasoning for every call:

```
[bridge] turn=2 stars=7 score=500 legal_actions=19
[bridge] --- model thinking ---
The capital is coastal with fish on three tiles; FISHING pays back immediately ...
[bridge] --- end thinking ---
[bridge] CHOSEN index 0: RESEARCH_TECH by tribe 0 : FISHING
[bridge] reasoning: Fishing unlocks three adjacent fish tiles for cheap population.
[bridge] 6.41s
```

**Game terminal** — the state summary and the action actually executed by the engine:

```
[LLMAgent] ---- turn 2 | tribe XIN_XI | stars 7 | score 500 | action #1 | 19 legal actions
[LLMAgent] EXECUTE [0] RESEARCH_TECH by tribe 0 : FISHING
[LLMAgent] reasoning: Fishing unlocks three adjacent fish tiles for cheap population.
...
[LLMAgent] game over. reward=... bridge calls=143 fallbacks=0 final score=...
```

## How it works (details worth knowing)

* **One call per action, not per turn.** Tribes asks the agent for one `Action` at a time and keeps
  asking until `END_TURN` is returned (or nothing is left). The prompt tells the model this, and the
  bridge shows the last 12 decisions so it doesn't dither. Expect ~5–15 calls per turn early on and
  more later; at effort=high expect roughly 5–15 s per action (effort=medium ~3–8 s but the model often skips thinking).
* **Payload** (`POST /act`): `turn`, `stars`, `score`, `context` (tech, own cities/units with
  HP/ATK/DEF/MOV/status, visible enemy cities/units, ASCII map with resources), `actions`
  (each action's `toString()` plus unit type / attack-target details). Extra keys
  `player_id`, `tribe`, `action_number_this_turn` are also sent and shown to the model.
* **Fallbacks.** Bridge side: unparsable JSON, out-of-range index, API error/timeout, or missing
  key → `END_TURN` index if present, else `0`. Java side: connection refused, HTTP error,
  60 s timeout, bad JSON, or bad index → `END_TURN` if legal, else `actions.get(0)`. So a dead bridge
  simply makes the LLM player pass; the game never crashes. `DESTROY`/`DISBAND` are filtered out
  of the offered actions (as the built-in `SimpleAgent` does).
* **Turn timers.** `Constants.TURN_TIME_LIMITED` is `false` in this repo, so the engine will not
  cut the LLM off mid-turn.
* **Observability.** `Constants.PLAY_WITH_FULL_OBS = true` by default, so the "visible" map is the
  whole map. Flip it to `false` for genuine fog of war — `LLMAgent` already checks
  `Tribe.isVisible()` per tile.
* **Cost control.** `LLM_BRIDGE_EFFORT` (low/medium/high), `LLM_BRIDGE_MODEL` and
  `LLM_BRIDGE_THINKING_MODE` env vars (see `llm_bridge/.env.example`) override the defaults; `--max-turns=N` bounds a game (default 50 in Capitals mode).
