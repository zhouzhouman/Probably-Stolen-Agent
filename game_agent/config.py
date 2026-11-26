"""配置加载模块。

优先级：环境变量（含 .env 文件） > config/agent.json > 内置默认值。
"""
from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Any

# 桥接客户端超时的硬性下限（T2）：必须大于 C# 插件主线程派发上限 15s
# （bridge/BridgeApi.cs：RunOnMain(timeoutMs = 15000)）。客户端超时若小于
# 服务端派发超时，客户端放弃等待后服务端仍会执行动作，AI 重试将导致
# 重复执行（重复扣费/成交）。推荐值 20s，与 bridge/client.py 的
# DEFAULT_TIMEOUT 一致。
BRIDGE_MIN_TIMEOUT_SECONDS = 15.0
BRIDGE_RECOMMENDED_TIMEOUT_SECONDS = 20.0

# 内置默认配置（config/agent.json 缺省时使用，也作为合并基底）
DEFAULT_CONFIG: dict[str, Any] = {
    "game": {
        # 用于匹配游戏窗口标题的关键词（任意一个命中即可），不区分大小写
        "window_title_keywords": ["深空当铺", "Probably Stolen", "ProbablyStolen"],
        # 明确排除的窗口标题关键词（避免误匹配 Steam 客户端等）
        "exclude_title_keywords": ["Steam"],
        # 最小窗口尺寸（宽, 高），过滤小工具窗口
        "min_window_size": [400, 300],
        # Unity 持久化路径自动推导参数：
        # %USERPROFILE%/AppData/LocalLow/<unity_company>/<unity_product>/
        "unity_company": "Questing Goose Studio",
        "unity_product": "Probably Stolen",
    },
    "statewatch": {
        # 游戏持久化目录（GameLog 日志与 save 存档所在）；留空 = 按上方公司/产品名自动推导
        "data_dir": "",
        # 轮询间隔（秒）：日志跟随 + 存档变化检查
        "poll_interval": 0.5,
        # 事件环形缓冲上限（保留最近多少条事件）
        "max_events": 500,
    },
    "vision": {
        # provider: openai_compat（任意 OpenAI 兼容视觉接口） | mock（离线假实现）
        "provider": "openai_compat",
        "base_url": "",
        "model": "",
        # 读取 API Key 的环境变量名（找不到时回退 OPENAI_API_KEY）
        "api_key_env": "VISION_API_KEY",
        "timeout_seconds": 60,
        "max_tokens": 1200,
        "temperature": 0.2,
        # 是否请求 response_format=json_object（部分服务商不支持，默认关闭）
        "json_response_format": False,
        # 发送给模型前的图像最长边（像素），超过则等比缩小以控制成本
        "max_image_side": 1600,
    },
    "brain": {
        # 决策模型（文本）：把结构化观察 + 历史翻译成下一步动作
        "provider": "openai_compat",
        "base_url": "",
        "model": "",
        "api_key_env": "BRAIN_API_KEY",
        "timeout_seconds": 60,
        "max_tokens": 800,
        "temperature": 0.3,
        # 提示词中携带的最近操作历史条数
        "history_size": 6,
    },
    "control": {
        # 鼠标移动时长（秒）：分步插值移动，更贴近真实操作、兼容游戏悬停逻辑
        "move_duration": 0.35,
        # 动作执行后的固定等待（秒），给游戏留出响应时间
        "after_action_delay": 0.6,
    },
    "structured": {
        # 观察通道优先级：桥接（游戏内插件，毫秒级）→ 状态监听（文件直读，
        # 百毫秒级）→ 视觉（截图+模型，兜底）。mock 模式强制走视觉。
        "prefer": ["bridge", "statewatch", "vision"],
        # 事件驱动主循环：轮询间隔（秒）与无变化心跳上限（秒）。
        # 心跳上限内状态签名无变化也强制执行一步，防止动画/未覆盖字段导致静默。
        "poll_interval": 0.5,
        "event_idle_limit": 20.0,
    },
    "knowledge": {
        # 机制知识库（v2.2）：默认关闭——先 `cli knowledge --validate/--select`
        # 检查卡片质量，再打开。开启后每步按局面检索 1~3 张机制卡注入提示词。
        "enabled": False,
        "dir": "knowledge/mechanics",
        "max_cards": 3,
        "max_chars": 1800,
        "min_score": 3.0,
    },
    "paths": {
        "artifacts_dir": "artifacts",
    },
}


def project_root() -> Path:
    """定位项目根目录。

    从本文件向上查找第一个包含 config/agent.json 或 pyproject.toml 的目录；
    找不到时回退到当前工作目录。
    """
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "config" / "agent.json").is_file() or (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()


def _load_dotenv(path: Path | None = None) -> None:
    """从项目根目录的 .env 文件加载环境变量（不覆盖已存在的变量）。

    格式：每行 KEY=VALUE；支持 # 注释；值两侧的引号会被去除。
    这样 API Key 等敏感配置可以放在本地 .env 文件（已在 .gitignore 中忽略），
    无需每次手动设置环境变量。重复调用是安全的（幂等）。
    """
    env_path = path or (project_root() / ".env")
    if not env_path.is_file():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass  # .env 读取失败不阻断（例如权限问题）


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典（override 覆盖 base），返回新字典，不修改入参。"""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------- 游戏内模型配置

# 游戏内 Ctrl+F9 面板写入的 ini 文件名（与 mod DLL 同目录）。
_MODEL_INI_NAME = "model_config.ini"
_MODEL_INI_RELATIVE = ("Mods", "DeepSpaceBridge", _MODEL_INI_NAME)


def _steam_libraries() -> list[Path]:
    """尽量找出 Steam 库目录（用于定位游戏的 Mods 文件夹）。失败返回空列表。"""
    roots: list[Path] = []
    try:
        # Steam 的库列表：<Steam>/steamapps/libraryfolders.vdf，里面每行一个 "path"
        for base in (
            Path("C:/Program Files (x86)/Steam"),
            Path("C:/Program Files/Steam"),
            Path("D:/Steam"),
            Path("D:/SteamLibrary"),
            Path("E:/SteamLibrary"),
        ):
            vdf = base / "steamapps" / "libraryfolders.vdf"
            if vdf.is_file():
                text = vdf.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    if '"path"' in line:
                        parts = line.split('"')
                        if len(parts) >= 4:
                            roots.append(Path(parts[3]))
                roots.append(base)
    except OSError:
        pass
    # 去重保序
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def resolve_model_ini(config: dict[str, Any]) -> Path | None:
    """定位游戏内模型配置 ini：显式配置 → 环境变量 → Steam 库自动探测。"""
    explicit = str((config.get("bridge") or {}).get("model_config") or "").strip()
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None

    env_path = os.environ.get("GAME_AGENT_MODEL_INI", "").strip()
    if env_path:
        path = Path(env_path)
        return path if path.is_file() else None

    # v3.10：运行期文件已搬出 Mods（扫描器会读 mod 目录里每个文件，日志被占用会整包拒绝）。
    # 新位置是用户数据目录，与 mod 侧 ModelConfig.DataDir 一致。
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    candidate = Path(local) / "DeepSpaceBridge" / _MODEL_INI_NAME
    if candidate.is_file():
        return candidate

    # 旧位置兜底（v3.10 之前放在 Mods 里；迁移后一般不存在）
    for lib in _steam_libraries():
        candidate = lib.joinpath("steamapps", "common", "Probably Stolen", *_MODEL_INI_RELATIVE)
        if candidate.is_file():
            return candidate
    return None


def read_model_ini(path: Path | None) -> dict[str, str]:
    """读 ini（逐行 key=value）。文件不存在/不可读返回空字典——绝不抛错。"""
    if path is None:
        return {}
    out: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().lower()
            if key:
                out[key] = value.strip()
    except OSError:
        return {}
    return out


def _apply_model_ini(config: dict[str, Any]) -> None:
    """把游戏内面板保存的设置叠加到配置上（空值 = 沿用原配置）。

    只作用于决策模型（brain）：面板文案已说明；视觉通道保持配置文件里的设置，
    避免把只支持文本的自定义模型中当成图像模型用。
    副作用之外还写入 _model_ini，供 get_api_key 优先取值。
    """
    path = resolve_model_ini(config)
    values = read_model_ini(path)
    config["_model_ini"] = {"path": str(path) if path else "", "values": values}
    if not values:
        return

    brain = config.setdefault("brain", {})
    if values.get("base_url"):
        brain["base_url"] = values["base_url"]
    if values.get("model"):
        brain["model"] = values["model"]
    if values.get("max_tokens"):
        try:
            brain["max_tokens"] = int(values["max_tokens"])
        except ValueError:
            pass
    if values.get("temperature"):
        try:
            brain["temperature"] = float(values["temperature"])
        except ValueError:
            pass


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """加载配置：默认值 <- JSON 文件 <- 环境变量覆盖（自动加载 .env）。"""
    _load_dotenv()
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝默认值

    config_path = Path(path) if path else project_root() / "config" / "agent.json"
    if config_path.is_file():
        try:
            user_config = json.loads(config_path.read_text(encoding="utf-8"))
            config = _deep_merge(config, user_config)
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"配置文件解析失败：{config_path}（{exc}）") from exc

    # 环境变量覆盖（便于临时切换模型 / CI 使用）
    env_map = {
        "GAME_AGENT_VISION_BASE_URL": ("vision", "base_url"),
        "GAME_AGENT_VISION_MODEL": ("vision", "model"),
        "GAME_AGENT_VISION_PROVIDER": ("vision", "provider"),
        "GAME_AGENT_BRAIN_BASE_URL": ("brain", "base_url"),
        "GAME_AGENT_BRAIN_MODEL": ("brain", "model"),
        "GAME_AGENT_BRAIN_PROVIDER": ("brain", "provider"),
    }
    for env_name, (section, key) in env_map.items():
        value = os.environ.get(env_name)
        if value:
            config[section][key] = value
    # 游戏内模型设置面板（Ctrl+F9）写入的 ini：优先级低于环境变量、高于 json 文件，
    # 这样"在游戏里改一下"能立刻影响下一轮，同时仍允许用环境变量临时压过。
    _apply_model_ini(config)
    _check_bridge_timeout(config)
    return config


def _check_bridge_timeout(config: dict[str, Any]) -> None:
    """桥接超时配置校验（T2）：客户端超时必须大于服务端派发上限 15s。

    配置值 < BRIDGE_MIN_TIMEOUT_SECONDS 时输出警告——不阻断、不修改配置值
    （用户显式配置优先，只提示风险）；值缺失或非数字时跳过（保持既有行为，
    由 BridgeClient 自行处理）。
    """
    raw = (config.get("bridge") or {}).get("timeout_seconds")
    if raw is None:
        return
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return
    if timeout < BRIDGE_MIN_TIMEOUT_SECONDS:
        warnings.warn(
            f"bridge.timeout_seconds={raw} 小于服务端主线程派发上限 "
            f"{BRIDGE_MIN_TIMEOUT_SECONDS:.0f}s（C# RunOnMain timeoutMs=15000）："
            "客户端超时放弃后服务端仍会执行动作，AI 重试将导致重复执行"
            f"（重复扣费/成交）；建议改为 {BRIDGE_RECOMMENDED_TIMEOUT_SECONDS:.0f}",
            stacklevel=2,
        )


def get_api_key(config: dict[str, Any], section: str = "vision") -> str | None:
    """按配置读取 API Key：优先 section.api_key_env 指定的变量，其次 OPENAI_API_KEY。

    注意：查找前会自动加载 .env（幂等），保证 Key 放在 .env 中即可生效。
    """
    _load_dotenv()
    section_cfg = config.get(section, {})
    default_env = "VISION_API_KEY" if section == "vision" else "BRAIN_API_KEY"
    env_name = section_cfg.get("api_key_env") or default_env

    # 游戏内面板填的 Key 优先（用户刚在游戏里改了模型，通常 Key 也一起换）
    if section == "brain":
        ini = config.get("_model_ini") or {}
        ini_key = (ini.get("values") or {}).get("api_key")
        if ini_key:
            return ini_key

    return os.environ.get(env_name) or os.environ.get("OPENAI_API_KEY")


def artifacts_dir(config: dict[str, Any], ensure: bool = True) -> Path:
    """返回 artifacts 目录（默认自动创建）。"""
    rel = config.get("paths", {}).get("artifacts_dir", "artifacts")
    path = Path(rel)
    if not path.is_absolute():
        path = project_root() / path
    if ensure:
        path.mkdir(parents=True, exist_ok=True)
    return path
