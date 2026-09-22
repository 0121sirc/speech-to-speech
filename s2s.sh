#!/usr/bin/env bash
# Unified launcher for the local voice assistant.
# Usage: s2s.sh [start [OPTIONS]] | stop
set -euo pipefail

export S2S_HOME="${S2S_HOME:-/media/kg/DEV4T/github/s2s}"

MODE="web"
API_URL=""
API_KEY=""
MODEL="qwen3.5-4b"
HOST="0.0.0.0"
PORT=8765
WEB_PORT=7860
USE_PROXY=1
USE_SEARXNG=1

# Optional external OpenAI-compatible TTS base URL (must include /v1, e.g.
# http://127.0.0.1:8091/v1). Empty -> built-in Qwen3-TTS. When set and usable,
# the pipeline switches to `--tts openai`; if the probe fails we warn and fall
# back to the built-in Qwen3-TTS.
TTS_URL=""
OPENAI_TTS_VOICE="seed:1688"
TTS_MODE="qwen3"

# Self-hosted SearXNG (shared across projects; started on demand, left running
# on `stop` so other consumers keep working).
SEARXNG_HOME="${SEARXNG_HOME:-/media/kg/DEV4T/gitee/searxng}"
SEARXNG_CONTAINER="searxng"
SEARXNG_IMAGE="ghcr.nju.edu.cn/searxng/searxng:latest"

usage() {
  cat <<'EOF'
Usage: s2s.sh [start [OPTIONS]] | stop

Unified launcher for the local voice assistant
(Silero VAD -> Paraformer STT(zh) -> OpenAI-compatible LLM -> TTS).

TTS defaults to the built-in Qwen3-TTS. Pass --tts-url to use any external
OpenAI-compatible TTS server (e.g. the ChatTTS service in ChatTTS_colab)
instead; an unusable URL warns and falls back to Qwen3-TTS.

Commands:
  start [OPTIONS]   Start the assistant (default when the first arg is an option)
  stop              Gracefully stop any running s2s services
                    (TERM -> wait -> KILL fallback, then verify ports)

Options (for start):
  --api-url URL   LLM OpenAI-compatible endpoint (required)
                  e.g. http://100.120.234.5:1234/v1/
  --api-key KEY   LLM API key (required)
  --mode MODE     web | local   (default: web)
                    web   = realtime backend + browser UI on :WEB_PORT
                    local = microphone + speakers on this machine
  --model NAME    LLM model name
                  (default: qwen3.5-4b)
  --host HOST     realtime server bind host (web mode, default 0.0.0.0)
  --port PORT     realtime server port (default 8765)
  --web-port P    browser UI port (default 7860)
  --no-proxy      do not set the http/https proxy (default proxy: 127.0.0.1:7897)
  --no-searxng    do not start the shared SearXNG container (web search then
                  falls back to Bing scraping; default: started on :8888)
  --tts-url URL   external OpenAI-compatible TTS base URL, including /v1
                  (e.g. http://127.0.0.1:8091/v1 for ChatTTS). Default: built-in
                  Qwen3-TTS. An unusable URL warns and falls back to Qwen3-TTS.
  -h, --help      show this help

Examples:
  ./s2s.sh start --api-url http://100.120.234.5:1234/v1/ --api-key 1234
  ./s2s.sh start --api-url http://100.120.234.5:1234/v1/ --api-key 1234 \
    --tts-url http://127.0.0.1:8091/v1
  ./s2s.sh --mode local --api-url http://100.120.234.5:1234/v1/ --api-key 1234
  ./s2s.sh stop
EOF
}

# Gracefully stop all running s2s services (backend + web UI + launcher).
# Discovery uses specific patterns so this `stop` run never kills itself.
stop_services() {
  local stop_timeout="${STOP_TIMEOUT:-20}"
  local pids alive pid rc=0 ports deadline

  pids="$(pgrep -f "speech-to-speech (serve|local)" 2>/dev/null || true)
$(pgrep -f "uvicorn --app-dir .*demo server:app" 2>/dev/null || true)
$(pgrep -f "s2s\.sh .*--api-url" 2>/dev/null || true)"
  pids="$(printf '%s\n' "$pids" | grep -E '^[0-9]+$' | sort -un | tr '\n' ' ')"

  if [[ -z "${pids// /}" ]]; then
    echo "No running s2s services found."
    return 0
  fi

  echo "Stopping s2s services: $(echo $pids)"
  for pid in $pids; do
    kill -TERM "$pid" 2>/dev/null || true
  done

  deadline=$((SECONDS + stop_timeout))
  while (( SECONDS < deadline )); do
    alive=""
    for pid in $pids; do
      kill -0 "$pid" 2>/dev/null && alive="$alive $pid"
    done
    [[ -z "$alive" ]] && break
    sleep 1
  done

  for pid in $pids; do
    if kill -0 "$pid" 2>/dev/null; then
      echo "  force killing $pid"
      kill -9 "$pid" 2>/dev/null || true
      rc=1
    fi
  done

  if command -v ss >/dev/null 2>&1; then
    ports="$(ss -tln 2>/dev/null | grep -E ':(8765|7860)\b' || true)"
  else
    ports="$(netstat -tln 2>/dev/null | grep -E ':(8765|7860)\b' || true)"
  fi
  if [[ -n "$ports" ]]; then
    echo "Warning: ports 8765/7860 still in use:"
    echo "$ports"
    return 1
  fi

  echo "All s2s services stopped; ports 8765/7860 released."
  return "$rc"
}

# ── Subcommand dispatch ─────────────────────────────────────────────────────
if [[ "${1:-}" == "stop" ]]; then
  stop_services || exit $?
  exit 0
fi
if [[ "${1:-}" == "start" ]]; then
  shift
fi
if [[ $# -eq 0 ]]; then
  usage
  exit 0
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-url)   API_URL="$2"; shift 2 ;;
    --api-key)   API_KEY="$2"; shift 2 ;;
    --mode)      MODE="$2"; shift 2 ;;
    --model)     MODEL="$2"; shift 2 ;;
    --host)      HOST="$2"; shift 2 ;;
    --port)      PORT="$2"; shift 2 ;;
    --web-port)  WEB_PORT="$2"; shift 2 ;;
    --tts-url)   TTS_URL="$2"; shift 2 ;;
    --no-proxy)  USE_PROXY=0; shift ;;
    --no-searxng) USE_SEARXNG=0; shift ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "ERROR: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$API_URL" || -z "$API_KEY" ]]; then
  echo "ERROR: --api-url and --api-key are required" >&2
  usage >&2
  exit 2
fi

# ── Environment (was activate.sh) ───────────────────────────────────────────
# PATH: use the conda env's python/tools
export PATH="$S2S_HOME/.conda_env/bin:$PATH"
# CUDA 13 runtime libs required by qwentts-cpp (libcudart.so.13 / libcublas.so.13)
# plus env lib dir for libportaudio (sounddevice) and other bundled system libs
export LD_LIBRARY_PATH="$S2S_HOME/.conda_env/lib/python3.12/site-packages/nvidia/cu13/lib:$S2S_HOME/.conda_env/lib:${LD_LIBRARY_PATH:-}"
# model / cache inside the project dir
export HF_HOME="$S2S_HOME/hf_cache"
export HF_HUB_CACHE="$S2S_HOME/hf_cache/hub"
export HF_ENDPOINT=https://huggingface.co
export MODELSCOPE_CACHE="$S2S_HOME/modelscope_cache"
# NLTK data (punkt_tab / averaged_perceptron_tagger_eng) vendored inside the
# project so sentence splitting never depends on a download at runtime.
export NLTK_DATA="$S2S_HOME/nltk_data"
# Trust the CA bundle shipped with the env: urllib/NLTK otherwise reject the
# local proxy's certificate chain and `nltk.download` fails silently.
export SSL_CERT_FILE="$S2S_HOME/.conda_env/lib/python3.12/site-packages/certifi/cacert.pem"
export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
export CURL_CA_BUNDLE="$SSL_CERT_FILE"
# proxy for downloads (skip when --no-proxy)
if [[ "$USE_PROXY" == "1" ]]; then
  export http_proxy=http://127.0.0.1:7897/
  export https_proxy=http://127.0.0.1:7897/
  # Keep localhost service calls (SearXNG, external local TTS) off the proxy.
  export no_proxy="127.0.0.1,localhost,${no_proxy:-}"
  export NO_PROXY="$no_proxy"
fi

echo "s2s env ready: $(which python)"

# ── SearXNG (web-search backend, shared) ───────────────────────────────────
# The demo's /api/search proxies to a local SearXNG JSON API by default and
# falls back to a Bing scrape if it's down. Start/verify the container here so
# web search works out of the box; `stop` intentionally leaves it running since
# other projects may share it (skip with --no-searxng).
ensure_searxng() {
  if [[ "$USE_SEARXNG" != "1" ]]; then
    echo "SearXNG skipped (--no-searxng); web search falls back to Bing."
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "WARNING: docker not found; skipping SearXNG (web search falls back to Bing)."
    return 0
  fi
  if ! docker ps -a --format '{{.Names}}' | grep -qx "$SEARXNG_CONTAINER"; then
    echo "Creating SearXNG container ($SEARXNG_CONTAINER)..."
    docker run -d --name "$SEARXNG_CONTAINER" --network host --restart unless-stopped \
      -e SEARXNG_PORT=8888 -e FORCE_OWNERSHIP=false \
      -v "$SEARXNG_HOME/config:/etc/searxng" \
      -v "$SEARXNG_HOME/data:/var/cache/searxng" \
      "$SEARXNG_IMAGE" >/dev/null
  elif [[ "$(docker inspect -f '{{.State.Running}}' "$SEARXNG_CONTAINER" 2>/dev/null)" != "true" ]]; then
    echo "Starting SearXNG container ($SEARXNG_CONTAINER)..."
    docker start "$SEARXNG_CONTAINER" >/dev/null
  fi
  for _ in $(seq 1 60); do
    if (exec 3<>/dev/tcp/127.0.0.1/8888) 2>/dev/null; then
      exec 3>&- 2>/dev/null || true
      echo "SearXNG up on http://127.0.0.1:8888"
      return 0
    fi
    sleep 1
  done
  echo "WARNING: SearXNG did not open :8888 in time (web search falls back to Bing)."
}

# ── External TTS (--tts-url) ───────────────────────────────────────────────
# Probe the endpoint with a tiny synthesis request; only a 2xx with audio bytes
# counts as usable. On failure, warn and fall back to the built-in Qwen3-TTS so
# a mistyped/offline URL never blocks startup.
ensure_tts_url() {
  TTS_MODE="qwen3"
  if [[ -z "$TTS_URL" ]]; then
    return 0
  fi

  if ! command -v curl >/dev/null 2>&1; then
    echo "WARNING: curl not found; cannot verify --tts-url. Falling back to built-in Qwen3-TTS." >&2
    TTS_URL=""
    return 0
  fi

  local probe_url="${TTS_URL%/}/audio/speech"
  local probe_out http_code size
  probe_out="$(mktemp)"
  http_code="$(curl -sS --max-time 15 --noproxy '*' -o "$probe_out" -w '%{http_code}' \
    -X POST "$probe_url" -H 'Content-Type: application/json' \
    -d "{\"input\":\"你好\",\"voice\":\"$OPENAI_TTS_VOICE\",\"response_format\":\"pcm\"}" 2>/dev/null || true)"
  size="$(stat -c%s "$probe_out" 2>/dev/null || echo 0)"
  rm -f "$probe_out"

  if [[ "$http_code" == "200" && "$size" -gt 0 ]]; then
    TTS_MODE="openai"
    echo "TTS: using external OpenAI-compatible endpoint $TTS_URL"
  else
    echo "WARNING: --tts-url '$TTS_URL' is not usable (http=${http_code:-none}, bytes=${size}); falling back to built-in Qwen3-TTS." >&2
    TTS_URL=""
  fi
}

ensure_tts_url
export TTS_URL

TTS_ARGS=()
if [[ "$TTS_MODE" == "openai" ]]; then
  TTS_ARGS=(
    --tts openai
    --openai_tts_base_url "$TTS_URL"
    --openai_tts_model chattts
    --openai_tts_voice "$OPENAI_TTS_VOICE"
    --openai_tts_response_format pcm
    --openai_tts_sample_rate 24000
  )
else
  TTS_ARGS=(
    --tts qwen3
    --qwen3_tts_backend ggml
    --qwen3_tts_ggml_quantization Q8_0
    --qwen3_tts_gguf_talker_path "$S2S_HOME/models/qwen-talker-1.7b-customvoice-Q8_0.gguf"
    --qwen3_tts_gguf_codec_path "$S2S_HOME/models/qwen-tokenizer-12hz-Q8_0.gguf"
  )
fi

COMMON=(
  --thresh 0.5 --min_speech_ms 300 --min_silence_ms 400
  --stt paraformer --paraformer_stt_device cpu
  --llm_backend responses-api
  "${TTS_ARGS[@]}"
  --model_name "$MODEL"
  --responses_api_base_url "$API_URL"
  --responses_api_api_key "$API_KEY"
  --responses_api_stream
  --enable_live_transcription
)

if [[ "$MODE" == "local" ]]; then
  exec speech-to-speech local --no_smart_turn "${COMMON[@]}"
fi

if [[ "$MODE" != "web" ]]; then
  echo "ERROR: unknown --mode '$MODE' (web | local)" >&2
  exit 2
fi

mkdir -p "$S2S_HOME/logs"
export SPEECH_TO_SPEECH_URL="ws://localhost:$PORT/v1/realtime"

ensure_searxng

speech-to-speech serve --host "$HOST" --port "$PORT" "${COMMON[@]}" \
  > "$S2S_HOME/logs/backend_realtime.log" 2>&1 &
BACKEND_PID=$!
trap 'echo; echo "Stopping backend (PID $BACKEND_PID)"; kill $BACKEND_PID 2>/dev/null || true' EXIT INT TERM

echo "Realtime backend starting (PID $BACKEND_PID) on ws://$HOST:$PORT ..."
for _ in $(seq 1 120); do
  if grep -q "v1/realtime" "$S2S_HOME/logs/backend_realtime.log" 2>/dev/null; then
    echo "Realtime backend is up."
    break
  fi
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "Backend died. Last log lines:"; tail -30 "$S2S_HOME/logs/backend_realtime.log"; exit 1
  fi
  sleep 1
done

echo "Web UI: http://localhost:$WEB_PORT  (Ctrl-C to stop)"
cd "$S2S_HOME/speech-to-speech"
python -m uvicorn --app-dir demo server:app --host 0.0.0.0 --port "$WEB_PORT"
