"""
LLM bridge for the Tribes (Polytopia) simulator.

Exposes POST /act on 127.0.0.1:8000. The Java LLMAgent sends one request per
*action* (Tribes asks the agent for one action at a time until END_TURN is
chosen or no actions remain). The bridge asks Claude (default claude-sonnet-5, adaptive
thinking enabled, to pick exactly one action index, and returns

    {"action_index": <int>, "reasoning": "<str>"}

Robustness contract (the game loop must never crash because of us):
  * any exception, malformed JSON, or out-of-range index -> safe fallback index
  * the fallback prefers END_TURN if present, otherwise index 0

Run:
    export ANTHROPIC_API_KEY=sk-ant-...        (or put it in llm_bridge/.env)
    uvicorn bridge:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import deque
from typing import Deque, List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

load_dotenv()  # picks up ANTHROPIC_API_KEY from llm_bridge/.env if present

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
# claude-3-7-sonnet-20250219 (the original target) has been retired from the API.
# Current models use *adaptive* thinking (thinking={"type":"adaptive"} + output_config.effort);
# the legacy {"type":"enabled","budget_tokens":N} form is only accepted by older models such as
# claude-haiku-4-5-20251001 and returns HTTP 400 on Sonnet 5 / Opus 5.
MODEL = os.getenv("LLM_BRIDGE_MODEL", "claude-sonnet-5")
THINKING_MODE = os.getenv("LLM_BRIDGE_THINKING_MODE", "adaptive").lower()  # adaptive | enabled | off
EFFORT = os.getenv("LLM_BRIDGE_EFFORT", "high").lower()   # low|medium|high|xhigh|max — below "high" the model
                                                           # often skips thinking on easy moves (no thinking log)
THINKING_DISPLAY = os.getenv("LLM_BRIDGE_THINKING_DISPLAY", "summarized")  # summarized | omitted (adaptive mode)
THINKING_BUDGET = int(os.getenv("LLM_BRIDGE_THINKING_BUDGET", "2048"))     # only for THINKING_MODE=enabled
MAX_TOKENS = int(os.getenv("LLM_BRIDGE_MAX_TOKENS", "8000"))
if THINKING_MODE == "enabled":
    MAX_TOKENS = max(MAX_TOKENS, THINKING_BUDGET + 1024)  # legacy rule: max_tokens > budget_tokens
API_TIMEOUT_S = float(os.getenv("LLM_BRIDGE_API_TIMEOUT", "55"))  # Java side times out at 60s (llm.bridge.timeout)
HISTORY_LEN = 12  # recent decisions shown back to the model to discourage dithering

app = FastAPI(title="Tribes LLM bridge", version="1.0")

# Lazily-initialised Anthropic client (so a missing key degrades to fallback, not a crash).
_client = None
_client_error: Optional[str] = None


def _get_client():
    global _client, _client_error
    if _client is not None or _client_error is not None:
        return _client
    try:
        import anthropic  # imported here so the module loads even if the SDK is missing

        _client = anthropic.Anthropic(timeout=API_TIMEOUT_S, max_retries=0)
        # The SDK raises lazily; force an early, explicit check on the key.
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
    except Exception as e:  # noqa: BLE001
        _client_error = f"{type(e).__name__}: {e}"
        _client = None
        _log(f"WARNING: Anthropic client unavailable -> every request will use the safe fallback ({_client_error})")
    return _client


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class ActRequest(BaseModel):
    """Payload sent by LLMAgent.java. Extra keys are accepted and ignored."""

    model_config = ConfigDict(extra="allow")

    turn: int
    stars: int
    score: int
    context: str = Field(description="Human-readable summary of the visible board, cities and units")
    actions: List[str] = Field(description="Serialized legal actions; the reply indexes into this list")


class ActResponse(BaseModel):
    action_index: int
    reasoning: str


# --------------------------------------------------------------------------- #
# Prompting
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """You are an expert player of The Battle of Polytopia, controlling one tribe in the
GAIGResearch "Tribes" simulator. You are called once per ACTION, not once per turn: each call you
choose exactly one legal action from a numbered list, the engine executes it, and you are called
again with the updated state until you choose END_TURN (or no actions remain).

Strategic priorities (roughly in order):
1. Economy first: capture nearby villages with units (CAPTURE), harvest resources that grow
   population (RESOURCE_GATHERING on fruit/animals/fish/crops), and level cities up (LEVEL_UP);
   choose level-up rewards that compound (workshop/explorer early, city walls when threatened,
   park/super unit later).
2. Research technologies that unlock what your surroundings offer (e.g. FISHING near water,
   HUNTING near animals, ORGANIZATION near fruit, RIDING for mobility, ARCHERY/SHIELDS for defence).
3. Explore with cheap units early to find villages and ruins (EXAMINE ruins for free rewards).
   ENGINE RULE: EXAMINE and CAPTURE are only offered to a unit that is FRESH (has not acted this
   turn) and is already standing on the ruins/village. So: move onto the tile this turn, and at the
   START of the next turn choose EXAMINE/CAPTURE for that unit before moving it anywhere. Walking
   a unit off ruins or a village without examining/capturing throws the reward away.
4. Keep units productive: a unit that has already moved and attacked this turn cannot act again.
   Do not shuffle a unit back and forth between the same tiles.
5. Attack when the trade is favourable (your ATK vs their DEF/HP, terrain, and whether the
   enemy can retaliate). Defend your capital; in "Capitals" mode losing it loses the game.
6. Stars are income: don't hoard them pointlessly, but don't spend on units you can't house.
7. Choose END_TURN only when nothing useful remains (no affordable productive actions, all
   units used). Ending the turn too early wastes tempo; refusing to end it wastes API calls.

Output format: reply with ONLY a single JSON object on one line, no markdown fences, no prose:
{"action_index": <integer index into the actions list>, "reasoning": "<one or two sentences>"}
"""


def _find_end_turn(actions: List[str]) -> Optional[int]:
    for i, a in enumerate(actions):
        if a.upper().startswith("END_TURN"):
            return i
    return None


def safe_fallback_index(actions: List[str]) -> int:
    """Index that can never harm the game loop: END_TURN if available, else 0."""
    if not actions:
        return 0
    et = _find_end_turn(actions)
    return et if et is not None else 0


def build_user_prompt(req: ActRequest, history: Deque[Tuple[int, str]]) -> str:
    action_lines = "\n".join(f"[{i}] {a}" for i, a in enumerate(req.actions))
    hist_lines = (
        "\n".join(f"  turn {t}: {a}" for t, a in history) if history else "  (none yet)"
    )
    extra = {k: v for k, v in (req.model_extra or {}).items()}
    extra_txt = f"\nAdditional info: {json.dumps(extra)}" if extra else ""
    return (
        f"TURN {req.turn} | STARS {req.stars} | SCORE {req.score}{extra_txt}\n\n"
        f"=== SITUATION ===\n{req.context}\n\n"
        f"=== YOUR RECENT ACTIONS (most recent last) ===\n{hist_lines}\n\n"
        f"=== LEGAL ACTIONS ({len(req.actions)}) ===\n{action_lines}\n\n"
        "Pick the single best action index now. Respond with only the JSON object."
    )


_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)


def parse_decision(text: str, n_actions: int) -> Tuple[Optional[int], str]:
    """Extract (action_index, reasoning). Returns (None, msg) when unusable."""
    if not text:
        return None, "empty model response"
    candidates = [text.strip()]
    # strip ```json fences if the model added them despite instructions
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    candidates.append(fenced)
    candidates.extend(m.group(0) for m in _JSON_RE.finditer(text))
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(obj, dict) or "action_index" not in obj:
            continue
        try:
            idx = int(obj["action_index"])
        except (TypeError, ValueError):
            return None, f"non-integer action_index: {obj.get('action_index')!r}"
        reasoning = str(obj.get("reasoning", "")).strip()
        if 0 <= idx < n_actions:
            return idx, reasoning
        return None, f"action_index {idx} out of range [0, {n_actions})"
    return None, f"no JSON object with action_index found in: {text[:200]!r}"


# --------------------------------------------------------------------------- #
# Model call
# --------------------------------------------------------------------------- #
def ask_claude(req: ActRequest, history: Deque[Tuple[int, str]]) -> Tuple[Optional[int], str, str]:
    """Returns (index or None, reasoning/diagnostic, thinking_text)."""
    client = _get_client()
    if client is None:
        return None, f"LLM unavailable ({_client_error})", ""

    kwargs = dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt(req, history)}],
    )
    if THINKING_MODE == "adaptive":
        # display="summarized" is required on Sonnet 5 / Opus 5 to get the thinking text back at
        # all (the default "omitted" returns empty thinking blocks even though you pay for them).
        kwargs["thinking"] = {"type": "adaptive", "display": THINKING_DISPLAY}
        # output_config is passed via extra_body so this works on any SDK version that
        # has not yet added a typed parameter for it.
        kwargs["extra_body"] = {"output_config": {"effort": EFFORT}}
    elif THINKING_MODE == "enabled":
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}
    resp = client.messages.create(**kwargs)

    thinking_parts, text_parts = [], []
    for block in resp.content:
        btype = getattr(block, "type", "")
        if btype == "thinking":
            thinking_parts.append(getattr(block, "thinking", ""))
        elif btype == "text":
            text_parts.append(getattr(block, "text", ""))
    thinking = "\n".join(p for p in thinking_parts if p)
    text = "\n".join(text_parts)

    idx, reasoning = parse_decision(text, len(req.actions))
    return idx, reasoning, thinking


# --------------------------------------------------------------------------- #
# HTTP endpoints
# --------------------------------------------------------------------------- #
_history: Deque[Tuple[int, str]] = deque(maxlen=HISTORY_LEN)
_stats = {"requests": 0, "fallbacks": 0}


def _log(msg: str) -> None:
    print(msg, flush=True)


@app.get("/health")
def health():
    _get_client()
    return {
        "ok": True,
        "model": MODEL,
        "thinking": THINKING_MODE,
        "effort": EFFORT if THINKING_MODE == "adaptive" else None,
        "llm_available": _client is not None,
        "llm_error": _client_error,
        **_stats,
    }


@app.post("/act", response_model=ActResponse)
def act(req: ActRequest) -> ActResponse:
    _stats["requests"] += 1
    t0 = time.time()
    n = len(req.actions)
    fallback = safe_fallback_index(req.actions)

    _log("\n" + "=" * 78)
    _log(f"[bridge] turn={req.turn} stars={req.stars} score={req.score} legal_actions={n}")

    if n == 0:
        _stats["fallbacks"] += 1
        _log("[bridge] no actions supplied -> fallback 0")
        return ActResponse(action_index=0, reasoning="No legal actions supplied; fallback.")

    idx: Optional[int] = None
    reasoning = ""
    thinking = ""
    try:
        idx, reasoning, thinking = ask_claude(req, _history)
    except Exception as e:  # noqa: BLE001 — network, auth, rate-limit, timeout, anything
        reasoning = f"LLM call failed: {type(e).__name__}: {e}"

    if thinking:
        _log("[bridge] --- model thinking ---")
        _log(thinking.strip())
        _log("[bridge] --- end thinking ---")
    elif idx is not None:
        _log(f"[bridge] (no thinking text returned — effort={EFFORT}, display={THINKING_DISPLAY})")

    if idx is None:
        _stats["fallbacks"] += 1
        _log(f"[bridge] FALLBACK ({reasoning}) -> index {fallback}: {req.actions[fallback]}")
        idx = fallback
        reasoning = f"[fallback] {reasoning}"
    else:
        _log(f"[bridge] CHOSEN index {idx}: {req.actions[idx]}")
        _log(f"[bridge] reasoning: {reasoning}")

    _history.append((req.turn, req.actions[idx]))
    _log(f"[bridge] {time.time() - t0:.2f}s")
    return ActResponse(action_index=idx, reasoning=reasoning)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("bridge:app", host="127.0.0.1", port=8000, log_level="warning")
