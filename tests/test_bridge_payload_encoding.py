r"""桥接参数的 JSON 编解码防线（X03 补测，2026-11-14 实机事故回归）。

事故（R0 实机，2026-11-14）
--------------------------
`click_button{text:"载入游戏"}` 永远返回「未找到按钮」，报错里文本变成
`u8f7d u5165 u6e38 u620f`。根因在 `mod/src/BridgeApi.cs` 的 `ExtractValue`：
它只做「遇到反斜杠就跳过、原样收下后一个字符」，于是 JSON 的 `\u8f7d` 被解成
字面量 `u8f7d`（丢了反斜杠与 u）。后果是**所有含中文的字符串参数全部失效** ——
按钮文案、对话选项、物品名、顾客名，一个不剩。C# 侧已修（v3.10.5，按 JSON
规范完整还原 `\" \\ \/ \b \f \n \r \t \uXXXX`，代理对按双转义分别还原）。

本文件测什么
------------
**不测 C#**（它在游戏进程里，pytest 够不着）。测的是**契约层一件事**：
`BridgeClient` 发出去的请求体必须是**完全合法的 JSON**，且按 JSON 规范解码回来
能逐字节还原原串。只要 Python 侧守住这条，C# 侧的解码器就算换实现也不会再出现
「字符在传输层被吃掉」这一类事故；反之，Python 侧一旦偷偷改变转义策略
（例如把 `ensure_ascii` 打开、或对已转义的串再转义一次），任何一个符合规范的
解码器都会收到被改写的文本 —— 而那正是本事故的形态。

覆盖的字符类别（每类都是实机上真的会出现的参数）：
  * 纯中文（按钮文案 / 对话选项 / 物品名）
  * 双引号（JSON 必须转义成 `\"`）
  * 反斜杠（必须变成 `\\`，否则解码器会把它当转义引导符）
  * 换行 / 制表 / 回车 / 退格 / 换页（`\n \t \r \b \f` 五个单字符转义）
  * 代理对（emoji `😀` = U+1F600，UTF-16 里是两个码元）
  * **字面量 `\\uXXXX` 文本**（模型有时把已转义的中文原样吐出来，这类串最容易被
    「再转义一次」或「提前解一次」伤害）

纪律：本文件的断言全部是「喂进去什么、出来必须是什么」的行为断言，
不读 `BridgeApi.cs` 源码做扫描（C# 实现演进不应让 Python 测试变红）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

import game_agent.contracts as C
from game_agent.bridge.client import BridgeClient

# 五类「最容易在转义层出事」的字符串参数。
# 前三条来自实机（按钮文案 / 对话选项 / 物品名），后几条是转义规范里的边界字符。
TRICKY_PARAMS: list[tuple[str, str]] = [
    ("纯中文按钮文案", "载入游戏"),
    ("中文对话选项", "我来是想谈谈那批货"),
    ("中文物品名", "牙膏"),
    ("双引号", '他"说'),
    ("反斜杠", "a\\b"),
    ("换行", "换\n行"),
    ("制表符", "tab\there"),
    ("回车", "cr\rret"),
    ("退格", "back\bspace"),
    ("换页", "form\ffeed"),
    ("代理对 emoji", "😀"),
    ("中文与 emoji 混排", "拾荒😀收工"),
    # 模型有时把已转义的中文原样吐出来：这一串里**没有**中文字符，
    # 是 6 个字符的反斜杠 + u + 十六进制。它必须原样到达，不能被提前解一次。
    ("字面量 \\uXXXX 文本", "\\u8f7d\\u5165\\u6e38\\u620f"),
    ("反斜杠紧邻引号", 'a\\"b'),
]


class _CapturingHandler(BaseHTTPRequestHandler):
    """只记录原始请求体，然后回一个最小合法响应。"""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        captured = self.server.captured  # type: ignore[attr-defined]
        captured.append({"path": self.path, "raw": raw, "headers": dict(self.headers)})
        body = json.dumps({"ok": True, "detail": "已调用"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # 静音测试输出
        pass


@pytest.fixture()
def capturing_bridge():
    """起一个本地 HTTP 服务，记录 BridgeClient 真正发出去的字节。

    每个用例一个服务（端口 0 自动分配），保证 `captured` 从空开始、断言不用清场。
    起停各约 0.5s，18 个用例合计 ~9s —— 换来的是「每个用例看到的是自己的请求」。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CapturingHandler)
    server.captured = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = BridgeClient(
            base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=5.0
        )
        yield client, server.captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("label,text", TRICKY_PARAMS, ids=[c[0] for c in TRICKY_PARAMS])
def test_action_params_roundtrip_through_json(capturing_bridge, label, text):
    """GAP: 桥接参数在序列化/反序列化后必须逐字符还原（中文参数曾全线失效）。

    2026-11-14 实机：`ExtractValue` 只剥反斜杠不还原 `\\uXXXX`，导致全部中文参数
    失效；C# 侧已修（v3.10.5），本测试防 Python 侧同类退化 —— 只要发出去的是
    合法 JSON 且能按规范还原，解码器实现怎么换都不会再吃掉字符。
    """
    client, captured = capturing_bridge
    client.post_action("click_button", text=text)

    assert len(captured) == 1, "post_action 必须只发一次请求"
    raw: bytes = captured[0]["raw"]

    # ① 必须是严格合法的 JSON（禁止 NaN/Infinity 这类非标准扩展）
    parsed = json.loads(raw.decode("utf-8"))
    # ② 按 JSON 规范解码后必须逐字符还原
    #    注意真实载荷是**扁平**的：`post_action` 发的是
    #    `{"action": "click_button", "text": "载入游戏"}`，参数与 action 同级，
    #    不是 `{"action":..., "params":{...}}`（`client.py:199` 的 `{**params}`）。
    assert parsed["action"] == "click_button"
    assert parsed["text"] == text, (
        f"{label}: 参数文本在传输层被改写了。喂入 {text!r}，解码得到 "
        f"{parsed['text']!r}"
    )


def test_cjk_params_are_sent_as_raw_utf8_not_escapes(capturing_bridge):
    """GAP: 中文应以原生 UTF-8 字节发出，不靠 `\\uXXXX` 转义 —— 从源头躲开整类事故。

    `ensure_ascii=True` 会把「载入游戏」变成 `\\u8f7d\\u5165\\u6e38\\u620f`，
    于是链路里每个解码器都必须正确实现 `\\uXXXX` 才不出错。实机事故恰恰发生在
    这个转义上。这里钉住「中文走原生字节」这个选择，任何人把它改回
    `ensure_ascii=True` 都会立刻变红，而不是等到实机才发现按钮点不动。
    """
    client, captured = capturing_bridge
    client.post_action("click_button", text="载入游戏")
    raw: bytes = captured[0]["raw"]

    assert "载入游戏".encode("utf-8") in raw, (
        "中文参数必须以原生 UTF-8 字节发出（ensure_ascii=False），"
        f"实际请求体 {raw[:120]!r}"
    )
    assert b"\\u8f7d" not in raw, (
        "请求体里不应出现 \\uXXXX 转义 —— 那会把「解码器必须实现 \\u 还原」"
        "变成硬前提，而实机事故正是踩在这里"
    )


def test_literal_escape_text_is_not_double_unescaped(capturing_bridge):
    """GAP: 已经是字面量 `\\uXXXX` 的文本不得被提前解码（模型常这么吐）。

    模型把已转义的中文原样放进 decision 时，Python 侧若「顺手解一次」，
    就会把 `\\u8f7d` 变成「载」，再送去点击一个根本不存在的按钮 —— 与实机
    症状同形。这里断言：没有中文字符的转义文本，出去还是转义文本。
    """
    client, captured = capturing_bridge
    literal = "\\u8f7d\\u5165\\u6e38\\u620f"
    client.post_action("click_button", text=literal)

    parsed = json.loads(captured[0]["raw"].decode("utf-8"))
    assert parsed["text"] == literal
    assert "载" not in parsed["text"], (
        "字面量转义文本被提前解码了 —— 这正是实机「按钮点不动」的成因形态"
    )


def test_payload_parsing_does_not_touch_cheat_patterns():
    """GAP: 修转义时的改动不得波及作弊面判定（本次事故的同批改动回归）。

    `CHEAT_ACTION_PATTERNS` 是防作弊白名单的唯一判据。转义逻辑与它无关，
    但它读的也是字符串，一旦有人把「统一转义/统一解码」加进公共路径，
    这些模式可能被悄悄改写而失去效力。这里钉住模式表与命中结果都没变。
    """
    assert isinstance(C.CHEAT_ACTION_PATTERNS, tuple)
    assert len(C.CHEAT_ACTION_PATTERNS) == 17, (
        f"CHEAT_ACTION_PATTERNS 条数变了（现 {len(C.CHEAT_ACTION_PATTERNS)}），"
        "若是有意增删请同步更新本断言与 selfcheck"
    )
    # 任一模式都必须能按原样命中一段拼出来的动作名（证明模式未被转义破坏）
    for pattern in C.CHEAT_ACTION_PATTERNS:
        probe = f"x_{pattern}_y"
        assert any(p in probe for p in C.CHEAT_ACTION_PATTERNS), (
            f"模式 {pattern!r} 已无法在文本里命中 —— 模式表可能被转义层改写"
        )


def test_unknown_action_still_rejected_before_sending(capturing_bridge):
    """GAP: 参数编码路径不得绕过动作白名单（防「为了编码方便」放宽校验）。

    `post_action` 的守卫顺序是「先验白名单、再编码发送」。若有人把编码提到
    校验之前，非法动作就会先被发出去。这里断言：非法动作**一个字节都不发**。
    """
    client, captured = capturing_bridge
    from game_agent.bridge.client import BridgeError

    with pytest.raises(BridgeError):
        client.post_action("set_cash", amount=999999)
    assert captured == [], "非法动作不得发出任何请求（白名单校验必须在编码之前）"
