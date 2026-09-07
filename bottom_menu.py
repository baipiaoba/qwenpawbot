# -*- coding: utf-8 -*-
"""Telegram inline-keyboard card: bottom-fixed toolbar (always visible).

Design:
- Collapsed: shows a single "三 菜单" button at the bottom
- Expanded: shows two rows of 4 action buttons + a close button
- Toggling is done via callback_data: `bm:toggle`
- Each action button uses `bm:{action}` format

Actions:
- bm:personal  — show current status (model, provider, chat count)
- bm:history   — show recent conversation list
- bm:quick     — quick command buttons (/new /clear /compact)
- bm:favorite  — show favorites list
- bm:cron      — manage scheduled tasks
- bm:memory    — memory management (compact/clear/export)
- bm:settings  — channel settings
- bm:help      — help / FAQ
"""
from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
NAME = "bottom_menu"
MESSAGE_TYPE = "bottom_menu"
CALLBACK_DATA_PREFIX = "bm"

# Telegram hard limits.
_MAX_BUTTON_LEN = 12

# Paths for persistent data.
_WORKSPACE = Path("/run/csi/mount-root/nas/4079184d856ecc166ed19d4887083405/workspaces/default")
_STATE_PATH = _WORKSPACE / ".bottom_menu_state.json"
_HISTORY_PATH = _WORKSPACE / ".model_selector_history.json"
_FAVORITES_PATH = _WORKSPACE / ".model_selector_favorites.json"
_AGENT_JSON = _WORKSPACE / "agent.json"

# Toggle state key (same structure as model_selector's pattern).
_STATE_KEY = "expanded"


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _load_state() -> Dict[str, Any]:
    try:
        if _STATE_PATH.exists():
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("bottom_menu: state unreadable", exc_info=True)
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("bottom_menu: state write failed", exc_info=True)


def is_expanded() -> bool:
    return _load_state().get(_STATE_KEY, False)


def set_expanded(expanded: bool) -> None:
    state = _load_state()
    state[_STATE_KEY] = expanded
    _save_state(state)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _btn(text: str, callback_data: str) -> InlineKeyboardButton:
    if len(text) > _MAX_BUTTON_LEN:
        text = text[: _MAX_BUTTON_LEN - 1] + "…"
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def _get_active_model() -> Tuple[str, str]:
    """Get (provider_id, model_id) from daemon API or agent.json."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8088/api/models/active", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        llm = data.get("active_llm") or {}
        return str(llm.get("provider_id") or ""), str(llm.get("model") or "")
    except Exception:
        try:
            cfg = json.loads(_AGENT_JSON.read_text(encoding="utf-8"))
            am = cfg.get("active_model", {})
            return str(am.get("provider_id", "")), str(am.get("model", ""))
        except Exception:
            return "", ""


def _get_provider_name(pid: str) -> str:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8088/api/models", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        for p in data:
            if p.get("id") == pid:
                return p.get("name", pid)
    except Exception:
        pass
    return pid


# ---------------------------------------------------------------------------
# View builders
# ---------------------------------------------------------------------------

def build_collapsed_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Collapsed state: just the menu trigger button."""
    return (
        "🎛 **AI 助手**\n\n点击下方按钮展开功能菜单：",
        InlineKeyboardMarkup([
            [_btn("📋 菜单", "bm:toggle")]
        ])
    )


def build_expanded_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Expanded state: two rows of 4 buttons + close."""
    return (
        "🎛 **AI 助手** — 选择功能：\n\n"
        "📊 **核心功能**\n"
        "个人 · 查询 · 快捷 · 收藏\n\n"
        "⚙️ **工具功能**\n"
        "任务 · 存储 · 设置 · 帮助",
        InlineKeyboardMarkup([
            [
                _btn("👤 个人", "bm:personal"),
                _btn("🔍 查询", "bm:history"),
                _btn("⚡ 快捷", "bm:quick"),
                _btn("⭐ 收藏", "bm:favorite"),
            ],
            [
                _btn("🎯 任务", "bm:cron"),
                _btn("💾 存储", "bm:memory"),
                _btn("🔧 设置", "bm:settings"),
                _btn("❓ 帮助", "bm:help"),
            ],
            [_btn("✖ 收起", "bm:toggle")]
        ])
    )


def build_personal_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Current status: model, provider, etc."""
    pid, mid = _get_active_model()
    name = _get_provider_name(pid) if pid else "未知"
    return (
        f"👤 **当前状态**\n\n"
        f"🤖 模型：`{mid or '未设置'}`\n"
        f"🏢 服务商：`{name}`\n"
        f"💬 对话：待统计\n\n"
        "回到主菜单：",
        InlineKeyboardMarkup([
            [_btn("🔙 返回", "bm:toggle")]
        ])
    )


def build_history_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Recent conversation history."""
    recents: List[Tuple[str, str]] = []
    try:
        if _HISTORY_PATH.exists():
            data = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
            recents = [
                (str(e[0]), str(e[1]))
                for e in data
                if isinstance(e, list) and len(e) >= 2
            ]
    except Exception:
        pass

    if not recents:
        text = "🔍 **最近会话**\n\n暂无历史记录"
    else:
        lines = ["🔍 **最近会话**", ""]
        for pid, mid in recents[:5]:
            lines.append(f"• `{mid}`")
        text = "\n".join(lines)

    return text, InlineKeyboardMarkup([
        [_btn("🔙 返回", "bm:toggle")]
    ])


def build_quick_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Quick commands."""
    return (
        "⚡ **快捷指令**\n\n"
        "一键执行常用命令：",
        InlineKeyboardMarkup([
            [_btn("/new 新会话", "cmd:new"), _btn("/clear 清空", "cmd:clear")],
            [_btn("/compact 压缩", "cmd:compact"), _btn("/history 历史", "cmd:history")],
            [_btn("🔙 返回", "bm:toggle")],
        ])
    )


def build_favorite_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Favorites list."""
    favs: List[Tuple[str, str]] = []
    try:
        if _FAVORITES_PATH.exists():
            data = json.loads(_FAVORITES_PATH.read_text(encoding="utf-8"))
            favs = [
                (str(e[0]), str(e[1]))
                for e in data
                if isinstance(e, list) and len(e) >= 2
            ]
    except Exception:
        pass

    if not favs:
        text = "⭐ **收藏夹**\n\n暂无收藏，去模型列表点 ⭐ 添加"
    else:
        lines = ["⭐ **收藏夹**", ""]
        for pid, mid in favs[:6]:
            lines.append(f"• {mid}")
        text = "\n".join(lines)

    return text, InlineKeyboardMarkup([
        [_btn("🔙 返回", "bm:toggle")]
    ])


def build_cron_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Cron task management."""
    return (
        "🎯 **定时任务**\n\n"
        "查看和管理 Cron 任务：\n\n"
        "• 任务列表\n• 创建新任务\n• 编辑/删除任务",
        InlineKeyboardMarkup([
            [_btn("📋 查看任务", "cron:list"), _btn("➕ 新建", "cron:create")],
            [_btn("🔙 返回", "bm:toggle")],
        ])
    )


def build_memory_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Memory management."""
    return (
        "💾 **记忆管理**\n\n"
        "管理 Agent 的记忆数据：\n\n"
        "• 压缩上下文\n• 清空记忆\n• 导出备份",
        InlineKeyboardMarkup([
            [_btn("📦 压缩", "cmd:compact"), _btn("🗑 清空", "cmd:clear_memory")],
            [_btn("💿 导出", "memory:export"), _btn("🔙 返回", "bm:toggle")],
        ])
    )


def build_settings_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Channel settings."""
    return (
        "🔧 **频道设置**\n\n"
        "调整 Bot 配置：\n\n"
        "• 语言切换\n• 主题颜色\n• 通知开关\n• 隐私设置",
        InlineKeyboardMarkup([
            [_btn("🌍 语言", "settings:language"), _btn("🎨 主题", "settings:theme")],
            [_btn("🔔 通知", "settings:notify"), _btn("🔙 返回", "bm:toggle")],
        ])
    )


def build_help_view() -> Tuple[str, InlineKeyboardMarkup]:
    """Help / FAQ."""
    return (
        "❓ **帮助**\n\n"
        "**常用命令：**\n"
        "/start — 开始新对话\n"
        "/model — 打开模型控制台\n"
        "/clear — 清空当前会话\n"
        "/compact — 压缩上下文\n"
        "/history — 查看历史\n"
        "/stop — 停止任务\n\n"
        "**底部菜单：**\n"
        "点击 📋 菜单展开/收起工具栏\n"
        "点击各按钮执行对应功能",
        InlineKeyboardMarkup([
            [_btn("🔙 返回", "bm:toggle")]
        ])
    )


# ---------------------------------------------------------------------------
# Main render function
# ---------------------------------------------------------------------------

async def render(
    channel: Any,
    to_handle: str,
    event: Any,
    send_meta: Dict[str, Any],
    meta: Dict[str, Any],
    **_kwargs: Any,
) -> bool:
    """Render the bottom toolbar."""
    chat_id = str(send_meta.get("chat_id") or to_handle)
    bot = getattr(getattr(channel, "_application", None), "bot", None)
    if not bot or not chat_id:
        return False

    try:
        expanded = is_expanded()
        if expanded:
            text, kb = build_expanded_view()
        else:
            text, kb = build_collapsed_view()

        kwargs: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": kb,
        }
        thread = meta.get("message_thread_id")
        if thread is not None:
            kwargs["message_thread_id"] = thread

        await bot.send_message(**kwargs)
        logger.info("bottom menu rendered: expanded=%s", expanded)
        return True
    except Exception:
        logger.exception("bottom_menu render failed")
        return False


# ---------------------------------------------------------------------------
# Callback handler
# ---------------------------------------------------------------------------

async def handle(channel: Any, query: Any) -> None:
    """Route bottom menu callback queries."""
    data = str(getattr(query, "data", "") or "")
    if not data.startswith("bm:"):
        return

    parts = data.split(":", 2)
    if len(parts) < 2:
        return

    action = parts[1]
    params = parts[2] if len(parts) > 2 else ""

    async def _edit(text: str, kb: InlineKeyboardMarkup) -> None:
        try:
            await query.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
        except Exception:
            logger.warning("bottom_menu: edit failed", exc_info=True)

    async def _answer(msg: str = "") -> None:
        try:
            await query.answer(msg or None)
        except Exception:
            pass

    # Toggle expand/collapse
    if action == "toggle":
        expanded = is_expanded()
        set_expanded(not expanded)
        if not expanded:
            text, kb = build_expanded_view()
            await _answer("展开菜单")
        else:
            text, kb = build_collapsed_view()
            await _answer("收起菜单")
        await _edit(text, kb)
        return

    # Personal status
    if action == "personal":
        text, kb = build_personal_view()
        await _answer()
        await _edit(text, kb)
        return

    # History
    if action == "history":
        text, kb = build_history_view()
        await _answer()
        await _edit(text, kb)
        return

    # Quick commands
    if action == "quick":
        text, kb = build_quick_view()
        await _answer()
        await _edit(text, kb)
        return

    # Favorites
    if action == "favorite":
        text, kb = build_favorite_view()
        await _answer()
        await _edit(text, kb)
        return

    # Cron tasks
    if action == "cron":
        text, kb = build_cron_view()
        await _answer()
        await _edit(text, kb)
        return

    # Memory management
    if action == "memory":
        text, kb = build_memory_view()
        await _answer()
        await _edit(text, kb)
        return

    # Settings
    if action == "settings":
        text, kb = build_settings_view()
        await _answer()
        await _edit(text, kb)
        return

    # Help
    if action == "help":
        text, kb = build_help_view()
        await _answer()
        await _edit(text, kb)
        return

    logger.warning("bottom_menu: unknown action %s", action)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "NAME",
    "MESSAGE_TYPE",
    "CALLBACK_DATA_PREFIX",
    "render",
    "handle",
    "is_expanded",
    "set_expanded",
]