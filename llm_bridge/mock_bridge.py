"""
Stdlib-only stand-in for bridge.py (no FastAPI / Anthropic needed).

Implements the same POST /act contract so the Java LLMAgent can be smoke-tested
without an API key or network access. Picks actions with a tiny priority
heuristic and returns {"action_index": int, "reasoning": str}.

Run:
    python llm_bridge/mock_bridge.py
"""

import json
import random
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PRIORITY = [
    "CAPTURE", "EXAMINE", "LEVEL_UP", "RESOURCE_GATHERING", "RESEARCH_TECH",
    "SPAWN", "BUILD", "ATTACK", "RECOVER", "MOVE", "BUILD_ROAD",
]
_rng = random.Random(42)


def choose(actions):
    for kw in PRIORITY:
        idxs = [i for i, a in enumerate(actions) if a.upper().startswith(kw)]
        if idxs:
            return _rng.choice(idxs), f"mock heuristic: prefer {kw}"
    for i, a in enumerate(actions):
        if a.upper().startswith("END_TURN"):
            return i, "mock heuristic: nothing else useful, end turn"
    return 0, "mock heuristic: default index 0"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "mock": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/act":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n))
            for key, typ in (("turn", int), ("stars", int), ("score", int), ("context", str), ("actions", list)):
                if not isinstance(req.get(key), typ):
                    raise ValueError(f"field '{key}' missing or not {typ.__name__}")
        except Exception as e:  # noqa: BLE001
            print(f"[mock] bad payload: {e}", flush=True)
            return self._send(400, {"error": str(e)})

        actions = req["actions"]
        idx, why = choose(actions) if actions else (0, "no actions")
        print("=" * 70, flush=True)
        print(f"[mock] turn={req['turn']} stars={req['stars']} score={req['score']} n_actions={len(actions)}", flush=True)
        print(f"[mock] context head: {req['context'][:160].replace(chr(10), ' | ')}", flush=True)
        print(f"[mock] CHOSEN [{idx}] {actions[idx] if actions else '-'}  ({why})", flush=True)
        self._send(200, {"action_index": idx, "reasoning": why})

    def log_message(self, *args):  # silence default access log
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"[mock] serving on http://127.0.0.1:{port}/act", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
