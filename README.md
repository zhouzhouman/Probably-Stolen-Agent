# 深空当铺 AI Agent（deepspace-agent）

让 AI **自主游玩真实运行中的 Steam 游戏**《深空当铺：可能是偷得》（Probably Stolen，正式版 appid 4348910）。

> 项目原则：不重制游戏、不创建模拟版、不生成文字替代环境。AI 通过游戏原生 mod 暴露的
> **双协议工具层**操控真实游戏进程，在窗口化/无边框模式下完成完整经营闭环。

## 三个技术亮点

**① 五阶段决策流水线（单模型 + 代码守卫）**
感知 → 机制知识库 → 规划 → 校验 → 执行，**严格顺序、每轮一个原子动作、执行后重新感知**。
模型不直接调用游戏接口；非法动作在三层白名单（决策校验 → 客户端 → C# case）被拒绝并回退重规划。
所有守卫来自实盘事故（r36/r49/r55/r68/r78/r97…），带编号留痕。

> 说明：这里的「五阶段」指同一条流水线内的五个阶段，**不是**五个独立 LLM 实例。
> 关键判断尽量由代码算成结论（`visible_info.next_step_hints`）再交给模型，
> 而不是把判断都寄托在模型自觉上。

**② 双协议工具层**
- **自研 HTTP 桥**（`127.0.0.1:8965`）：毫秒级状态直读 + 68 个游戏 API 动作 case，供内部闭环高频调用；
- **标准 MCP 工具服务**（`127.0.0.1:28739/mcp`，Streamable HTTP / JSON-RPC 2.0）：供任意 AI 客户端即插即用。

两份工具的 schema 都从 `game_agent/contracts.py` **单一契约源派生**，启动时双向比对——
杜绝「模型按提示词输出、校验层拒绝」的漂移。这是踩过 ACTIONS 59 / 参数表 58 / C# case 61
三套清单各自演进的坑之后的设计。

**③ 执行安全三件套**
动作前置守卫（白名单 + 昼夜/弹窗/解锁/租金红线）→ 场景陈旧状态检测（`scene` 不匹配即拒，
绝不下发动作）→ 执行后 `Verify` 回读（只认现金/库存/天数真实变化，不信任 `ok=true`）。

## 架构

```
  任意 AI 客户端 ──MCP(JSON-RPC 2.0)──┐
                                      ├─→ 五阶段流水线 ──→ 8965 桥客户端 ──→ 游戏原生 mod ──→ 游戏进程
  本项目 CLI / 自研 brain ─────────────┘        │                                  │
                                        感知/知识/规划/校验/执行              HTTP /state · /action
                                        (contracts.py 单一真相)               + 游戏内决策浮层
```

三通道感知（共享决策层，自动按 `bridge → statewatch → vision` 优先级选择）：
- **bridge**：HTTP 直读内存状态 + 直调游戏 API（首选，毫秒级）；
- **statewatch**：直读游戏明文日志与存档（无需插件）；
- **vision**：截图 + 视觉模型（兜底，秒级）。

## 快速开始

```bash
pip install -e .                                     # 安装（首次）

python -m game_agent.cli bridge state                 # ① 读实时状态（需游戏运行 + mod 已启用）
python -m game_agent.cli run --source bridge --max-steps 5   # ② 自主跑 5 步（默认 dry-run）
python -m game_agent.cli mcp                          # ③ 启动 MCP 工具服务（供外部 AI 客户端接入）

python -m game_agent.cli goal                         # 查看商业目标进度
python -m game_agent.cli knowledge --validate         # 校验机制知识卡
python -m pytest -q tests                             # 全量测试（当前 874 passed）
```

启动游戏：`steam://rungameid/4348910`（直接跑 exe 会退出码 53）。必须**窗口化/无边框**运行。
关闭：`taskkill`（关闭后 DLL 才可覆盖部署）。

> 项目已获用户授权：可自行启动/关闭游戏做测试。

## MCP 工具服务

`python -m game_agent.cli mcp` 启动后，连接信息（url + 每次启动随机的 Bearer token）写入
`artifacts/mcp.connection.json`。把它注册到任意支持 Streamable HTTP 的 AI 客户端：

```json
{"mcpServers": {"deepspace": {"url": "http://127.0.0.1:28739/mcp",
                              "headers": {"Authorization": "Bearer <从连接文件读取>"}}}}
```

暴露两个工具：
- `game_state`：读 `/state`，可选 `section` 裁剪顶层段；
- `game_action`：执行单个原子动作（56 个模型可控动作），带 `scene` 陈旧守卫与执行后回读。

工具 schema 由 `python -m game_agent.contracts --export-mcp` 导出，不另写副本。
设计取舍（为什么不放在 C# mod 侧）见 `docs/28-简历向MCP融合方案.md`。

## 商业目标（30 天内现金净增 3000）

记分口径：**赚 = 现金净增**（库存估值只作余额底线，避免囤货刷分）。配置在
`config/agent.json` 的 `goal` 段，进度落盘 `artifacts/goal_state.json`，跨批次续跑。

> ⚠ 「3000」是短期现金流目标，**不是买房首付**。正式版店价 12,000 / 24,000 / 36,000，
> 首付按难度比例算（`max(500, round(店价×比例/500)×500)`）。买店/按揭数字一律实时读
> `/state.mortgage.*`，不在提示词或文档写死。

## 机制知识库（203 张卡 / 12 类）

机制不堆在提示词里，而是拆成**可检索、可验证的卡片**（触发条件 / 事实 / 该做 / 禁忌 /
后果 / 证据 / 置信度）。每步按 `/state` 检索 1~3 张（≤1,800 字符）注入；检索规则：
触发器为主、关键词只匹配值、置信度只做同分微调、同类别去重。

```bash
python -m game_agent.cli knowledge --validate      # 校验
python -m game_agent.cli knowledge --select        # 用当前局面检索
python scripts/audit_cards.py --strict             # 质量审计
python scripts/gen_knowledge_doc.py --check        # 总纲同步检查
```

来源与版本：`knowledge/wiki_raw/`（原始摘要，含 Demo 期快照）→ `knowledge/mechanics/`
（正式版 1.0 校正后的卡片）→ `docs/13-维基机制知识总纲.md`（自动生成）。
Demo 期数值冲突的校正表见 `docs/25` §0.3。

## 接口方法论

AI 的每个动作都对应**玩家在游戏里能做的操作**，三级并如实标注：
UI 级（点按钮/选对话/拖拽）> 库存 API 级（容器间搬运，走游戏接受校验）> 字段级（仅标记）。

自绘 PixelWindow 没有 Button 组件，因此补了通用输入层：`/ui/objects`（全量可点击对象）、
`ui_hit`（只读射线）、`ui_click`（派发 pointerDown/Up/Click）、`ui_drag`（完整拖拽链）、
`object_action`（OnClick/OnDoubleClick）。完整对照见 `docs/16`。

## 目录结构

```
├── game_agent/
│   ├── cli.py              # 命令行入口（bridge / run / knowledge / goal / mcp ...）
│   ├── contracts.py        # 动作/状态/MCP 工具的唯一真相源 + 自检
│   ├── mcp_server.py       # MCP 工具服务（JSON-RPC 2.0 / Streamable HTTP）
│   ├── config.py           # 配置加载（默认值 + agent.json + 环境变量 + 游戏内面板）
│   ├── bridge/             # HTTP 桥客户端（8965，显式禁用代理）
│   ├── statewatch/         # 日志/存档直读通道
│   ├── vision/             # 视觉观察（兜底）
│   ├── control/            # 鼠标/键盘/窗口控制
│   ├── brain/              # LLM 决策 + 提示词 + 校验 (prompt/schema/brain/client)
│   ├── knowledge/          # 机制卡 schema + 检索 + 渲染
│   ├── structured/         # 结构化观察 + 状态签名
│   ├── goal.py             # 商业目标跟踪
│   └── agent_loop.py       # 主循环（观察→知识→规划→校验→执行→落盘）
├── mod/                    # 【活跃】正式版原生 mod（C# / netstandard2.1 / Mono）
│   ├── manifest.xml        #   manifest（双版本声明 1.0.0 + 0.46D，能力 network+files）
│   └── src/                #   BridgeApi / BridgeServer / GameAccess / 浮层 / 模型面板
├── bridge/                 # 【历史基线】试玩版 BepInEx/IL2CPP 插件，勿改
├── knowledge/mechanics/    # 203 张机制卡（12 类，JSON）
├── config/agent.json       # 生效配置
├── scripts/                # 部署 / 运行 / 审计 / 分析脚本
├── tests/                  # 单元测试（874 passed，离线可跑）
├── artifacts/              # 运行产物（steps / observations / play_journal.md / STOP）
└── docs/                   # 设计/评估/报告（01 ~ 28）
```

## 配置

`config/agent.json` 的 `brain` 段配模型（`base_url` / `model` / `max_tokens`），
API Key 放项目根 `.env`。游戏内 Ctrl+F9 面板可直接改模型配置并写回 ini。
视觉通道另需 `VISION_API_KEY`。

## 文档索引（按主题）

- 架构与改造：`docs/24-架构评估与可优化项.md`、`docs/27-改造执行台账.md`
- 全机制接口：`docs/25-全机制接口设计与实现建议.md`
- 多智能体协作提示词：`docs/26-多智能体协作修改提示词.md`
- 简历向 MCP 融合：`docs/28-简历向MCP融合方案.md`
- 机制知识：`docs/12-AI机制知识体系设计.md`、`docs/13-维基机制知识总纲.md`
- 多 Agent 架构：`docs/08-多Agent协作架构.md`
- 实盘报告：`docs/10` / `docs/11`（旧目标）、`docs/17` / `docs/18`（新档与 Bug 台账）
- 正式版迁移：`docs/19` / `docs/20`

历史 Demo 期文档（口径已过时，仅留档，不要照做）：`docs/01`~`docs/07`。

## 项目边界（诚实说明）

这是一个**个人技术项目**，不是可直接交付的产品。下表区分已验证与未验证：

| 已验证 | 说明 |
|---|---|
| 完整营业日闭环 | 实机跑通 Day 11→12：开门 → 接待 → 送客 → 卖货 → 打烊 → 拾荒 → 收纳 → 回店 → 开清洁服务 → 次日垃圾归零、吸引力恢复满值 |
| 动作可靠执行 | 68 个动作走游戏原生路径（UI 按钮/Toggle 优先），失败有兜底与回读校验 |
| 关键假成功已修 | 例：`TabletCheckbox.SetOn()` 不触发 `onChanged`，曾导致"勾选清洁"返回成功但 `is_using` 仍为 false |
| 回归保护 | 918 个单元测试 + mod 合规扫描 |

| 未验证 / 不具备 | 说明 |
|---|---|
| 长期经营能力 | 未验证能否连续多日盈利、能否避免破产；"跑通流程"不等于"经营得好" |
| 开箱即用 | 依赖本地 Steam 游戏本体、本地 LLM 服务与自行编译的 mod，他人无法一键复现 |
| 生产级健壮性 | 单机自用；无并发、无掉线重连、无多存档管理 |
| 版权内容 | 不含游戏本体、反编译源码与第三方逆向工具（见 `.gitignore`），需自行准备 |

## 快速开始（本地）

```bash
python -m game_agent.cli observe          # 只观察不动作，确认能读到游戏状态
python -m game_agent.cli step --dry-run   # 走一次决策但不下发动作
python -m game_agent.cli step --live      # 真实执行一个原子动作
python -m game_agent.cli run --steps 20   # 连续自主运行
```

前置条件：游戏以**窗口化/无边框**运行（全屏下截图与控制都会失败）、mod 已部署到
`<游戏根>/Mods/DeepSpaceBridge/`、`.env` 里配置好模型服务地址与 Key。
