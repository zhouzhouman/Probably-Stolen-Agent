# 正式版迁移 · Agent 任务提示词

> 用法：整段复制给执行 Agent（建议在 `D:/WorkBuddyData/深空当铺aiagent` 工作区内运行）。
> 目标是**在保留现有能力的前提下**把整个项目从试玩版迁到正式版，不是重写。

---

## 角色与目标

你是这个项目的迁移工程师。项目现状：一个让 AI 自主游玩《深空当铺：可能是偷得》的智能体，
由三部分组成——Python 决策层（`game_agent/`）、C# BepInEx6 桥接（`bridge/`，HTTP 127.0.0.1:8965）、
机制知识库（`knowledge/`，194 张卡）。

**游戏已发布正式版（已购买，装在本机）。你的任务：把整套系统迁到正式版并可实机跑通。**

## 不可违反的约束

1. **玩家视角合法性**：所有操作必须是玩家在游戏里能做的（点按钮、选对话、拖拽、交易）。
   禁止直调内部方法凭空生成物品/资源/状态，禁止改存档，禁止绕开游戏自身校验。
2. **不重制游戏**：不写模拟版、不做文字替代环境，一切以真机为准。
3. **以状态变化为准**：「接口返回 ok」不算成功，必须看到现金/库存/日期的真实变化。
4. **不做无证据结论**：每个"修好了"都要附步骤 JSON、现金/库存前后值或截图。
5. **禁止用 mock 验证真机**：mock 决策不得推送到游戏内浮层（此前已有污染事故）。
6. **遇到签名不兼容，改代码适配**，不要用猜偏移、反射硬塞、忽略异常的方式糊过去。

## 第 0 步：基线快照（先做，且必须先做）

项目没有 git。开工前：

```bash
cd D:/WorkBuddyData/深空当铺aiagent
git init && git add -A && git commit -m "baseline: demo version (610 tests green)"
```

记录基线数字，后续每一步都跟它对账：测试 610 项、桥接动作 56 个、参数表 55 条、
知识卡 194 张、`tools/Il2CppDumper/dump.cs` 19 MB。

## 第 1 步：环境侦察（只读，不许动游戏文件）

产出一张表，写进 `docs/19-正式版迁移报告.md` §1：

- Steam appid、安装目录、可执行文件名、是否有独立 `*_Data` 目录；
- Unity 版本（看 `GameAssembly.dll` / `global-metadata.dat` 头部）、是否仍为 IL2CPP、metadata 是否可读；
- 存档目录：`%USERPROFILE%/AppData/LocalLow/<Studio>/<Game>/` 是否改名（这决定 statewatch 是否能继续用）；
- BepInEx 6 (IL2CPP x64) 是否需要在正式版目录重新安装、首次启动能否生成 interop；
- 正式版相对试玩版的内容规模（天数、物品数、机制），用于后面重标目标。

**硬性要求**：先只读侦察，确认可逆再动手；任何"装 BepInEx / 替换文件"都要先备份原文件。

## 第 2 步：反汇编重做 + 与试玩版做结构化 diff（本任务核心）

```bash
# 输出到新目录，绝不覆盖 tools/Il2CppDumper（它是试玩版基线）
tools/Il2CppDumper/Il2CppDumper.exe "<正式版>/GameAssembly.dll" \
  "<正式版>/*_Data/il2cpp_data/Metadata/global-metadata.dat" tools/Il2CppDumper_full
```

然后写一个 diff 脚本（放 `scripts/diff_dump.py`），比对两版 `dump.cs`：

- 类名集合的新增/删除/改名；
- 桥接实际依赖的类型逐项核对：`PlayerStore`、`EmporiumEntry`、`StoreClient`、
  `GameItem`、`GameInventory`、`GameGridInventory`、`MachineSlots` 相关、
  `DialogUIManager`、`MapUIManager`、`HealthData`、`Prompt/Overlay` 用到的 UI 类型；
- 每个依赖类型的**字段名、方法签名、返回类型**变化；
- 关键字符串常量（物品 identifier、对话文本、UI tooltip）是否变化。

产出一张变更清单表：`类型 | 变更内容 | 对桥接的影响 | 处理方式`。
**凡是"字段没了/改名了/签名变了"的，逐个给出替代方案，不允许留空。**

## 第 3 步：桥接重编译与端点回归

1. `bridge/DeepSpaceBridge.csproj` 里的 `BepInExDir` 默认值是试玩版路径，必须改（或用 `-p:BepInExDir=`）；
2. 在正式版目录装 BepInEx6 IL2CPP，启动一次让 interop 程序集生成；
3. 编译（本机 `dotnet` 不在 PATH）：`"/c/Program Files/dotnet/dotnet.exe" build ... --no-restore`；
4. 部署到正式版的 `BepInEx/plugins/`，`/ping` 通；
5. 逐端点回归，一条一条给结果：
   `/state` `/machines` `/sellable` `/expedition` `/ui/buttons` `/ui/objects` `/ui/nodes` `/decision` `/debug/slots`；
6. 56 个桥接动作按"只读优先、破坏性最后"的顺序冒烟；破坏性动作只用最小代价验证一次；
7. 记录基线对比：`/state` 字段有没有少、`owned/not_owned` 是否仍有重叠（已修过一次，别回退）。

## 第 4 步：Python 侧适配

- 观察层字段映射（`game_agent/structured/observer.py`）：正式版新增的段要接进来，
  原有压缩规则（`inventory` 体积、`refused` 缓存、每次外出拾荒计数）必须保留；
- `next_step_hints`（用代码算结论的那层）要复核：正式版新增机制是否该有新提示；
- **目标重新标定**：`config/agent.json` 的 `goal` 现在是"十日内净赚 1000"（试玩版口径），
  正式版流程更长，给出新口径并说明依据；
- 提示词（`game_agent/brain/prompt.py`）里写死的试玩版规则逐条复核：
  「打烊前检查单」「拾荒铁律」「房东铁律」「Demo 边界」等，该改的改、该删的删。

## 第 5 步：知识库重建

- 按正式版 wiki 重抓（沿用 `knowledge/wiki_raw/` + `knowledge/mechanics/` 卡格式，
  抓取脚本 `scripts/import_wiki_cards.py`，总纲生成 `scripts/gen_knowledge_doc.py`）；
- 旧 194 张卡逐条判定：**保持 / 需改数值 / 已废弃（正式版改了机制）/ 新增机制补卡**，
  废弃的不要直接删，标注 `deprecated: true` 并写原因；
- `python scripts/gen_knowledge_doc.py --check` 必须通过；
- `python scripts/audit_cards.py` 报告零死卡。

## 第 6 步：实机验收（硬性门槛）

两条都必须满足才算迁移完成：

**A. 回归测试**：`python -m pytest -q` 全绿（若因正式版语义变化必须调整测试，
逐条说明"为什么改、改成什么、证据"）。

**B. 实机连跑 20 步零失败**，且必须覆盖这条完整链路：
`开门 → 叫客 → 对话 → 成交（≥2 笔）→ 打烊 → 夜間服务 → 拾荒（scavenge→stow_loot→leave_outside）→ 进入次日`

验收时同时给：步骤 JSON 列表、现金与库存前后值、决策浮层截图（确认显示的是真实决策）。

## 交付物

1. `docs/19-正式版迁移报告.md`：环境表、diff 变更清单、逐端点回归结果、失败项与处置、
   目标重标定依据、遗留风险；
2. 更新 `docs/16-玩家操作接口对照表.md`（正式版新增/变更的接口）、`README.md`、`config/agent.json`；
3. 分阶段 commit（侦察 / 反汇编 / 桥接 / Python / 知识库 / 验收），每个 commit 可回滚；
4. 旧试玩版基线**保留**（`tools/Il2CppDumper/` 与 `bridge/_deploy/` 不覆盖），
   以便随时对比与回退。

## 反模式（出现即视为未完成）

- 把旧 `dump.cs` 当正式版用，或"差不太多就跳过重编译"；
- `TryCast` 失败就吞异常、靠猜字段名凑通；
- 接口返回 200 就宣布成功，不看现金/库存；
- 用 mock 运行去验证浮层/端到端；
- 为了让测试变绿而删测试或放宽断言（放宽必须有正式版依据）；
- 把"试玩版专属"的硬编码（appid 4349200、`Probably Stolen Demo` 路径）留在代码里。

## 报告格式

每个阶段结束输出四行：**做了什么 / 证据（文件或数据）/ 未解决的风险 / 下一步**。
遇到卡点先给"我判断是机制变更还是签名变更，依据是什么"，再问要不要改方案。
