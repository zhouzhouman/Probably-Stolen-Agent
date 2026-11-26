"""桥接测试探针：通过 DeepSpaceBridge 完成一次"开门→叫号→对话→读物品"全链路。

用法：
    python scripts/bridge_test.py              # 完整流程
    python scripts/bridge_test.py --state      # 只读状态
    python scripts/bridge_test.py --debug      # 只读调试信息

前置：游戏已运行且 DeepSpaceBridge 插件已加载（http://127.0.0.1:8965/ping 可达）。

说明：统一走 game_agent.bridge.client.BridgeClient（显式禁用系统代理）——
早期版本用裸 urllib，在设了 http_proxy 的环境下会把 127.0.0.1 请求发给代理，
返回 502 假故障。
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from game_agent.bridge.client import BridgeClient, BridgeError


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="DeepSpaceBridge 链路测试")
    parser.add_argument("--state", action="store_true", help="只读状态")
    parser.add_argument("--debug", action="store_true", help="只读调试信息")
    args = parser.parse_args(argv)

    client = BridgeClient()
    if not client.is_available():
        print("[失败] 桥接不可达：游戏需运行且 DeepSpaceBridge 插件已加载"
              "（http://127.0.0.1:8965/ping）")
        return 1

    if args.state:
        print(json.dumps(client.get_state(), ensure_ascii=False, indent=2))
        return 0
    if args.debug:
        print(json.dumps(client.get_debug_client(), ensure_ascii=False, indent=2))
        return 0

    # ---------- 完整流程 ----------
    print("== [1] 初始状态 ==")
    state = client.get_state()
    print(f"  store_state={state.get('store_state')} "
          f"shutter_closed={state.get('shutter_closed')} cash={state.get('cash')}")

    try:
        if state.get("store_state") == "MORNING" or state.get("shutter_closed"):
            r = client.post_action("open_shutter")
            print(f"== [2] 开门: ok={r.get('ok')} → {r.get('state', {}).get('store_state')} ==")
            time.sleep(2)
        else:
            print("== [2] 门已是开启状态 ==")

        print("== [3] 呼叫下一位顾客 ==")
        r = client.post_action("call_next")
        client_info = (r.get("state") or {}).get("client") or {}
        print(f"  ok={r.get('ok')} client={client_info.get('identifier')}")

        # 推进对话：直到顾客离店（处理导师剧情）
        print("== [4] 推进对话直至该顾客离店 ==")
        for i in range(15):
            state = client.get_state()
            cur = (state.get("client") or {}).get("identifier")
            if cur is None:
                print(f"  [{i}] 顾客已离店")
                break
            dlg = state.get("dialog") or {}
            print(f"  [{i}] client={cur} dialog={dlg}")
            if not dlg.get("is_end"):
                client.post_action("dialog_advance")
            elif cur:
                # 对话结束但人还在：推进确保完整结束
                client.post_action("dialog_advance")
            time.sleep(2.5)

        print("== [5] 再次呼叫（获取交易型顾客） ==")
        r = client.post_action("call_next")
        client_info = (r.get("state") or {}).get("client") or {}
        print(f"  ok={r.get('ok')} client={client_info.get('identifier')} "
              f"selling={client_info.get('is_selling')} buying={client_info.get('is_buying')}")

        print("== [6] 等待顾客站定并读取物品数据 ==")
        time.sleep(10)
        debug = client.get_debug_client().get("debug")
        print(json.dumps(debug, ensure_ascii=False, indent=2))
    except BridgeError as e:
        print(f"[失败] 桥接调用异常：{e}")
        return 1

    print("\n全链路测试完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
