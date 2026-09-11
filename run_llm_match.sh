#!/usr/bin/env bash
# Compile Tribes (plain javac, no Maven/Gradle) and run the LLM_PLAYER vs RULE_BASED match.
#
#   ./run_llm_match.sh              # GUI game (Java Swing window) + LLM log in this terminal
#   ./run_llm_match.sh --nogui      # headless, fastest
#   ./run_llm_match.sh --nogui --max-turns=10
#
# Start the bridge first in another terminal:  cd llm_bridge && uvicorn bridge:app --host 127.0.0.1 --port 8000
set -euo pipefail
cd "$(dirname "$0")"

# --- locate a Java runtime (macOS's /usr/bin/java is only a stub) ---------------------
if ! java -version >/dev/null 2>&1; then
  for cand in \
      "${JAVA_HOME:-}/bin/java" \
      "$(/usr/libexec/java_home 2>/dev/null || true)/bin/java" \
      /Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java \
      /usr/local/opt/openjdk*/bin/java /opt/homebrew/opt/openjdk*/bin/java \
      /usr/lib/jvm/*/bin/java; do
    if [ -x "$cand" ] 2>/dev/null && "$cand" -version >/dev/null 2>&1; then
      export PATH="$(dirname "$cand"):$PATH"; echo "Using Java at $cand"; break
    fi
  done
fi
if ! java -version >/dev/null 2>&1; then
  echo "ERROR: no Java runtime found. Install a JDK 11+ (e.g. 'brew install --cask temurin', or the .pkg from https://adoptium.net) and rerun."; exit 1
fi

if command -v javac >/dev/null 2>&1; then
  mkdir -p out
  find src -name '*.java' > out/sources.txt
  javac -nowarn -d out -cp lib/json.jar @out/sources.txt
elif [ -f out/Play.class ]; then
  echo "javac not found - using the precompiled classes in out/ (Java 11 bytecode)."
else
  echo "ERROR: no javac and no precompiled out/ directory. Install a JDK (e.g. 'brew install openjdk') or unpack Tribes-out-java11.tar.gz."; exit 1
fi

if ! curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "WARNING: nothing is listening on http://127.0.0.1:8000 - LLMAgent will fall back to END_TURN every call."
  echo "         Start it with:  cd llm_bridge && uvicorn bridge:app --host 127.0.0.1 --port 8000"
fi

exec java -cp out:lib/json.jar Play --llm "$@"
