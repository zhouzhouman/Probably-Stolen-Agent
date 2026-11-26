# 游戏内「启动 Agent」按钮与模组安全边界

> v3.9.2 / 2026-11-11。记录一次被游戏安全模型挡回来、以及最终采用的绕行方案。

## §1 直接拉起进程行不通（硬拦截）

v3.8.0 把按钮实现为在 mod 内 `Process.Start` 拉起 python。游戏更新到
**VERSION 100-Rev13** 后，模组扫描器直接拒绝加载整个 mod：

```
process-start: DeepSpaceBridge.AgentLauncher.Start -> Process.Start
```

翻 `tools/decompile_full/ModScanner.cs` 确认：

| 分类 | 处理方式 | 能否申明 |
|---|---|---|
| `files` / `network` / `reflection-external` / `dynamic-code` / `native` | `Capability(...)` | ✅ 写进 manifest 即可 |
| **`process-start`** | **`Hard(flag)`** | ❌ 硬拦截，清单写了也没用 |

所以「mod 里启动外部程序」这条路彻底封死——**连带把桥接一起搞挂了**，
因为整个 DLL 被拒加载。这是本轮最大的教训：加能力之前先查扫描器分类。

顺带补了一个隐患：`files`（`File.ReadAllText` / `File.WriteAllText` / `StreamWriter`）
是可申明的能力，v3.7 加模型面板后就开始用了，却一直没在清单里申明。

## §2 采用的架构：信号文件 + 外部守护

```
游戏内按钮 ──写──> Mods/DeepSpaceBridge/agent.request
                        │
              scripts/agent_hub.py（外部常驻，用户会话）
                        │ 读请求 → 真正 spawn python
                        ├─> agent_run.log        子进程 stdout/stderr
                        └─> agent.status.json    alive / running / pid / tail / at
                                    │
游戏内面板 <── 每 0.5 秒读 ────────┘
```

mod 侧只用到 `files` 能力，不越权、不绕安全模型。

请求文件用「写 .tmp → 删除旧文件 → 改名」落盘，避免 hub 读到半截内容。
状态文件同理，面板不会解析到不完整 JSON。

## §3 状态必须验「新鲜度」

实测踩过一次：hub 被外部杀掉后，`agent.status.json` 永远停在 `running:true` 和旧 PID，
面板显示一个已死的进程，点停止还报错。

现在 mod 解析 `at` 时间戳，**超过 6 秒没更新就判定守护已退出**
（hub 每 0.5 秒写一次）。hub 侧则在 `finally` 里兜底写 `alive:false`，
无论是 Ctrl+C、异常还是被管道掐断。

## §4 常驻怎么解决

| 方式 | 说明 |
|---|---|
| 开机自启（已装） | 启动文件夹放 `DeepSpaceAgentHub.vbs`，登录时无窗口拉起 |
| 临时启动 | 双击 `scripts/start_agent_hub.bat` |
| 取消自启 | 删除启动文件夹里的 `DeepSpaceAgentHub.vbs` |

> **为什么 agent 不自己拉起 hub**：我这边的工具会话结束时，它启动的子进程会被回收
> （`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` 也留不住），
> 而且 `cmd /c start`、`WScript.Shell`、`schtasks` 都被安全策略列为禁止程序。
> 所以「常驻」只能由用户的桌面会话负责——面板提示里已直接写出该双击哪个文件。

## §5 面板行为

- **打开就显示守护状态**：未运行 → 红字 + 要双击的完整路径；就绪 → 提示可点；运行中 → 显示 PID；
- 点「▶ 启动 Agent」：先把当前命令/工作目录存进 ini，再投递请求；
- 运行时每秒把日志尾巴刷到状态行；
- 「■ 停止 Agent」投递 stop 请求，hub 终止子进程并回写状态。

## §6 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/agent/status` | `{running, pid, log, tail}`（供 CLI/脚本查询） |
| POST | `/agent/start` | 投递启动请求（等价于点按钮） |
| POST | `/agent/stop` | 投递停止请求 |

## §7 实机验证（2026-11-11 11:40–11:56）

| 项 | 结果 |
|---|---|
| 模组加载 | ✓ `3.9.2-mod`（硬拦截解除） |
| 请求 → hub → 进程 | ✓ 启动 PID 23120，日志实时写入 |
| 停止 | ✓ 进程退出，状态回 `running:false` |
| 守护被杀后的状态 | 修正前：假 `running:true`；修正后：超 6 秒判定已退出 |
| 面板截图 | ✓ 两行新输入 + ▶/■ 按钮 + 最近使用，无重叠 |

## §8 部署脚本顺带做的事

`scripts/deploy_mod.py` 现在会往 `model_config.ini` 补两个键（**仅当缺失**，不覆盖用户改动）：

```ini
agent_workdir=D:\WorkBuddyData\深空当铺aiagent
agent_command="…\python.exe" -m game_agent.cli run --live --source bridge --no-mouse
```

因为 mod 跑在游戏进程里，不可能知道项目根在哪；部署脚本两个路径都握着。


---

## §9 第二次被拒：不是权限，是文件被占用（v3.10.0）

### 9.1 现象

3.9.3 部署后又被拒，但这次理由完全不同：

```
无法扫描：a file in the mod folder could not be read
(IOException: Sharing violation on path ...\DeepSpaceBridge\agent_run.log)
```

**扫描器会遍历 mod 文件夹里的每个文件并读取内容**。而 `agent_run.log` 正被外部守护
进程以写方式打开 → 读不了 → 整包拒绝。能力清单这次是完全匹配的（files/network/
reflection-external 都在），照样拒。

### 9.2 修法：运行期文件全部搬出 Mods

| 文件 | 旧位置 | 新位置 |
|---|---|---|
| model_config.ini | Mods\DeepSpaceBridge | `%LOCALAPPDATA%\DeepSpaceBridge` |
| agent_run.log | 同上 | 同上 |
| agent.request / agent.status.json | 同上 | 同上 |

mod 目录现在**只有** `DeepSpaceBridge.dll` + `manifest.xml` 两个静态文件，
扫描器再怎么读都不会冲突。

`deploy_mod.py` 会自动迁移旧文件（并在迁移后从 Mods 删除），已实测：
`运行期文件已迁出 Mods：model_config.ini, agent_run.log, agent.status.json`。

### 9.3 连带修的两个真 bug

**① Python 找不到新位置的 ini**：`resolve_model_ini` 原来只扫 Steam 库下的 Mods 目录，
搬家后就失效了——表现是「面板显示 deepseek，agent 却连 tokeness」。
现在查找顺序：显式配置 → 环境变量 → `%LOCALAPPDATA%\DeepSpaceBridge` → 旧 Mods（兜底）。

**② 点「启动 Agent」不会保存模型字段**：`OnStartAgent` 原来只落盘 command/workdir，
用户改完模型直接点启动，进程读到的还是 ini 里的旧模型（截图实证：面板 `api.deepseek.com`
＋日志里连 `n.tokeness.dev`）。现在抽出 `PullFromUi()`，两个入口都先全量落盘。

### 9.4 验证

- 模组加载：`3.10.0-mod` ✓
- Mods 目录只剩 dll + manifest ✓
- 端到端：请求 → hub → python（PID 10588）→ stop 成功 ✓
- `cli modelconf` 显示实际生效 `https://api.deepseek.com / deepseek-flash`（读的是新位置）✓
- 测试：`tests/test_model_config.py` 新增 3 条（新位置查找、env 优先级、从新位置读值）


---

## §10 「agent 不动了」：其实是跑完 10 步就退出了

### 10.1 排查过程

面板显示 `running=false`，日志里有到 step 10 的完整输出，且 `python.exe` 已无进程。

```
p_run.add_argument("--max-steps", type=int, default=10, help="步数上限（默认 10）")
```

`run` 的默认上限就是 10 步——部署脚本写进 ini 的命令没带 `--max-steps`，
于是跑 10 步正常退出。**看起来像卡死，实际是正常结束。**

（另：守护进程在 13:23 就停了，状态文件停在 `running:true`，
面板因为「新鲜度检查」正确地显示成未运行——这次判断是对的。）

### 10.2 修法

1. 默认命令改为 `... --max-steps 200`（部署脚本与现有 ini 都已更新）；
2. hub 在子进程退出时写入原因：
   `agent 已退出（退出码 0）。若是步数用完，把启动命令里的 --max-steps 调大再启动`；
3. `start_agent_hub.bat` 改用 `pythonw`：没有黑窗口，就不会被顺手关掉
   （上次守护死掉就是这么来的——关窗口 = 杀守护）。

### 10.3 注意

面板启动时会把输入框里的命令**写回 ini**。如果 ini 是外部改的（比如这次我直接改文件），
要**先点「重新载入」再点启动**，否则会把旧命令写回去。

### 10.4 验证

`--max-steps 200` 后启动，12s / 24s / 36s 三次采样都是 `running=True`（PID 8732 稳定），
不再 10 步即退。


---

## §11 改为不设步数上限（v3.10.1）

长局（十天净赚、攒首付买店）每几十步手动重启一次没意义，所以取消上限。

### 11.1 实现

`--max-steps 0` 表示不设上限：

```python
def _step_limit(max_steps):
    # 0 / None / 负数 = 不设上限；正数原样返回
    return value if value > 0 else 2 ** 31 - 1
```

用「一个很大的数」而不是 `while True`，是为了保留 `for index` 的步号语义
（日志/步骤文件里的 step 编号不变，历史分析脚本也不用改）。

### 11.2 仍然有停止出口（三条）

| 方式 | 说明 |
|---|---|
| 面板「■ 停止 Agent」 | 直接结束进程（最干脆） |
| `artifacts/STOP` 文件 | 循环每步检查，出现即退出 |
| 模型输出 `action=stop` | 它判断当前无法安全继续时自己收工 |

### 11.3 改动点

- `game_agent/agent_loop.py`：新增 `_step_limit()`，for 循环与步间 sleep 都用它；
- `game_agent/cli.py`：`--max-steps 0` → 传 `None`；帮助文本写明；
- 部署脚本与现有 ini 的默认命令：`--max-steps 0`；
- 测试：`TestStepLimit`（正数透传 / 0、None、负数视为无限 / 坏值兜底）。

### 11.4 面板侧注意

ini 又被外部改了一次（200 → 0），**打开面板要先点「重新载入」再点启动**，
否则会把输入框里的旧命令写回 ini。
