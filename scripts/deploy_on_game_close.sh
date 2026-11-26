#!/usr/bin/env bash
# 等待游戏退出后自动部署新版 DeepSpaceBridge.dll
# 背景：游戏运行时 BepInEx/plugins/DeepSpaceBridge.dll 被进程锁定，无法覆盖。
# 用法：bash scripts/deploy_on_game_close.sh  （后台运行，最多等待 40 分钟）
set -u

SRC="D:/WorkBuddyData/深空当铺aiagent/bridge/_deploy/DeepSpaceBridge.dll"
DST="D:/SteamLibrary/steamapps/common/Probably Stolen Demo/BepInEx/plugins/DeepSpaceBridge.dll"
MARKER="D:/WorkBuddyData/深空当铺aiagent/bridge/_deploy/deploy_result.txt"

echo "watcher started $(date)" > "$MARKER"

for i in $(seq 1 800); do   # 800 * 3s ≈ 40 分钟
    if tasklist 2>/dev/null | grep -qi "Probably Stolen"; then
        sleep 3
        continue
    fi
    # 游戏已退出，等待文件锁彻底释放
    sleep 2
    if cp "$SRC" "$DST" 2>/dev/null; then
        echo "DEPLOYED OK $(date)" >> "$MARKER"
        ls -la "$DST" >> "$MARKER"
        exit 0
    else
        echo "COPY FAILED $(date) (file still locked?)" >> "$MARKER"
        exit 1
    fi
done

echo "TIMEOUT waiting for game to close $(date)" >> "$MARKER"
exit 2
