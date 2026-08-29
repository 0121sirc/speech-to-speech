#!/usr/bin/env bash
# Unified launcher for the local voice assistant.
# All config via command-line arguments (no env vars required).
set -euo pipefail

S2S_HOME="${S2S_HOME:-/media/kg/DEV4T/s2s}"

MODE="web"
API_URL=""
API_KEY=""
MODEL="qwen3.5-4b"
HOST="0.0.0.0"
PORT=8765
WEB_PORT=7860
USE_PROXY=1

usage() {
  cat <<'EOF'
Usage: s2s.sh [OPTIONS]

Unified launcher for the local voice assistant
(Silero VAD -> Paraformer STT(zh) -> OpenAI-compatible LLM -> Qwen3-TTS).

Options:
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
  --no-proxy      do not use the http/https proxy set in activate.sh
  -h, --help      show this help

Examples:
  ./s2s.sh --api-url http://100.120.234.5:1234/v1/ --api-key 1234
  ./s2s.sh --mode local --api-url http://100.120.234.5:1234/v1/ --api-key 1234
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-url)   API_URL="$2"; shift 2 ;;
    --api-key)   API_KEY="$2"; shift 2 ;;
    --mode)      MODE="$2"; shift 2 ;;
    --model)     MODEL="$2"; shift 2 ;;
    --host)      HOST="$2"; shift 2 ;;
    --port)      PORT="$2"; shift 2 ;;
    --web-port)  WEB_PORT="$2"; shift 2 ;;
    --no-proxy)  USE_PROXY=0; shift ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "ERROR: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$API_URL" || -z "$API_KEY" ]]; then
  echo "ERROR: --api-url and --api-key are required" >&2
  usage >&2
  exit 2
fi

# Environment: PATH, CUDA13 libs, HF/proxy, model caches
source "$S2S_HOME/activate.sh"
if [[ "$USE_PROXY" == "0" ]]; then
  unset http_proxy https_proxy
fi

COMMON=(
  --thresh 0.5 --min_speech_ms 300 --min_silence_ms 400
  --stt paraformer --paraformer_stt_device cpu
  --llm_backend responses-api
  --tts qwen3
  --qwen3_tts_backend ggml
  --qwen3_tts_ggml_quantization Q8_0
  --qwen3_tts_gguf_talker_path "$S2S_HOME/models/qwen-talker-1.7b-customvoice-Q8_0.gguf"
  --qwen3_tts_gguf_codec_path "$S2S_HOME/models/qwen-tokenizer-12hz-Q8_0.gguf"
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
