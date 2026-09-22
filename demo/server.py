"""
Tiny server for the speech-to-speech demo.

The demo used to ship as a `sdk: static` Space, but the web-search tool needs a
search backend the browser must NOT talk to directly. A static Space has no
runtime process, so it can't hold a secret or proxy anything. This server fixes
that: it serves the unchanged front-end AND exposes a same-origin `/api/search`
proxy. Search works out of the box via a self-hosted SearXNG instance (no key,
no registration), falling back to a direct Bing scrape; when a Serper key is
configured — or a user supplies one — it uses Serper instead (see docs/adr/0001).

Everything lives in one container; the speech-to-speech backend stays a separate,
load-balanced service the browser talks to over WebSocket as before. The load
balancer's address is a secret too (like the Serper key): the browser never sees
it. `/api/session` proxies the session handshake server-side so only the
per-session compute URL the LB hands back (which the browser must dial) is exposed.

On the deployed Space the server also meters conversation time by HF login tier
(anonymous / signed-in / PRO) — see `limiter.py` and `auth.py`. That whole feature
is off unless BOTH `LOAD_BALANCER_URL` and `SPACE_ID` are set, so it runs only on
the live Space, never locally (even with the LB exported for testing).

`SPEECH_TO_SPEECH_URL` overrides everything: when set, the LB logic above is
disabled entirely (no session proxy, no queue, no metering, no sign-in) and the
browser connects directly to that URL, shown read-only in Settings.

Endpoints:
  GET  /api/config           -> { search, searchKey, lb, allowDirect, s2sUrl, rtc, iceServers, auth }
  GET  /api/me               -> login + tier + remaining budget (LB mode only)
  POST /api/search           -> { results, answer }  SearXNG (keyless) or Bing or Google via Serper.dev
  POST /api/calls            -> proxies the WebRTC SDP offer to <s2s>/v1/realtime/calls
  POST /api/session          -> proxies <LB>/session: a grant, or a queue ticket
  GET  /api/queue/{id}       -> proxies <LB>/queue/{id}: position, or a grant on claim
  DELETE /api/queue/{id}     -> leave the queue (explicit "Leave queue" button)
  POST /api/queue/end        -> leave the queue (sendBeacon on teardown)
  POST /api/session/heartbeat-> extend the reservation; { expired }
  POST /api/session/end      -> reconcile + refund (sendBeacon on teardown)
  /*                         -> static files (index.html, main.js, ...)

When every compute slot is busy the load balancer hands back a queue ticket
instead of a grant; the browser polls /api/queue/{id} until it reaches the front
and a slot frees. Waiting reserves nothing — the daily budget is only reserved at
the moment a slot is actually claimed (a grant), never while queued.
"""

import asyncio
import html as html_lib
import json
import logging
import os
import re
from urllib.parse import urlsplit, urlunsplit

import auth
import httpx
import limiter
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logger = logging.getLogger("s2s.search")

SERPER_KEY = os.environ.get("SERPER_API_KEY", "").strip()
# Speech-to-speech load balancer URL. When set, the browser POSTs /api/session
# (which proxies <lb>/session here, server-side) and connects to the URL the LB
# returns (the original flow). The LB address itself is never sent to the browser.
# When empty, the user may instead set a direct s2s server URL in Settings and the
# browser connects to it straight (no load balancer).
LOAD_BALANCER_URL = os.environ.get("LOAD_BALANCER_URL", "").strip()
# Direct s2s server URL pinned by the deploy. Takes priority over the load
# balancer: when set, ALL LB logic is disabled (no /api/session proxy, no queue,
# no limiter, no sign-in) and the browser connects to this URL directly. Unlike
# the LB address it is NOT a secret — /api/config sends it to the client, which
# shows it read-only in Settings.
SPEECH_TO_SPEECH_URL = os.environ.get("SPEECH_TO_SPEECH_URL", "").strip()
if SPEECH_TO_SPEECH_URL:
    LOAD_BALANCER_URL = ""
# HF injects SPACE_ID ("owner/space") into every Space runtime; it's absent
# locally and on a plain `docker run`. We meter conversation time ONLY on the
# deployed Space — i.e. when BOTH the LB is configured AND we're on a Space.
# Off-Space (local dev, even with the LB exported) the app still proxies the LB,
# but nothing is metered: no budget, no reservations, no sign-in gating.
SPACE_ID = os.environ.get("SPACE_ID", "").strip()
LIMITER_ENABLED = bool(LOAD_BALANCER_URL) and bool(SPACE_ID)


def _parse_ice_servers(raw: str) -> list:
    """ICE servers for the browser's RTCPeerConnection, from RTC_ICE_SERVERS.

    Accepts a JSON list of RTCIceServer dicts (same format as the s2s
    server's SPEECH_TO_SPEECH_ICE_SERVERS, e.g.
    ``[{"urls": "turn:t.example.com", "username": "u", "credential": "c"}]``),
    a single such dict, or a plain comma-separated list of STUN/TURN URLs.
    Empty when unset — host candidates only, which is fine for local use."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except ValueError:
        pass
    return [{"urls": u.strip()} for u in raw.split(",") if u.strip()]


RTC_ICE_SERVERS = _parse_ice_servers(os.environ.get("RTC_ICE_SERVERS", ""))
DEFAULT_STARTUP_GREETING = (
    "Start the conversation now with a brief, spontaneous greeting in character. "
    "Keep it to one sentence, invite the user in naturally, and vary the wording each time."
)
# Exposed to the browser through /api/config. Set an empty value to disable the
# automatic greeting without changing the client bundle.
STARTUP_GREETING = os.environ.get("STARTUP_GREETING", DEFAULT_STARTUP_GREETING).strip()


def _webrtc_calls_url(s2s_url: str) -> str:
    """Derive the WebRTC handshake URL from the pinned realtime URL.

    ``ws://host:port/v1/realtime`` -> ``http://host:port/v1/realtime/calls``
    (ws->http, wss->https; a bare host gets the default /v1/realtime path,
    mirroring the client's buildDirectWsUrl normalisation)."""
    s = s2s_url.strip()
    if not s.startswith(("ws://", "wss://", "http://", "https://")):
        s = "http://" + s
    parts = urlsplit(s)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    path = parts.path if parts.path not in ("", "/") else "/v1/realtime"
    return urlunsplit((scheme, parts.netloc, path.rstrip("/") + "/calls", parts.query, ""))


SERPER_URL = "https://google.serper.dev/search"
# Self-hosted SearXNG instance (Docker, --network host). Primary keyless search
# backend: aggregates baidu/quark/sogou/360/bing/duckduckgo/... over the local
# proxy, so Chinese queries get proper results without a third-party key.
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
# External OpenAI-compatible TTS base URL (s2s.sh --tts-url). Empty means the
# built-in Qwen3-TTS. Only used to expose the available voice list to the UI.
TTS_URL = os.environ.get("TTS_URL", "").strip().rstrip("/")
QWEN3_VOICES = ["Aiden", "Ryan", "Dylan", "Eric", "Ono_Anna", "Serena", "Sohee", "Uncle_Fu", "Vivian"]
# Keyless fallback: Bing's HTML endpoint (no API key, no registration). The
# Chinese endpoint is reachable directly from residential IPs (the main site is
# not reliably reachable behind some networks).
BING_URL = "https://cn.bing.com/search"
BING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
# Each organic result is an <li class="b_algo"> block; the class may carry more
# tokens (e.g. `b_algo b_rmb`), so match the leading prefix, not the exact token.
BING_ALGO_RE = re.compile(r'<li class="b_algo[^"]*"', re.I)
# Cap results so the tool output stays small enough to feed back to the model.
MAX_RESULTS = 5


def _strip_tags(text: str) -> str:
    """Drop HTML tags and unescape entities (titles/snippets from Bing)."""
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", text)).strip()


async def _bing_search(query: str) -> dict:
    """Keyless web search via Bing's HTML endpoint (cn.bing.com).

    No API key, no registration — the default when no Serper key is available.
    ``trust_env=False`` pins the connection to a direct route instead of
    inheriting the proxy env vars s2s.sh exports (the proxy exit IP is fine for
    Bing, but a direct residential route is the most reliable)."""
    params = {"q": query, "setlang": "zh-hans", "mkt": "zh-CN", "count": MAX_RESULTS}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=12.0, follow_redirects=True) as http:
            resp = await http.get(BING_URL, params=params, headers=BING_HEADERS)
    except httpx.RequestError as exc:
        logger.warning("Bing unreachable for %r: %s", query, exc)
        raise HTTPException(status_code=502, detail="Search provider unreachable.")
    if resp.status_code != 200:
        logger.warning("Bing returned HTTP %d for %r", resp.status_code, query)
        raise HTTPException(status_code=502, detail=f"Search provider error ({resp.status_code}).")

    text = resp.text
    starts = [m.start() for m in BING_ALGO_RE.finditer(text)]
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[start:end]
        link = re.search(r"<h2[^>]*>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, re.S)
        if not link:
            link = re.search(r"<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, re.S)
        if not link:
            continue
        url = link.group(1)
        title = _strip_tags(link.group(2))
        if not url.startswith("http") or not title or url in seen:
            continue
        seen.add(url)
        snippet = ""
        snip = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        if snip:
            snippet = _strip_tags(snip.group(1))
        results.append({"title": title, "snippet": snippet, "url": url})
        if len(results) >= MAX_RESULTS:
            break

    # Best-effort instant answer (b_ans card). Bounded by the next result block
    # so nested/related content doesn't leak in; None when there's no card.
    answer = None
    ans_match = re.search(r'<div class="b_ans[^"]*"', text, re.I)
    if ans_match:
        chunk = text[ans_match.end():]
        cut = len(chunk)
        m = re.search(r"</ol>", chunk)
        if m:
            cut = min(cut, m.start())
        m = BING_ALGO_RE.search(chunk)
        if m:
            cut = min(cut, m.start())
        raw = _strip_tags(chunk[:cut])
        if len(raw) > 300:
            raw = raw[:300] + "…"
        answer = raw or None

    return {"query": query, "answer": answer, "results": results}


async def _serper_search(query: str, key: str) -> dict:
    """Google search via Serper.dev. The key stays on the server unless the
    user brought their own (then theirs is used for this request only)."""
    headers = {"X-API-KEY": key, "Content-Type": "application/json"}
    payload = {"q": query, "num": MAX_RESULTS}
    try:
        async with httpx.AsyncClient(timeout=12.0) as http:
            resp = await http.post(SERPER_URL, headers=headers, json=payload)
    except httpx.RequestError as exc:
        logger.warning("Serper unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Search provider unreachable.")

    if resp.status_code != 200:
        # Serper's error body carries the real reason (e.g. "Not enough
        # credits") and contains no key, so it's safe to log and relay.
        body = resp.text[:300]
        logger.warning("Serper error %s: %s", resp.status_code, body)
        msg = None
        try:
            msg = resp.json().get("message")
        except Exception:
            pass
        detail = f"Search provider error ({resp.status_code})"
        if msg:
            detail += f": {msg}"
        raise HTTPException(status_code=502, detail=detail)

    data = resp.json()
    results = []
    for item in (data.get("organic") or [])[:MAX_RESULTS]:
        results.append(
            {
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "url": item.get("link", ""),
            }
        )

    # A direct answer when Google has one — saves the model a hop.
    box = data.get("answerBox") or {}
    answer = box.get("answer") or box.get("snippet") or None
    if not answer:
        kg = data.get("knowledgeGraph") or {}
        answer = kg.get("description") or None

    return {"query": query, "answer": answer, "results": results}


async def _searxng_search(query: str) -> dict:
    """Keyless web search via the self-hosted SearXNG JSON API.

    The local instance aggregates baidu/quark/sogou/360/bing/duckduckgo/... (all
    outbound via the proxy), so Chinese queries get proper results without a
    third-party key. Returns the same ``{query, answer, results}`` shape as the
    other backends; raises so the caller can fall back."""
    params = {"q": query, "format": "json", "language": "zh-CN", "safesearch": 0}
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.get(f"{SEARXNG_URL}/search", params=params)
    except httpx.RequestError as exc:
        logger.warning("SearXNG unreachable for %r: %s", query, exc)
        raise HTTPException(status_code=502, detail="Search provider unreachable.")

    if resp.status_code != 200:
        logger.warning("SearXNG returned HTTP %d for %r", resp.status_code, query)
        raise HTTPException(status_code=502, detail=f"Search provider error ({resp.status_code}).")

    data = resp.json()
    results = []
    for item in (data.get("results") or [])[:MAX_RESULTS]:
        if not item.get("url"):
            continue
        results.append(
            {
                "title": item.get("title", ""),
                "snippet": item.get("content", ""),
                "url": item.get("url", ""),
            }
        )

    # A direct answer / infobox when SearXNG has one — saves the model a hop.
    answer = None
    answers = [a for a in (data.get("answers") or []) if a]
    if answers:
        answer = "; ".join(answers)[:300]
    else:
        infobox = (data.get("infoboxes") or [None])[0]
        if infobox:
            text = infobox.get("content") or infobox.get("description") or ""
            if not text and infobox.get("attributes"):
                text = " ".join(str(a.get("value", "")) for a in infobox.get("attributes", []))
            answer = text[:300] or infobox.get("title")

    return {"query": query, "answer": answer, "results": results}


HERE = os.path.dirname(os.path.abspath(__file__))
LB_USER_AGENT = "speech-to-speech-demo"

app = FastAPI(title="s2s-demo")


@app.middleware("http")
async def _revalidate_static(request: Request, call_next):
    """Force browsers to revalidate the app shell (index.html / JS / CSS).

    StaticFiles sends only ETag/Last-Modified, so a cached main.js survives a
    normal reload and the UI can run stale code after an update. ``no-cache``
    keeps conditional requests (304 when unchanged) while guaranteeing the
    browser always checks.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/vendor/openai-realtime-agents.umd.js", include_in_schema=False)
def agents_sdk_bundle():
    """Serve the exact npm-pinned browser bundle without committing build output."""
    bundled = os.path.join(HERE, "vendor", "openai-realtime-agents.umd.js")
    installed = os.path.join(
        HERE,
        "node_modules",
        "@openai",
        "agents-realtime",
        "dist",
        "bundle",
        "openai-realtime-agents.umd.js",
    )
    path = bundled if os.path.isfile(bundled) else installed
    if not os.path.isfile(path):
        raise HTTPException(status_code=503, detail="Run npm ci in demo/")
    return FileResponse(path, media_type="text/javascript")

# Wire HF OAuth before the app serves (no-op unless the OAuth env is present).
# Sign-in only matters when we're metering (prod Space), so gate it on that.
AUTH_ENABLED = LIMITER_ENABLED and auth.attach(app)


@app.on_event("startup")
async def _startup():
    """Stand up the usage DB and a periodic sweeper — metered (prod Space) only."""
    if not LIMITER_ENABLED:
        return
    limiter.init()
    asyncio.create_task(_sweeper())


async def _sweeper():
    while True:
        await asyncio.sleep(limiter.REAP_AFTER_SEC)
        try:
            await asyncio.to_thread(limiter.sweep)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("usage sweep failed: %r", exc)


class SearchRequest(BaseModel):
    query: str
    # Optional user-supplied key (fallback when the deploy has no server key).
    # Used for this request only; never stored.
    key: str | None = None


@app.get("/api/config")
def config():
    """Client bootstrap: whether web search is available, whether the deploy runs
    behind a load balancer (so the browser uses the /api/session proxy + limiter),
    whether HF sign-in is available, and whether the user may instead set a direct
    s2s server URL. The LB address itself is intentionally NOT included."""
    return {
        # Web search always works: keyless Bing by default. `searchKey` tells
        # the client whether the deploy prefers Serper (key held server-side).
        "search": True,
        "searchKey": bool(SERPER_KEY),
        "lb": bool(LOAD_BALANCER_URL),
        "allowDirect": not LOAD_BALANCER_URL,
        # Deploy-pinned direct s2s URL (empty when unset). Not a secret: the
        # browser dials it itself, and Settings shows it locked.
        "s2sUrl": SPEECH_TO_SPEECH_URL,
        # WebRTC transport availability: the /api/calls proxy only forwards to
        # the env-pinned URL (never a client-supplied one), so the toggle is
        # offered exactly when that URL exists.
        "rtc": bool(SPEECH_TO_SPEECH_URL),
        "iceServers": RTC_ICE_SERVERS,
        "startupGreeting": STARTUP_GREETING,
        "auth": AUTH_ENABLED,
        # External TTS base URL when s2s.sh was started with --tts-url (empty
        # means the built-in Qwen3-TTS). Drives the voice picker in the UI.
        "ttsUrl": TTS_URL,
    }


@app.get("/api/voices")
async def tts_voices():
    """Voice list for the active TTS backend.

    With an external OpenAI-compatible TTS (``--tts-url``), proxy its
    ``/audio/voices`` so the UI lists the real ChatTTS voices and knows the seed
    control applies (``backend="chattts"``). Otherwise report Qwen3-TTS voices.
    A reachable URL that exposes no voices list returns ``backend=null`` so the
    UI keeps the selector but flags the seed box as ChatTTS-only.
    """
    if not TTS_URL:
        return {"backend": "qwen3", "voices": QWEN3_VOICES, "default": None}
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.get(f"{TTS_URL}/audio/voices")
        if resp.status_code == 200:
            data = resp.json()
            voices = [v for v in (data.get("voices") or []) if isinstance(v, str) and v]
            if voices:
                return {"backend": "chattts", "voices": voices, "default": data.get("default")}
    except httpx.RequestError as exc:
        logger.warning("TTS voices unavailable at %s: %r", TTS_URL, exc)
    return {"backend": None, "voices": QWEN3_VOICES, "default": None}


@app.get("/api/me")
async def me(request: Request):
    """Login state, tier, and remaining daily budget. Only meaningful in LB mode;
    sets the anonymous tracking cookie when first seen."""
    if not LIMITER_ENABLED:
        return {"enabled": False}
    tier, keys, set_cookie = auth.resolve_identity(request)
    view = auth.user_view(request, tier=tier)
    unlimited = limiter.budget_for(tier) is None
    rem = None if unlimited else await asyncio.to_thread(limiter.remaining, keys, tier)
    out = {
        "enabled": True,
        "auth": AUTH_ENABLED,
        **view,
        "remainingSec": rem,
        "limitSec": limiter.budget_for(tier),
        "loginUrl": auth.OAUTH_LOGIN_PATH if AUTH_ENABLED else None,
        "logoutUrl": auth.OAUTH_LOGOUT_PATH if AUTH_ENABLED else None,
    }
    resp = JSONResponse(out)
    if set_cookie:
        auth.set_anon_cookie(resp, set_cookie)
    return resp


@app.post("/api/search")
async def search(req: SearchRequest):
    """Proxy a web search. Uses Serper.dev when a key is available (the server's,
    or the user's — theirs for this request only); otherwise searches keyless via
    the local SearXNG instance, falling back to a direct Bing scrape if SearXNG
    is unavailable, so web search works with no configuration at all."""
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Empty query.")

    key = (req.key or "").strip() or SERPER_KEY
    if key:
        return JSONResponse(await _serper_search(query, key))

    try:
        return JSONResponse(await _searxng_search(query))
    except HTTPException:
        # SearXNG down / erroring: keep search alive with the Bing scrape.
        return JSONResponse(await _bing_search(query))


@app.post("/api/calls")
async def calls(request: Request):
    """Proxy the WebRTC SDP handshake to the pinned s2s server.

    The browser can't POST /v1/realtime/calls cross-origin (the s2s server has
    no CORS middleware, and an application/sdp POST is preflighted), so it
    posts the offer here and we forward it server-side. Only the signaling hop
    goes through this proxy — the negotiated audio/data-channel media flows
    directly between the browser and the s2s server.

    Deliberately forwards ONLY to SPEECH_TO_SPEECH_URL: honouring a
    client-supplied target would make this an open proxy (SSRF). No env pin,
    no WebRTC — the client keeps such setups on the WebSocket transport."""
    if not SPEECH_TO_SPEECH_URL:
        raise HTTPException(status_code=404, detail="Not found.")

    offer = await request.body()
    url = _webrtc_calls_url(SPEECH_TO_SPEECH_URL)
    try:
        # Generous timeout: the s2s server waits for its own ICE gathering
        # (up to ~5 s) before returning the answer.
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(url, headers={"Content-Type": "application/sdp"}, content=offer)
    except httpx.RequestError as exc:
        logger.warning("s2s calls endpoint unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Speech service unreachable.")

    # Relay the answer (or the error body) as-is; keep the Location header the
    # s2s server sets on success (the call id, per the OpenAI GA contract).
    headers = {}
    if "location" in resp.headers:
        headers["Location"] = resp.headers["location"]
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/sdp"),
        headers=headers,
    )


@app.post("/api/session")
async def session(request: Request):
    """Proxy the session handshake to the load balancer, keeping its URL secret,
    and meter conversation time by tier.

    The browser POSTs here (same-origin); we resolve the caller's tier, refuse if
    today's budget is already spent (402), otherwise POST <LOAD_BALANCER_URL>/session
    and relay the JSON back. The LB body carries a per-session `connect_url`
    (compute host + short-lived token) the browser must dial directly — that one
    URL is unavoidably exposed, but the stable load-balancer address is not. On a
    successful grant we reserve the first time chunk against the day's budget."""
    if not LOAD_BALANCER_URL:
        # No LB configured — this deploy is direct-mode only; the browser should
        # never call this. 404 so it's indistinguishable from a missing route.
        raise HTTPException(status_code=404, detail="Not found.")

    login_reason = auth.oauth_login_required_reason(request)
    if login_reason:
        return _login_required_response(login_reason)

    tier, keys, set_cookie = auth.resolve_identity(request)
    # Metering runs only on the deployed Space; off-Space the LB still proxies but
    # nothing is tracked. Within metering, unlimited tiers (pro, org) aren't either.
    tracked = LIMITER_ENABLED and limiter.budget_for(tier) is not None

    # Refuse before troubling the LB if the day's budget is already gone. Done
    # here (at enqueue) so we never put a user who can't talk into the queue.
    if tracked:
        rem = await asyncio.to_thread(limiter.remaining, keys, tier)
        if rem is not None and rem <= 0:
            resp = JSONResponse(
                {"tier": tier, "reason": "limit", "remainingSec": 0}, status_code=402
            )
            if set_cookie:
                auth.set_anon_cookie(resp, set_cookie)
            return resp

    url = f"{LOAD_BALANCER_URL.rstrip('/')}/session"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            lb = await http.post(
                url,
                headers=_load_balancer_headers(request),
                content="{}",
            )
    except httpx.RequestError as exc:
        logger.warning("Load balancer unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Speech service unreachable.")

    # The queue is full: the LB replies 503 {state:"at_capacity"}. Relay it as-is
    # so the client shows a soft "try again shortly", not a hard error.
    if lb.status_code == 503:
        body = _safe_json(lb)
        if body.get("state") == "at_capacity":
            resp = JSONResponse({"state": "at_capacity"}, status_code=503)
            if set_cookie:
                auth.set_anon_cookie(resp, set_cookie)
            return resp

    if lb.status_code == 401:
        body = _safe_json(lb)
        reason = body.get("reason", "login_required")
        if reason == "token_invalid":
            session = getattr(request, "scope", {}).get("session")
            if isinstance(session, dict):
                session.pop("oauth_info", None)
        logger.info("Session authentication rejected: %s", reason)
        return _login_required_response(reason, set_cookie)

    if lb.status_code != 200:
        # The LB's error body may name the reason (e.g. capacity); it carries no
        # secret, so relay a trimmed copy.
        logger.warning("Session handshake failed %s: %s", lb.status_code, lb.text[:300])
        raise HTTPException(status_code=502, detail=f"Session handshake failed ({lb.status_code}).")

    data = lb.json()

    # Busy pool: the LB queued us. Relay the ticket untouched — crucially with NO
    # reservation, so waiting in line never costs the day's budget.
    if data.get("state") == "queued":
        data["tier"] = tier
        resp = JSONResponse(data)
        if set_cookie:
            auth.set_anon_cookie(resp, set_cookie)
        return resp

    # A slot was free: reserve the first chunk now and return the grant.
    return await _finalize_grant(data, keys, tier, tracked, set_cookie)


def _login_required_response(reason: str, set_cookie=None) -> JSONResponse:
    """Actionable 401 understood by the browser's login-required flow."""
    resp = JSONResponse(
        {
            "reason": reason,
            "loginUrl": auth.OAUTH_LOGIN_PATH if AUTH_ENABLED else None,
        },
        status_code=401,
    )
    if set_cookie:
        auth.set_anon_cookie(resp, set_cookie)
    return resp


def _load_balancer_headers(request: Request) -> dict[str, str]:
    """Headers for the server-to-server session allocation request.

    The dedicated authorization header matches the Reachy Mini client and lets
    the load balancer validate and attribute an optional HF user token without
    exposing it to browser JavaScript. Anonymous visitors send no credential.
    """
    headers = {
        "Content-Type": "application/json",
        "User-Agent": LB_USER_AGENT,
    }
    token = auth.current_access_token(request)
    if token:
        headers["X-Reachy-Mini-Authorization"] = f"Bearer {token}"
    return headers


@app.get("/api/queue/{queue_id}")
async def queue_status(queue_id: str, request: Request):
    """Poll a waiting ticket: relay the position, or — when the head of the line
    claims a freed slot — reserve the budget now and return the grant. Re-checks the
    daily budget at claim, since a multi-minute wait could have spent it elsewhere."""
    if not LOAD_BALANCER_URL:
        raise HTTPException(status_code=404, detail="Not found.")

    login_reason = auth.oauth_login_required_reason(request)
    if login_reason:
        return _login_required_response(login_reason)

    tier, keys, set_cookie = auth.resolve_identity(request)
    tracked = LIMITER_ENABLED and limiter.budget_for(tier) is not None

    url = f"{LOAD_BALANCER_URL.rstrip('/')}/queue/{queue_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            lb = await http.get(url)
    except httpx.RequestError as exc:
        logger.warning("Load balancer unreachable: %r", exc)
        raise HTTPException(status_code=502, detail="Speech service unreachable.")

    if lb.status_code == 404:
        # Ticket unknown/expired (reaped after we stopped polling). Tell the client
        # to start over rather than spin.
        resp = JSONResponse({"state": "expired"}, status_code=404)
        if set_cookie:
            auth.set_anon_cookie(resp, set_cookie)
        return resp

    if lb.status_code != 200:
        logger.warning("Queue poll failed %s: %s", lb.status_code, lb.text[:300])
        raise HTTPException(status_code=502, detail=f"Queue poll failed ({lb.status_code}).")

    data = lb.json()

    if data.get("state") == "queued":
        data["tier"] = tier
        resp = JSONResponse(data)
        if set_cookie:
            auth.set_anon_cookie(resp, set_cookie)
        return resp

    # Claimed a slot. Re-check the budget: it may have been spent in another tab
    # during the wait. If so, refuse — the just-claimed slot is now a pending
    # session on the LB and its pending-timeout reaper reclaims it shortly.
    if tracked:
        rem = await asyncio.to_thread(limiter.remaining, keys, tier)
        if rem is not None and rem <= 0:
            resp = JSONResponse(
                {"tier": tier, "reason": "limit", "remainingSec": 0}, status_code=402
            )
            if set_cookie:
                auth.set_anon_cookie(resp, set_cookie)
            return resp

    return await _finalize_grant(data, keys, tier, tracked, set_cookie)


@app.delete("/api/queue/{queue_id}")
async def queue_leave(queue_id: str):
    """Leave the queue from the explicit 'Leave queue' button (a real fetch)."""
    if not LOAD_BALANCER_URL:
        raise HTTPException(status_code=404, detail="Not found.")
    await _lb_leave(queue_id)
    return {"ok": True}


@app.post("/api/queue/end")
async def queue_end(request: Request):
    """Leave the queue on teardown/tab-close (navigator.sendBeacon, which can only
    POST). Body: { queueId }. Best-effort; the LB reaps the ticket on TTL anyway."""
    if not LOAD_BALANCER_URL:
        raise HTTPException(status_code=404, detail="Not found.")
    qid = await _queue_id(request)
    if qid:
        await _lb_leave(qid)
    return {"ok": True}


async def _finalize_grant(data, keys, tier, tracked, set_cookie):
    """Shared grant tail (fast path or queue claim): reserve the first chunk, attach
    the metering fields the client needs, and set the anon cookie."""
    remaining = None
    if tracked and data.get("session_id"):
        await asyncio.to_thread(limiter.begin, data["session_id"], keys, tier)
        remaining = await asyncio.to_thread(limiter.remaining, keys, tier)

    data.update({
        "tier": tier,
        "limited": tracked,
        "remainingSec": remaining,
        "heartbeatSec": limiter.HEARTBEAT_SEC,
    })
    resp = JSONResponse(data)
    if set_cookie:
        auth.set_anon_cookie(resp, set_cookie)
    return resp


async def _lb_leave(queue_id: str) -> None:
    """Best-effort: tell the LB to drop a waiting ticket."""
    url = f"{LOAD_BALANCER_URL.rstrip('/')}/queue/{queue_id}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            await http.delete(url)
    except httpx.RequestError as exc:
        logger.warning("Queue leave failed: %r", exc)


def _safe_json(response) -> dict:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def _queue_id(request: Request) -> str:
    """Pull `queueId` from a JSON body, tolerating sendBeacon's blob posts."""
    try:
        data = await request.json()
    except Exception:
        return ""
    return (data or {}).get("queueId", "") if isinstance(data, dict) else ""


async def _session_id(request: Request) -> str:
    """Pull `sessionId` from a JSON body, tolerating sendBeacon's blob posts."""
    try:
        data = await request.json()
    except Exception:
        return ""
    return (data or {}).get("sessionId", "") if isinstance(data, dict) else ""


@app.post("/api/session/heartbeat")
async def session_heartbeat(request: Request):
    """Extend the live reservation one chunk at a time. `expired` once the day's
    budget is spent — the client then tears down."""
    if not LIMITER_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    sid = await _session_id(request)
    alive = bool(sid) and await asyncio.to_thread(limiter.heartbeat, sid)
    return {"expired": not alive}


@app.post("/api/session/end")
async def session_end(request: Request):
    """Clean teardown: reconcile to real elapsed time and refund the unused
    chunk. Sent via navigator.sendBeacon, so it must succeed without a response."""
    if not LIMITER_ENABLED:
        raise HTTPException(status_code=404, detail="Not found.")
    sid = await _session_id(request)
    if sid:
        await asyncio.to_thread(limiter.end, sid)
    return {"ok": True}


# Static front-end. Registered last so the /api routes win. `html=True` serves
# index.html at "/". The repo is public anyway, so serving the dir is fine.
app.mount("/", StaticFiles(directory=HERE, html=True), name="static")
