"""Agent 启动守护进程（配合游戏内「▶ 启动 Agent」按钮）。

为什么需要它：游戏 Rev13 的模组扫描器把 process-start 定为**硬拦截**，
mod 里只要出现 Process.Start 就直接拒绝加载（v3.8.0 实测把桥接一起搞挂了）。
所以真正拉起 python 的活儿必须放到游戏进程外面。

工作方式：
    游戏内按钮 → 写 Mods/DeepSpaceBridge/agent.request
    本脚本轮询该文件 → 按 action 启停 agent
    每 0.5 秒把状态写 agent.status.json（alive/running/pid/tail），面板据此显示

用法：
    python scripts/agent_hub.py            # 前台跑（Ctrl+C 退出）
    python scripts/agent_hub.py --once     # 只处理一次请求就退出（调试用）
    python scripts/agent_hub.py --stop     # 顺手停掉正在跑的 agent

约定：
    · 请求文件处理完就删，避免重复触发；
    · 同一时刻只跑一个 agent（重复 start 会被拒绝并写进 tail）；
    · 状态文件用「写临时文件再改名」落盘，面板不会读到半截 JSON。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def _default_dir() -> Path:
    """运行期数据目录（与 mod 侧 ModelConfig.DataDir 一致）。

    为什么不放 Mods：Rev13 扫描器会读取 mod 文件夹里的每个文件，
    而 agent_run.log 被本进程写着 → Sharing violation → 整个 mod 被拒加载。
    """
    env = os.environ.get("GAME_AGENT_BRIDGE_DIR")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "DeepSpaceBridge"


DEFAULT_DIR = _default_dir()

REQUEST = "agent.request"
STATUS = "agent.status.json"
LOG = "agent_run.log"


def split_command(cmd: str) -> list[str]:
    """拆 `"exe path" args...`。Windows 路径含空格，不能简单 split。"""
    cmd = (cmd or "").strip()
    if not cmd:
        return []
    if cmd[0] == '"':
        end = cmd.find('"', 1)
        if end < 0:
            return []
        exe = cmd[1:end]
        rest = cmd[end + 1 :].strip()
        try:
            return [exe] + shlex.split(rest, posix=False)
        except ValueError:
            return [exe] + rest.split()
    parts = cmd.split(" ", 1)
    exe = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    try:
        return [exe] + shlex.split(rest, posix=False)
    except ValueError:
        return [exe] + rest.split()


def parse_request(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return data
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            data[k.strip().lower()] = v.strip()
    return data


def write_status(path: Path, *, running: bool, pid: int, tail: str) -> None:
    payload = {
        "alive": True,
        "running": bool(running),
        "pid": int(pid) if running else 0,
        "tail": tail,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        if path.exists():
            path.unlink()
        tmp.replace(path)
    except OSError:
        pass


def tail_log(log_path: Path, lines: int = 4) -> str:
    try:
        if not log_path.exists():
            return ""
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            content = fh.readlines()
        return "".join(content[-lines:]).strip()
    except OSError:
        return ""


class AgentProcess:
    def __init__(self, log_path: Path) -> None:
        self.proc: subprocess.Popen | None = None
        self.log_path = log_path
        self._log_fh = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> int:
        return self.proc.pid if self.running and self.proc else 0

    def start(self, command: str, workdir: str) -> str | None:
        if self.running:
            return f"已在运行（PID {self.pid}），先停止再启动"
        argv = split_command(command)
        if not argv:
            return "命令为空"
        exe = Path(argv[0])
        if not exe.exists():
            return f"找不到可执行文件：{exe}"
        wd = Path(workdir)
        if not wd.is_dir():
            return f"工作目录不存在：{wd}"

        self._log_fh = self.log_path.open("a", encoding="utf-8", errors="replace")
        self._log_fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动：{' '.join(argv)}\n")
        self._log_fh.flush()

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            self.proc = subprocess.Popen(
                argv,
                cwd=str(wd),
                env=env,
                stdout=self._log_fh,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self._log_fh.write(f"===== 启动失败：{exc}\n")
            self._log_fh.flush()
            return f"启动失败：{exc}"
        return None

    def stop(self) -> str | None:
        if not self.running or self.proc is None:
            return "没有在运行"
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        except OSError as exc:
            return f"停止失败：{exc}"
        if self._log_fh:
            self._log_fh.write("===== 已停止\n")
            self._log_fh.flush()
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="游戏内「启动 Agent」按钮的守护进程")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="Mods/DeepSpaceBridge 目录")
    ap.add_argument("--once", action="store_true", help="只处理一次请求就退出（调试）")
    ap.add_argument("--poll", type=float, default=0.4, help="轮询间隔秒数")
    ap.add_argument("--stop", action="store_true", help="启动前先停掉已有 agent")
    args = ap.parse_args()

    base = Path(args.dir)
    if not base.is_dir():
        print(f"× 目录不存在：{base}")
        return 2

    req_path = base / REQUEST
    status_path = base / STATUS
    log_path = base / LOG

    agent = AgentProcess(log_path)
    if args.stop:
        agent.stop()

    print(f"agent_hub 已启动，监听 {req_path}")
    print("（游戏内按 Ctrl+F9 → ▶ 启动 Agent；Ctrl+C 退出本守护）")

    last_note = ""
    try:
        return _loop(args, agent, req_path, status_path, log_path)
    finally:
        # 无论是 Ctrl+C、异常还是管道被关（head 掐断），都要把状态标成"守护已退出"，
        # 否则面板会一直显示最后一次的 running=true。
        agent.stop()
        try:
            status_path.write_text(
                json.dumps({"alive": False, "running": False, "pid": 0, "tail": ""},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass


def _loop(args, agent, req_path, status_path, log_path) -> int:
    last_note = ""
    was_running = False
    while True:
        try:
            if req_path.exists():
                data = parse_request(req_path)
                action = (data.get("action") or "").lower()
                note = ""
                if action == "start":
                    err = agent.start(data.get("command", ""), data.get("workdir", ""))
                    note = f"启动失败：{err}" if err else f"已启动（PID {agent.pid}）"
                elif action == "stop":
                    err = agent.stop()
                    note = f"停止失败：{err}" if err else "已停止"
                else:
                    note = f"未知动作：{action or '（空）'}"
                print(f"[{time.strftime('%H:%M:%S')}] {note}")

                try:
                    req_path.unlink()
                except OSError:
                    pass
                last_note = note

            running = agent.running
            if was_running and not running:
                # agent 自己退出了（模型 stop / STOP 文件 / 报错 / 被停）——说清楚，否则面板看着像卡死
                code = agent.proc.returncode if agent.proc else None
                last_note = (f"agent 已退出（退出码 {code}）。常见原因：模型输出 stop、"
                             f"artifacts/STOP 文件、或报错；详情见上方日志")
            was_running = running

            tail = tail_log(log_path)
            if last_note and not running:
                tail = (last_note + "\n" + tail).strip()
            write_status(status_path, running=running, pid=agent.pid, tail=tail)

            if args.once:
                return 0
            time.sleep(args.poll)
        except KeyboardInterrupt:
            print("\n收到 Ctrl+C，停止 agent 并退出")
            return 0


if __name__ == "__main__":
    sys.exit(main())
