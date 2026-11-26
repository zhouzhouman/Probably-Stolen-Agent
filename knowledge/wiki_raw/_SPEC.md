# 维基抓取与卡片化规范（v1）

## 0. 任务性质

把 `https://probably-stolen.nicokobo.com/zh/wiki` 的机制文档，转成项目可用的两类产物：

1. **原始摘要**（人读、可审计）：`knowledge/wiki_raw/<NN>-<slug>.md`
2. **候选机制卡**（机读、可直接加载）：`knowledge/wiki_cards/<NN>-<slug>.json`

**禁止改写数值。** 页面里出现的所有数字、阈值、公式、百分比、天数、价格必须原样保留。
页面没写的一律不写，不要用常识补；确实需要推断时，在 `facts` 里写明「（推断）」。

## 1. 抓取规则

- 只用 `WebFetch`，域名限定 `probably-stolen.nicokobo.com`。
- 每个页面一次 `WebFetch`，prompt 要求「逐条提取、保留数字与表格、不要概括成一句话」。
- 页面内的 `/zh/wiki/**` 链接要顺着抓（同主题、最多再下钻 2 层），并在 md 里记录实际抓过的 URL。
- 页面 404 / 空内容：在 md 里记一行 `- （无内容 / 404）`，不要编造。

## 2. `knowledge/wiki_raw/<NN>-<slug>.md` 格式

```markdown
# <主题中文名>

> 抓取时间：2026-10-24 ｜ 覆盖页面：<n> 个

## <页面标题>
- URL: https://probably-stolen.nicokobo.com/zh/wiki/terms/xxx

### 机制要点
- 一句话一条，带数值。例：租金每 7 天一期，每期上涨 5%（Demo 内实测 321 → 337）。

### 表格 / 数值
| 项目 | 数值 | 说明 |
|---|---:|---|

### 触发与前置
- ...

### 后果与风险
- ...

### 与其他机制的关联
- ...

### 原文关键句
> 直接引用 1~3 句最有信息量的原文（保留专有名词）。
```

同一 md 里按页面顺序重复 `## 页面` 小节。**宁可长，不要漏。**

## 3. `knowledge/wiki_cards/<NN>-<slug>.json` 格式

顶层是**数组**，每个元素一张卡：

```json
[
  {
    "id": "rent-increase-rate",
    "title": "租金每期上涨 5%/10%",
    "category": "economy",
    "summary": "一句话（≤60 字）说明这条机制是什么、对经营意味着什么。",
    "facts": [
      "租金每 7 天一期；每期在上一期基础上上涨 5%，连续延期后按 10%。",
      "rent_due 才是本期实缴金额（含历史欠款与罚金），rent_value 只是名义租金。"
    ],
    "actions": ["交租日前把现金攒到 ≥ rent_due", "现金不足时优先卖低值杂货，不要拒付"],
    "pitfalls": ["用 rent_value 做预算：实测名义 242、实缴 693"],
    "effects": ["pay_rent 成功：现金 −rent_due，day_until_rent 重置为 7"],
    "guards": ["现金 < rent_due×3 时禁止大额进货"],
    "keywords": ["rent_due", "涨租", "pay_rent"],
    "triggers": {"any": [{"field": "visible_info.rent.day_until_rent", "op": "lte", "value": 2}]},
    "evidence": ["wiki: /zh/wiki/terms/rent-and-loans", "实测 r78：名义 242 / 实缴 693"],
    "related": ["rent-lease"],
    "confidence": 0.9,
    "verified_at": "2026-10-24",
    "source": "wiki:/zh/wiki/terms/rent-and-loans"
  }
]
```

### 3.1 字段硬约束

| 字段 | 必填 | 规则 |
|---|---|---|
| `id` | 是 | 小写字母/数字/`-`/`_`，2~64 字符，全局唯一 |
| `title` | 是 | 中文，≤20 字，说清机制 |
| `category` | 是 | 只能是 `loop trade customer economy security night phone quest production store risk meta` |
| `summary` | 是 | ≤60 字，回答「这是什么 + 对我有什么用」 |
| `facts` | 是 | ≥2 条；每条一个可执行事实，**必须带数值或明确判定条件** |
| `actions` | 否 | AI 可直接执行的动作建议，用动作名或中文指令 |
| `pitfalls` | 否 | 反例/踩坑，带后果 |
| `effects` | 否 | 动作成功后状态怎么变（用于事后核对） |
| `guards` | 否 | 前置约束（现金/声望/许可/容量） |
| `keywords` | 否 | **只放专有名词**：字段名、动作名、英文标识、独特中文术语。禁止「服务」「价格」「顾客」这类泛词 |
| `triggers` | 否 | `{all:[...], any:[...]}`；`field` 只能用下方字段字典里的路径；`op` 只能是 `eq ne in contains truthy falsy gte lte exists` |
| `evidence` | 是 | 至少一条，格式 `wiki: <路径>` 或 `实测 <轮次>` |
| `related` | 否 | 关联卡 id |
| `confidence` | 是 | 0~1；wiki 明确写了数值给 0.9+，只有描述性文字给 0.7~0.85 |
| `verified_at` | 是 | 最近一次核实的日期（`YYYY-MM-DD`）。**wiki 快照卡写抓取日 `2026-10-24`；用解包/实机核过的写核实日** |
| `source` | 是 | `wiki:<路径>` / `decompile:<cs 文件名>` / `live:<观测点>` / `docs:<文档名>` / `bridge:<接口>` / `test:<用例>`；**确实无来源可引时写「来源待核实：<理由>」，禁止编 URL 或文档名** |
| `game_version` | 否 | 见 §3.4。`"1.0.0"` / `"demo"` / `""`（无版本证据） |
| `verify_basis` | 否 | 见 §3.4。`"live_state"` / `"decompile"` / `"wiki_snapshot"` / `""` |
| `needs_verify` | 否 | 布尔。见 §3.4 |
| `gated_until` | 否 | 只允许 `"post-launch"`；不适用就**不写该键** |
| `deprecated` | 否 | 布尔；为 `true` 时必须同时有非空 `deprecated_reason` |
| `deprecated_reason` | 否 | 废弃理由，含「为什么不成立」与证据位置 |

### 3.2 触发器字段字典（唯一合法来源）

见同目录 `_observation_fields.txt`。常用：

```
visible_info.money                     现金
visible_info.day                       天数
visible_info.store_state               MORNING / OPEN / CLOSED
visible_info.scene                     当前界面（InventoryScene / ShopScene …）
visible_info.client                    null 或 {identifier,is_buying,is_selling,is_wanted,buying_tags,budget_left,…}
visible_info.client.is_wanted
visible_info.client.is_aug
visible_info.client.buying_tags        ["FOOD","MEDICAL",…]
visible_info.table.nego_open
visible_info.table.nego_buttons.primary / secondary / wholesale / accept_offer
visible_info.table.wholesale_cost
visible_info.queue.count / has_client / can_next
visible_info.rent.day_until_rent / rent_value / rent_due / can_pay_rent
visible_info.rent.garbage_level / is_power_on / shutter_closed
visible_info.economy.attractiveness / stock_value / retail_markup
visible_info.economy.loan_days / loan_value / lottery_number / wild_favor / insurance_days_without
visible_info.services[].id / cost / is_using / mandatory
visible_info.night.visit_left / is_outside / current_location / map_open / seriously_wounded / scavenging_attempts
visible_info.phone.open / book_shown / contacts[].name
visible_info.dialog.is_end / is_choice / text / choices
visible_info.inventory.owned[].identifier / item_types / value / count
```

v3.x 起新增、且**已确认真实发射**的路径（2026-11-14 补进 `_observation_fields.txt`）：

```
visible_info.client.buying_ids         客户端指定要买的物品 id 列表（BridgeApi.cs:1275）
visible_info.client.black_tags         客户端拒收的品类标签（BridgeApi.cs:1276）
visible_info.client.black_ids          客户端拒收的物品 id（BridgeApi.cs:1277）
visible_info.client.refused            逐件拒收判定结果（BridgeApi.cs:1299/1320，带缓存）
visible_info.client.intent / .stage    ⚠️ 非 C# 生产，只由 statewatch 通道填
                                       （contracts.py:1793-1801；observer.py:1374-1383）
visible_info.client.gun_order.tier / .accepted
visible_info.table.barter.acceptable / set_index / set_count
visible_info.difficulty.id / title / subtitle
visible_info.perks[].id
visible_info.afterhour_events[].{id,title,active,days_left,location,closes_location}
visible_info.locations[].{id,available,visits,last_visit_day,source}
visible_info.appraisal.rank / rank_name / progress / days_missed / promotion_pending
visible_info.risk.{crime_value,crime_count,contraband_crime,trafficking_crimes,luck,liquid_luck_days_left}
visible_info.aquaculture.items[].{identifier,name,count}
visible_info.mortgage.{store_price,down_payment,total_weeks,weekly_payment,weeks_left,can_be_offered}
```

**不存在的字段（不要写进 triggers）**：`visible_info.table.haggle`、`visible_info.client.faction`
—— 在 `mod/src/BridgeApi.cs` 里 grep 零命中，docs/26 的字段清单里这两项是笔误。

**没有可观测字段的机制**：`triggers` 留空 `{}`，只靠 `keywords` 命中，
并标 `keyword_only: true`（见 §3.6）在 `facts` 最后一条注明「当前观测层无对应字段，需扩展」。

### 3.3 质量红线

- ❌ 不要写「合理定价」「注意风险」这类空话；每条 fact 都要能被代码或动作验证。
- ❌ 不要把同一机制拆成 5 张卡；也不要两句话凑一张卡。
- ❌ 不要重复已有卡：`day-cycle` `wholesale-direction` `customer-buying-tags`
  `appraisal-commission` `rent-lease` `wanted-report` `night-service-cost`
  `scavenge-stow` `fish-farm` `faction-request`。这些机制**只补充新事实**，用新 id（如 `rent-increase-rate`），并在 `related` 里指向已有卡。
- ✅ 一张卡 = 一个可决策的机制单元。判据：AI 看完这张卡，能不能改变下一步动作。

### 3.4 版本标记字段（2026-11-13 新增，v2）

`knowledge/wiki_raw/` 全部内容抓于 **2026-10-24**，而《Probably Stolen》正式版 **2026-10-28** 才发售。
因此每张卡必须自己声明**凭什么是可信的**，而不是让读者假设所有 wiki 数值在 1.0 依然成立。

| 字段 | 取值 | 含义 |
|---|---|---|
| `game_version` | `"1.0.0"` | 已用**正式版实机快照**或**正式版解包**核对过 |
| | `"demo"` | **确认**该结论来自 2026-10-24 的 Demo 期 wiki 快照，且与 1.0 未对上 |
| | `""` | 无版本证据 —— 不知道它对不对。**这是绝大多数卡的正确答案** |
| `verify_basis` | `"live_state"` | 依据 `artifacts/state_v36.json` 一类 1.0 实机 `/state` 快照 |
| | `"decompile"` | 依据 `tools/decompile_full/*.cs` 中的具体行号 |
| | `"wiki_snapshot"` | 依据 `knowledge/wiki_raw/*.md`（Demo 期） |
| | `""` | 仅有人工/实测复盘记录，无法归入上面三类 |
| `needs_verify` | `true` | **默认值**。表示「可用于提示，但不得当硬规则」 |
| | `false` | 必须有 `game_version="1.0.0"` 且 `verify_basis` 非空才允许 |
| `gated_until` | `"post-launch"` | 官方确认未来才实装的内容（当前：远征/雇工线）。**不适用就不写该键** |
| `deprecated` | `true` | 结论在正式版已被证伪或机制已移除。**必须同时写 `deprecated_reason`** |

**联动规则（写卡时必须自检）**

1. `game_version=""` ⇒ `verify_basis=""`。
2. `game_version="demo"` ⇒ `verify_basis="wiki_snapshot"` ⇒ `needs_verify=true`。
3. `game_version="1.0.0"` ⇒ `verify_basis ∈ {"live_state","decompile"}`。
4. `needs_verify=false` 只允许出现在 `game_version="1.0.0"` 的卡上。
5. `gated_until="post-launch"` 的卡：`actions` 每条都以
   `【post-launch 未实装，勿执行】` 开头，`guards` 首条点名「1.0 未实装」。
6. `deprecated=true` 的卡**不物理删除**（保留作为「旧结论为什么错」的对照），
   但检索时应被 router 排除。

**铁律**：**不许把 Demo 数值「顺手修正」成另一个你猜的数字**。
不确定就留 `""` + `needs_verify: true`。「不知道」是可接受状态，「编一个看起来合理的数」不可接受。
凡是改了旧结论的卡，必须在 `facts` 末尾追加一条：

```
2026-11-13 校正：<旧结论> → <新结论>，依据 <文件:行号>。
```

### 3.5 `_observation_fields.txt` 的性质（重要）

该文件是 **2026-10-24 生成的 Demo 期合成状态**，只用于 `scripts/audit_cards.py`
校验 `triggers` 的字段路径是否存在，**不是 1.0 的字段取值依据**。已知 Demo 口径残留：

- `visible_info.economy.retail_markup = 15`（1.0 由难度覆盖为 20/15/0/0）
- `visible_info.economy.contraband_markup.* = 1.2/1.5/2.0/3.0`（**倍率**口径；
  1.0 是 35/70/100/150 的**百分比**口径，两者不可互换）
- `visible_info.mortgage.total_weeks = 14`、`down_payment = 2000`
  （1.0 期数按难度 8/12/16/16、首付按房价比例对齐 500）

引用这些值写卡时，必须按 §3.4 标 `needs_verify: true`。

### 3.6 `keyword_only`：有意无触发器的卡（2026-11-14 新增）

`triggers` 为空有**两种**完全不同的含义，必须能区分，否则无法判断是「设计选择」还是「漏做」：

| 写法 | 含义 | 判定责任 |
|---|---|---|
| `triggers: {}` 且 `keyword_only: true` | **有意**——该机制在观测层没有对应字段，只能靠 `keywords` 检索命中 | 写卡人已确认（读卡人看到即知） |
| `triggers: {}` 且无 `keyword_only` | **遗漏**——应补触发器 | 待补，计入审计缺口 |

当前 12 张 `keyword_only` 卡：Steam 成就/统计类 2 张（`ach-demo-steam-status`
`ach-steam-stat-counters`）、远征/雇工线 9 张（8 张 quest +
`risk/expedition-cut-accident`，同时带 `gated_until="post-launch"`）、
`store/network-upgrade-demo-lock`（已 `deprecated`）。这 12 张在观测层都没有可用信号：
成就进度不上 `/state`、远征未实装、废弃卡不需要触发。

**本字段当前无任何代码读取，是纯数据标记。** 已实测确认：`cards.py` 的 `MechanicCard`
没有 `keyword_only` 属性，`from_dict()` 会静默丢弃该键（`load_cards()` 后
`hasattr(card, "keyword_only") == False`）；`router.py` / `audit_cards.py` /
`gen_knowledge_doc.py` / `knowledge_staleness.py` 也都不读它。

因此**不要**以为 `audit_cards.py` 会因为本字段而少报「遗漏」——它目前不区分。
把「现在没人读」这个事实写在这里，是为了避免有人误以为审计口径已经生效。
若将来审计需要区分「有意无触发器」与「遗漏」，由本字段承载：需先在
`cards.py` 的 `MechanicCard` 加字段并让 `from_dict`/`to_dict` 往返一致，
再在 `audit_cards.py` 加分支。

## 4. 完成标准

- md 覆盖分配给你的**每一个** URL；
- json 卡片数 ≥ 页面数 × 0.7，且每张卡 JSON 合法（能被 `json.load` 解析）；
- 两个文件都用 UTF-8 无 BOM 写入。
