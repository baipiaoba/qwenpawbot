# -*- coding: utf-8 -*-
"""Telegram inline-keyboard card: AI model console (fully dynamic).

Zero hard-coded providers/models: the picker is built live from the
daemon API (``GET /api/models``) with a TTL cache, falling back to a
local-manifest snapshot when the API is unreachable. Clicking a model
row hot-switches the active model via ``PUT /api/models/active``.

Views:
* home      — active model highlight + recent-used shortcuts +
              provider categories (available/total) + refresh/close
* category  — paginated model list of one provider

Operational guards:
* provider/active caches (30s / 10s TTL) to avoid API stampedes
* per-target switch throttle (3s) against button mashing
* recent-used history persisted to a workspace JSON file
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identity / constants
# ---------------------------------------------------------------------------
NAME = "model_selector"
MESSAGE_TYPE = "model_selector"
CALLBACK_DATA_PREFIX = "ms"

PAGE_SIZE = 6
API_BASE = "http://127.0.0.1:8088/api/models"
ACTIVE_URL = "http://127.0.0.1:8088/api/models/active"

_CACHE_PATH = Path(
    "/run/csi/mount-root/nas/"
    "4079184d856ecc166ed19d4887083405/workspaces/default/"
    ".model_selector_cache.json"
)

_HISTORY_PATH = Path(
    "/run/csi/mount-root/nas/"
    "4079184d856ecc166ed19d4887083405/workspaces/default/"
    ".model_selector_history.json"
)

_SNAPSHOT_DIRS: List[Path] = [
    Path(
        "/run/csi/mount-root/nas/"
        "c290b2d43aaded85d5a7f9d7e5d2359b/providers/custom"
    ),
    Path("/app/working.secret/providers/custom"),
]

_AGENT_JSON_CANDIDATES: List[Path] = [
    Path("agent.json"),
    Path(
        "/run/csi/mount-root/nas/"
        "4079184d856ecc166ed19d4887083405/workspaces/default/agent.json"
    ),
]

# Telegram hard limits.
_MAX_CALLBACK_LEN = 64
_MAX_BUTTON_LEN = 45

# Cache / throttle windows (seconds).
_PROVIDERS_TTL = 30.0
_ACTIVE_TTL = 10.0
_SWITCH_THROTTLE = 3.0
_HISTORY_CAP = 20
_RECENT_LIMIT = 5
# Render cache TTL: faster for home (user sees this first), slower for cat (stable).
_RENDER_HOME_TTL = 2.0
_RENDER_CAT_TTL = 10.0
# Pre-render warmup: render once on startup to populate cache.
_WARMUP_ENABLED = True

# Fallback registry for over-long callback payloads: "#<n>" codes.
_CODE_REGISTRY: Dict[str, Tuple[str, str]] = {}

# Caches.
_prov_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_active_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_switch_last: Dict[Tuple[str, str], float] = {}
# Cached rendered (text, markup) tuples keyed by view id.
_home_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_cat_cache: Dict[str, Any] = {"ts": 0.0, "data": {}}
# Warmup flag: set True after first successful warmup.
_warmed: bool = False


# ---------------------------------------------------------------------------
# Data sources (cached)
# ---------------------------------------------------------------------------

def _parse_provider(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pid = str(obj.get("id") or "")
    if not pid:
        return None
    # Skip built-in / local providers (qwenpaw-local, ollama, lmstudio, ...).
    if obj.get("is_local"):
        return None
    models: List[Dict[str, Any]] = []
    seen: set = set()
    for m in (obj.get("extra_models") or []) + (
        obj.get("discovered_models") or []
    ):
        mid = str(m.get("id") or "")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        models.append(
            {
                "id": mid,
                "status": m.get("availability_status") or "unverified",
                "multimodal": bool(m.get("supports_multimodal")),
            }
        )
    if not models:
        return None
    models.sort(
        key=lambda x: (0 if x["status"] == "available" else 1, x["id"])
    )
    return {"name": str(obj.get("name") or pid), "models": models}


def _has_available_models(providers: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Keep only providers that expose at least one available model."""
    return {
        pid: info
        for pid, info in providers.items()
        if any(m["status"] == "available" for m in info["models"])
    }


def _snapshot_fallback() -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for d in _SNAPSHOT_DIRS:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            parsed = _parse_provider(obj)
            if parsed:
                result.setdefault(str(obj.get("id")), parsed)
    return result


def _load_disk_cache() -> Dict[str, Dict[str, Any]]:
    """Load the persisted provider cache (serves cold starts)."""
    try:
        if _CACHE_PATH.exists():
            data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return data
    except Exception:
        logger.warning("model_selector: disk cache unreadable", exc_info=True)
    return {}


def _save_disk_cache(providers: Dict[str, Dict[str, Any]]) -> None:
    """Persist provider snapshot (best-effort, called after API fetch)."""
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps(providers, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("model_selector: disk cache write failed", exc_info=True)


def fetch_live_providers(force: bool = False) -> Dict[str, Dict[str, Any]]:
    """Providers from the daemon API (30s cache); snapshot on failure.

    On a successful API fetch the result is also persisted to a disk
    cache so the next cold start can serve from disk in <1ms instead
    of waiting on the HTTP round-trip.
    """
    now = time.time()
    if (
        not force
        and _prov_cache["data"]
        and now - _prov_cache["ts"] < _PROVIDERS_TTL
    ):
        return _prov_cache["data"]
    try:
        with urllib.request.urlopen(API_BASE, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result: Dict[str, Dict[str, Any]] = {}
        for obj in data if isinstance(data, list) else []:
            parsed = _parse_provider(obj)
            if parsed:
                result[str(obj.get("id"))] = parsed
        if result:
            _prov_cache["ts"] = now
            _prov_cache["data"] = result
            _save_disk_cache(result)
            return result
        logger.warning("model_selector: live API empty, using snapshot")
    except Exception as exc:
        logger.warning("model_selector: live API failed (%s)", exc)
    # API failed: try the persisted disk cache before snapshots.
    disk = _load_disk_cache()
    if disk:
        _prov_cache["ts"] = now
        _prov_cache["data"] = disk
        return disk
    fallback = _snapshot_fallback()
    if fallback:
        _prov_cache["ts"] = now
        _prov_cache["data"] = fallback
    return fallback or _prov_cache["data"] or {}


def get_active_model(force: bool = False) -> Tuple[str, str]:
    """(provider_id, model_id) of the active model (10s cache)."""
    now = time.time()
    if (
        not force
        and _active_cache["data"]
        and now - _active_cache["ts"] < _ACTIVE_TTL
    ):
        return _active_cache["data"]
    pair: Tuple[str, str] = ("", "")
    try:
        with urllib.request.urlopen(ACTIVE_URL, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        llm = data.get("active_llm") or {}
        pair = (str(llm.get("provider_id") or ""), str(llm.get("model") or ""))
    except Exception:
        for p in _AGENT_JSON_CANDIDATES:
            try:
                if p.exists():
                    am = json.loads(
                        p.read_text(encoding="utf-8")
                    ).get("active_model", {})
                    pair = (
                        str(am.get("provider_id", "")),
                        str(am.get("model", "")),
                    )
                    break
            except Exception:
                continue
    if pair[0] or pair[1]:
        _active_cache["ts"] = now
        _active_cache["data"] = pair
    return pair


# ---------------------------------------------------------------------------
# Recent-used history
# ---------------------------------------------------------------------------

def _load_history() -> List[List[Any]]:
    try:
        if _HISTORY_PATH.exists():
            data = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, list) and len(x) >= 2]
    except Exception:
        logger.warning("model_selector: history unreadable", exc_info=True)
    return []


def record_switch(pid: str, mid: str) -> None:
    entries = [e for e in _load_history() if not (e[0] == pid and e[1] == mid)]
    entries.insert(0, [pid, mid, time.time()])
    try:
        _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _HISTORY_PATH.write_text(
            json.dumps(entries[:_HISTORY_CAP], ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("model_selector: history write failed", exc_info=True)


def recent_models(limit: int = _RECENT_LIMIT) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for e in _load_history():
        pair = (str(e[0]), str(e[1]))
        if pair not in out:
            out.append(pair)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Favorites
# ---------------------------------------------------------------------------

_FAVORITES_PATH = Path(
    "/run/csi/mount-root/nas/"
    "4079184d856ecc166ed19d4887083405/workspaces/default/"
    ".model_selector_favorites.json"
)


def get_favorites() -> List[Tuple[str, str]]:
    """List favorited (provider_id, model_id) pairs."""
    try:
        if _FAVORITES_PATH.exists():
            data = json.loads(_FAVORITES_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [
                    (str(e[0]), str(e[1]))
                    for e in data
                    if isinstance(e, list) and len(e) >= 2
                ]
    except Exception:
        logger.warning("model_selector: favorites unreadable", exc_info=True)
    return []


def toggle_favorite(pid: str, mid: str) -> bool:
    """Toggle a favorite; returns True when the pair is now favorited."""
    favs = get_favorites()
    pair = (pid, mid)
    if pair in favs:
        favs.remove(pair)
        saved = False
    else:
        favs.insert(0, pair)
        favs = favs[:_HISTORY_CAP]
        saved = True
    try:
        _FAVORITES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FAVORITES_PATH.write_text(
            json.dumps([list(p) for p in favs], ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("model_selector: favorites write failed", exc_info=True)
    return saved


# ---------------------------------------------------------------------------
# Callback-data helpers
# ---------------------------------------------------------------------------

def _switch_code(pid: str, mid: str) -> str:
    raw = f"{pid}:{mid}"
    if len(f"ms:s:{raw}") <= _MAX_CALLBACK_LEN:
        return raw
    for code, val in _CODE_REGISTRY.items():
        if val == (pid, mid):
            return f"#{code}"
    code = str(len(_CODE_REGISTRY))
    _CODE_REGISTRY[code] = (pid, mid)
    return f"#{code}"


def _resolve_switch(payload: str) -> Tuple[str, str]:
    if payload.startswith("#"):
        return _CODE_REGISTRY.get(payload[1:], ("", ""))
    pid, _, mid = payload.partition(":")
    return pid, mid


def _btn(text: str, data: str) -> InlineKeyboardButton:
    if len(text) > _MAX_BUTTON_LEN:
        text = text[: _MAX_BUTTON_LEN - 1] + "…"
    return InlineKeyboardButton(text=text, callback_data=data)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def build_home_view(force: bool = False) -> Tuple[str, InlineKeyboardMarkup]:
    """Home view: active highlight + recent-used + provider categories (lazy stats)."""
    now = time.time()
    ttl = _RENDER_HOME_TTL
    if (
        not force
        and _home_cache["data"]
        and now - _home_cache["ts"] < ttl
    ):
        return _home_cache["data"]
    providers = fetch_live_providers(force=force)
    curr_p, curr_m = get_active_model(force=force)

    lines = ["🎛 **AI 模型控制台**", ""]
    if curr_p and curr_m:
        cur_name = providers.get(curr_p, {}).get("name", curr_p)
        lines += ["🟢 **当前激活**", f"`{cur_name} / {curr_m}`", ""]
    else:
        lines += ["👇 请选择模型：", ""]

    recents = [r for r in recent_models() if r != (curr_p, curr_m)]
    favorites = [r for r in get_favorites() if r != (curr_p, curr_m)]
    rows: List[List[InlineKeyboardButton]] = []
    if favorites:
        lines += ["❤️ **收藏夹**（点击切换）", ""]
        pair: List[InlineKeyboardButton] = []
        for pid, mid in favorites:
            pair.append(_btn(f"⭐ {mid}", f"ms:s:{_switch_code(pid, mid)}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
    if recents:
        lines += ["🕐 **最近使用**（点击切回）", ""]
        pair = []
        for pid, mid in recents:
            pair.append(_btn(mid, f"ms:s:{_switch_code(pid, mid)}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)

    lines += ["🏢 **服务商分类**（点击展开）", ""]
    pair = []
    for pid, info in _has_available_models(providers).items():
        # Lazy: show provider name only, stats resolved on category open.
        pair.append(
            _btn(
                info["name"],
                f"ms:c:{pid}:0",
            )
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    rows.append([_btn("🔄 刷新数据", "ms:refresh"), _btn("✖ 关闭", "ms:close")])
    out = ("\n".join(lines), InlineKeyboardMarkup(rows))
    _home_cache["ts"] = now
    _home_cache["data"] = out
    return out


def build_category_view(
    pid: str, page: int = 0, force: bool = False
) -> Tuple[str, InlineKeyboardMarkup]:
    """Paginated model list of one provider (longer TTL, stable content)."""
    now = time.time()
    ttl = _RENDER_CAT_TTL
    key = f"{pid}:{page}"
    if (
        not force
        and key in _cat_cache["data"]
        and now - _cat_cache["ts"] < ttl
    ):
        return _cat_cache["data"][key]

    providers = fetch_live_providers(force=force)
    info = providers.get(pid)
    if not info:
        return (
            "⚠️ 未找到该服务商",
            InlineKeyboardMarkup([[_btn("🏠 返回控制台", "ms:home")]]),
        )

    models = info["models"]
    total = len(models)
    avail = sum(1 for m in models if m["status"] == "available")
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    curr_p, curr_m = get_active_model()

    text = (
        f"🏢 **{info['name']}**\n"
        f"📊 共 {total} 个模型 · 可用 {avail} · 第 {page + 1}/{pages} 页"
    )

    rows: List[List[InlineKeyboardButton]] = []
    favs = set(get_favorites())
    for m in models[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        tags = ""
        if m["status"] == "available":
            tags += " ✓"
        if m.get("multimodal"):
            tags += " 👁"
        prefix = "🔘 " if (pid == curr_p and m["id"] == curr_m) else ""
        star = "⭐" if (pid, m["id"]) in favs else "☆"
        rows.append(
            [
                _btn(
                    f"{prefix}{m['id']}{tags}",
                    f"ms:s:{_switch_code(pid, m['id'])}",
                ),
                _btn(star, f"ms:f:{_switch_code(pid, m['id'])}"),
            ]
        )

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(_btn("◀ 上一页", f"ms:c:{pid}:{page - 1}"))
    else:
        nav.append(_btn("· 首页 ·", "ms:noop"))
    nav.append(_btn(f"📄 {page + 1}/{pages}", "ms:noop"))
    if page < pages - 1:
        nav.append(_btn("下一页 ▶", f"ms:c:{pid}:{page + 1}"))
    else:
        nav.append(_btn("· 末页 ·", "ms:noop"))
    rows.append(nav)
    rows.append(
        [_btn("🏠 控制台", "ms:home"), _btn("🔄 刷新", "ms:refresh")]
    )
    rows.append([_btn("✖ 关闭", "ms:close")])
    out = (text, InlineKeyboardMarkup(rows))
    _cat_cache["data"][key] = out
    _cat_cache["ts"] = now
    return out


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

async def send_console(
    channel: Any, chat_id: str, meta: Optional[Dict[str, Any]] = None
) -> bool:
    """Send the model console (home view) into a chat."""
    meta = meta or {}
    if not chat_id:
        logger.warning("model_selector: no chat_id")
        return False
    bot = getattr(getattr(channel, "_application", None), "bot", None)
    if not bot:
        return False
    text, keyboard = build_home_view()
    kwargs: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": keyboard,
    }
    thread = meta.get("message_thread_id")
    if thread is not None:
        kwargs["message_thread_id"] = thread
    await bot.send_message(**kwargs)
    logger.info("model console sent to %s", chat_id)
    return True


async def render(
    channel: Any,
    to_handle: str,
    event: Any,
    send_meta: Dict[str, Any],
    meta: Dict[str, Any],
    **_kwargs: Any,
) -> bool:
    """CardKind.render entry-point (event-driven path)."""
    chat_id = str(send_meta.get("chat_id") or to_handle)
    try:
        return await send_console(channel, chat_id, send_meta)
    except Exception:
        logger.exception("model_selector render failed")
        return False


# ---------------------------------------------------------------------------
# Warmup: pre-render home + first page of each provider for instant first load
# ---------------------------------------------------------------------------

async def warmup() -> None:
    """Pre-render views on startup so the first /model is instant.

    Fetches the provider list and the active model concurrently, then
    pre-renders the home view and the first page of the top providers.
    """
    global _warmed
    if _warmed:
        return
    try:
        loop = asyncio.get_running_loop()
        prov_fut = loop.run_in_executor(
            None, lambda: fetch_live_providers(force=True)
        )
        active_fut = loop.run_in_executor(
            None, lambda: get_active_model(force=True)
        )
        providers, _ = await asyncio.gather(prov_fut, active_fut)
        build_home_view(force=True)
        for pid in list(providers.keys())[:5]:
            build_category_view(pid, 0, force=True)
        _warmed = True
        logger.info(
            "model_selector: warmup complete (%d providers)", len(providers)
        )
    except Exception:
        logger.warning("model_selector: warmup failed", exc_info=True)


# ---------------------------------------------------------------------------
# Callback handling
# ---------------------------------------------------------------------------

async def _switch_model(provider_id: str, model_id: str) -> Dict[str, Any]:
    body = json.dumps(
        {
            "provider_id": provider_id,
            "model": model_id,
            "scope": "agent",
            "agent_id": "default",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        ACTIVE_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None, lambda: urllib.request.urlopen(req, timeout=15)
        )
        return {"ok": True, "detail": ""}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}


async def handle(channel: Any, query: Any) -> None:
    """Route model-console callback queries.

    Design: ``query.answer`` is fired immediately so the user gets an
    instant acknowledgement; the (potentially slower) ``edit_message``
    happens right after on the same coroutine, so the whole turn is
    still atomic.  Heavy work (API fetch) is cached, so repeated
    clicks are cheap.
    """
    data = str(getattr(query, "data", "") or "")
    if not data.startswith("ms:"):
        return

    async def _edit(text: str, kb: InlineKeyboardMarkup) -> None:
        try:
            await query.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
        except Exception:
            logger.warning("model_selector: edit failed", exc_info=True)

    async def _answer(msg: str = "", alert: bool = False) -> None:
        try:
            await query.answer(msg or None, show_alert=alert)
        except Exception:
            pass

    if data == "ms:noop" or data.startswith("ms:hdr:"):
        await _answer()
        return

    if data == "ms:close":
        await _answer()
        try:
            await query.edit_message_text(
                text="🎛 模型控制台已关闭。", reply_markup=None
            )
        except Exception:
            pass
        return

    if data == "ms:home":
        await _answer()
        text, kb = build_home_view()
        await _edit(text, kb)
        return

    if data == "ms:refresh":
        await _answer("正在刷新…")
        text, kb = build_home_view(force=True)
        await _edit(text, kb)
        return

    if data.startswith("ms:c:"):
        parts = data.split(":", 3)
        if len(parts) >= 4:
            pid = parts[2]
            page = int(parts[3]) if parts[3].isdigit() else 0
            await _answer()
            text, kb = build_category_view(pid, page)
            await _edit(text, kb)
        return

    if data.startswith("ms:f:"):
        parts = data.split(":", 2)
        if len(parts) >= 3:
            pid, mid = _resolve_switch(parts[2])
            if not pid or not mid:
                await _answer("菜单已过期，请重新打开 /model", alert=True)
                return
            added = toggle_favorite(pid, mid)
            await _answer("⭐ 已收藏" if added else "已取消收藏")
            text, kb = build_category_view(pid, 0, force=True)
            await _edit(text, kb)
        return

    if data.startswith("ms:s:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            return
        pid, mid = _resolve_switch(parts[2])
        if not pid or not mid:
            await _answer("菜单已过期，请重新打开 /model", alert=True)
            return

        now = time.time()
        if now - _switch_last.get((pid, mid), 0.0) < _SWITCH_THROTTLE:
            await _answer("操作太频繁，请稍后再试")
            return
        _switch_last[(pid, mid)] = now

        # Instant ack — the user sees feedback immediately.
        await _answer(f"正在切换至: {mid} …")
        res = await _switch_model(pid, mid)
        # Fetch provider name off the cached providers (cheap, non-blocking).
        name = fetch_live_providers().get(pid, {}).get("name", pid)
        if res.get("ok"):
            record_switch(pid, mid)
            _active_cache["ts"] = time.time()
            _active_cache["data"] = (pid, mid)
            # Only invalidate home cache (category views are stable).
            _home_cache["ts"] = 0.0
            msg = (
                "✅ **模型热切换成功！**\n\n"
                f"🏢 服务商：`{name}` (`{pid}`)\n"
                f"🤖 当前激活：`{mid}`\n\n"
                "💬 后续对话已无缝转由新模型驱动。\n"
                "⭐ 已记入最近使用。"
            )
            kb = InlineKeyboardMarkup(
                [
                    [_btn("🎛 返回控制台", "ms:home")],
                    [_btn("✖ 关闭", "ms:close")],
                ]
            )
        else:
            msg = (
                "❌ **模型切换失败**\n\n"
                f"🏢 服务商：`{pid}`\n"
                f"🤖 目标模型：`{mid}`\n"
                f"⚠️ 错误详情：`{res.get('detail', '未知错误')}`\n\n"
                "请选择其他可用模型重试。"
            )
            kb = InlineKeyboardMarkup(
                [
                    [_btn("🔄 返回重选", f"ms:c:{pid}:0")],
                    [_btn("🏠 返回控制台", "ms:home")],
                ]
            )
        await _edit(msg, kb)
        return


__all__ = [
    "NAME",
    "MESSAGE_TYPE",
    "CALLBACK_DATA_PREFIX",
    "fetch_live_providers",
    "get_active_model",
    "record_switch",
    "recent_models",
    "get_favorites",
    "toggle_favorite",
    "build_home_view",
    "build_category_view",
    "send_console",
    "render",
    "warmup",
    "handle",
]