#!/bin/bash
# ============================================================================
# 在新容器中一键恢复 QwenPaw Telegram 中文菜单 + 模型控制台
# 用法: 复制整段粘贴到新容器终端执行
# 前提: 新容器已安装 QwenPaw，且 qwenpaw daemon 正在运行
# ============================================================================
set -e

echo "=============================================="
echo " QwenPaw Telegram 中文菜单 + 模型控制台 安装"
echo "=============================================="
echo ""

# 1. 定位 QwenPaw 安装目录
echo ">>> [1/5] 定位 QwenPaw 安装目录..."
if [ -d "/app/venv/lib/python3.11/site-packages/qwenpaw" ]; then
    QP_DIR="/app/venv/lib/python3.11/site-packages/qwenpaw"
else
    QP_DIR=$(python3 -c "import qwenpaw, os; print(os.path.dirname(qwenpaw.__file__))")
fi
echo "    QP_DIR = $QP_DIR"

CARDS_DIR="$QP_DIR/app/channels/telegram/cards"
CHANNEL_FILE="$QP_DIR/app/channels/telegram/channel.py"
DISPATCHER_FILE="$CARDS_DIR/dispatcher.py"
MODEL_SELECTOR_FILE="$CARDS_DIR/model_selector.py"

# 2. 备份原始文件
echo ">>> [2/5] 备份原始文件..."
TS=$(date +%Y%m%d_%H%M%S)
if [ ! -f "$CHANNEL_FILE.bak" ]; then
    cp "$CHANNEL_FILE" "$CHANNEL_FILE.bak_$TS"
    echo "    channel.py 备份完成"
fi
if [ ! -f "$DISPATCHER_FILE.bak" ]; then
    cp "$DISPATCHER_FILE" "$DISPATCHER_FILE.bak_$TS"
    echo "    dispatcher.py 备份完成"
fi

# 3. 永久汉化 channel.py（重启不复原）
echo ">>> [3/5] 永久汉化 channel.py（重启不复原）..."
python3 - << 'PYEOF'
fpath = "$CHANNEL_FILE"
with open(fpath, "r", encoding="utf-8") as f:
    c = f.read()

replaces = [
    ('"Start a new conversation"', '"开始新对话"'),
    ('"Start a new conversation (clear memory)"', '"开启全新会话（重置记忆）"'),
    ('"Compact conversation memory"', '"压缩上下文记忆"'),
    ('"Clear conversation history"', '"清空当前会话记录"'),
    ('"Show conversation history"', '"查看历史对话"'),
    ('"Show or switch AI model"', '"切换/查看AI模型"'),
    ('"Stop the current task"', '"停止当前运行的任务"'),
]
for old, new in replaces:
    c = c.replace(old, new)

with open(fpath, "w", encoding="utf-8") as f:
    f.write(c)
print("    channel.py 汉化完成")
PYEOF

# 4. 确保 model_selector.py 存在
echo ">>> [4/5] 确保模型控制台文件存在..."
if [ ! -f "$MODEL_SELECTOR_FILE" ]; then
    echo "    ⚠️ model_selector.py 不存在，需要手动写入"
    echo "    请从原容器复制或使用下方内联脚本生成"
else
    echo "    model_selector.py 已存在"
fi

# 5. 同步 /app/src + 刷新 Telegram 命令 + 重启频道
echo ">>> [5/5] 同步 /app/src + 刷新 Telegram 命令 + 重启频道..."
if [ -d "/app/src/qwenpaw" ]; then
    cp "$CHANNEL_FILE" "/app/src/qwenpaw/app/channels/telegram/channel.py" 2>/dev/null || true
    cp -r "$CARDS_DIR/." "/app/src/qwenpaw/app/channels/telegram/cards/" 2>/dev/null || true
    echo "    /app/src 同步完成"
fi

# 刷新 Telegram 官方命令
python3 - << 'PYEOF'
import json, urllib.request
cfg = json.load(open("agent.json"))
token = cfg.get("channels", {}).get("telegram", {}).get("bot_token")
if token:
    cmds = [
        {"command": "start", "description": "开始新对话"},
        {"command": "new", "description": "开启全新会话（重置记忆）"},
        {"command": "compact", "description": "压缩上下文记忆"},
        {"command": "clear", "description": "清空当前会话记录"},
        {"command": "history", "description": "查看历史对话"},
        {"command": "model", "description": "切换/查看AI模型"},
        {"command": "stop", "description": "停止当前运行的任务"},
    ]
    for p in [{"commands": cmds}, {"commands": cmds, "language_code": "zh"}]:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/setMyCommands",
            data=json.dumps(p).encode(),
            headers={"Content-Type": "application/json"}), timeout=10)
    print("    Telegram 命令刷新完成")
PYEOF

# 热重启频道
curl -s -X POST http://127.0.0.1:8088/api/config/channels/telegram/restart \
    -H "Content-Type: application/json" -d "{}" >/dev/null 2>&1 || true
echo "    频道已重启"

echo ""
echo "=============================================="
echo " ✅ 安装完成！"
echo "=============================================="
echo ""
echo " 功能："
echo "  • 左下角菜单永久中文（7 条命令）"
echo "  • /model 命令 → 弹出动态模型控制台"
echo "  • 控制台支持：当前激活高亮、❤️ 收藏夹、"
echo "    🕐 最近使用、🏢 服务商分类（过滤内置/未配置）、"
echo "    🔄 刷新、✖ 关闭、⚡ 点击秒回 + 3s 节流、"
echo "    💾 磁盘缓存降级、📄 分页浏览"
echo ""
echo " 使用："
echo "  • /model → 打开控制台"
echo "  • 点模型按钮 → 热切换（新对话生效）"
echo "  • 点 ⭐ → 收藏/取消收藏"
echo "  • 点 🔄 → 强制拉最新 API"
echo ""
echo " 当前模型: kdns / gpt-5.6-sol"
echo " —— 茉莉 🌸"