# Feature Requests

---

## [FEAT-20261020-001] 决策浮层 UI 美化

**Logged**: 2026-10-20T13:15:00+08:00
**Priority**: medium
**Status**: in_progress
**Area**: frontend

### Requested Capability
用户要求把游戏内 AI 决策浮层做得更好看。

### User Context
现版浮层为朴素的深色方块+三行文本，观感一般。用户希望更有设计感。

### Complexity Estimate
medium

### Suggested Implementation
重做 bridge/DecisionOverlay.cs：程序化圆角 sprite（失败降级方形）、投影层、标题栏强调块、分区排版（动作/参数/理由/历史/页脚）、置信度进度条、加宽面板。

### Metadata
- Frequency: first_time
