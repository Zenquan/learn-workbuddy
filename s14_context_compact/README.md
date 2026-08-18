# s14: Context Compact — 上下文总会满, 要有办法腾地方

> *"上下文总会满, 要有办法腾地方"* — 四层压缩管线，保最新、弃最旧、留摘要。
>
> **Harness 层**: 上下文管理 — agent 的记忆预算。

---

![四层上下文压缩管线](./images/context-compact.svg)

## 代码架构图

```mermaid
flowchart LR
    A["Transcript-derived messages"] --> B["Deep-copy prompt view"]
    B --> C["L1 truncate → L2 dedup → L3 prune → L4 summary"]
    C --> D["Compacted messages"]
    H["Selected MemoryHit"] --> P["capture source / score / rank / conflict"]
    P --> S["DurableContextState"]
    S --> R["Lossless renderer"]
    R --> E["Next API call"]
    D --> E
    S -. "bypasses lossy layers" .-> C
```

## 学习前置知识

- 压缩不是截断, 而是结构化重建上下文。
- 不同角色需要不同压缩策略: 主会话、子任务、摘要恢复。
- 触发阈值应该在溢出前, 不是报错后。
- Transcript、Memory 和当前 Prompt 是生命周期不同的视图；压缩只允许改变最后一个。

## 本章抓住的 WorkBuddy-style 机制

- 吸收公开 Agent 架构中的分层压缩与结构化摘要思路。
- 四层策略只处理可丢弃、可重建的 messages 副本，不修改 Transcript 派生输入。
- 用不可变 `DurableContextState` 单独携带已确认事实、未决事项和已选检索证据。
- 每条 durable item 强制保留 source pointer 与 `last_confirmed_at`，并在下一轮 system context 中结构化渲染。
- `capture_retrieval_evidence()` 只冻结上游已经选中的 hit，不在压缩阶段重做 scope、score、冲突或预算裁决。

## 常见误区

- 简单删除早期消息, 会丢掉用户原始意图。
- 让生成式摘要负责保存 durable fact，模型可能改写事实或遗漏 pending task。
- 压缩后不标注来源, 后续很难验证。
- 原地修改 messages 会连带污染 Transcript 回放或调用方保存的证据视图。
- 摘要生成失败后仍用错误字符串替换旧历史，会静默丢失最后一份可用上下文。
## 问题

agent 跑得越久，消息历史越长。一次对话可能产生几十条消息——每次工具调用的输入输出都堆在 `messages` 列表里。模型的上下文窗口是有限的（128K、200K，无论多大终归有限），一旦超限，API 直接报错。

这不是边缘情况。一个真正干活的 agent——读文件、跑命令、改代码——几轮工具调用就能吃掉几万 token。长对话必须有一种机制：**在不丢失关键信息的前提下，压缩上下文。**

你不能简单地删掉旧消息——那会让 agent 失忆，重复之前做过的事。也不能不删——那会让 API 调用失败。你需要一个**分层压缩策略**：先做最廉价的压缩，不够再做更激进的，层层递进。

---

## 解决方案

四层压缩管线，从轻到重依次触发：

| 层级 | 策略 | 做什么 | 代价 |
|------|------|-------|------|
| Layer 1 | 工具结果截断 | 大输出截断为摘要 | 低（不丢消息） |
| Layer 2 | 文件内容去重 | 同一文件多次读取 → 只留最新 | 低（不丢消息） |
| Layer 3 | 消息历史修剪 | 删除旧的非关键消息 | 中（可能丢细节） |
| Layer 4 | 全对话摘要 | 用模型生成摘要替换历史 | 高（一次 API 调用） |

```
每次 API 调用前检查上下文大小:

  messages token count
        │
        ▼
  ┌─────────────┐
  │ < 阈值?     │── Yes ──▶ 正常调用 API
  └─────────────┘
        │ No
        ▼
  ┌─────────────────────────────────────┐
  │ Layer 1: 截断超大工具结果             │
  │ (单条 tool_result > 5000 tokens?)    │
  └─────────────────────────────────────┘
        │ 还超?
        ▼
  ┌─────────────────────────────────────┐
  │ Layer 2: 文件内容去重                 │
  │ (同一文件被读多次? 只留最新一次)       │
  └─────────────────────────────────────┘
        │ 还超?
        ▼
  ┌─────────────────────────────────────┐
  │ Layer 3: 修剪旧消息                   │
  │ (保留最近 N 轮, 旧的删除)             │
  └─────────────────────────────────────┘
        │ 还超?
        ▼
  ┌─────────────────────────────────────┐
  │ Layer 4: 生成摘要替换历史              │
  │ (调用模型总结, 替换全部旧消息)          │
  └─────────────────────────────────────┘
```

**关键原则**：系统提示、工具定义与 `DurableContextState` **永远不进入有损压缩层**。压缩器先深拷贝 messages，四层只操作这份可丢弃 Prompt 视图；已确认事实、未决事项，以及已选 MemoryHit 的来源、分数、排名和冲突标记沿旁路进入下一次 API 调用。

### 压缩对象边界：Messages 可以有损，Durable state 必须无损

```python
@dataclass(frozen=True)
class DurableFact:
    fact_id: str
    content: str
    source_pointer: str
    last_confirmed_at: str

@dataclass(frozen=True)
class PendingItem:
    item_id: str
    description: str
    source_pointer: str
    last_confirmed_at: str

@dataclass(frozen=True)
class RetrievalEvidence:
    memory_id: str
    source_id: str
    source_type: str
    source_title: str
    captured_at: str
    score: float
    source_rank: int
    conflict_key: str | None = None

@dataclass(frozen=True)
class DurableContextState:
    facts: tuple[DurableFact, ...] = ()
    pending_items: tuple[PendingItem, ...] = ()
    retrieval_evidence: tuple[RetrievalEvidence, ...] = ()
```

`frozen=True` 防止压缩流程就地改写字段，tuple 防止在 state 内追加或删除条目。构造时还会拒绝空 ID、空 source、重复 ID、越界 score、非正 rank 和没有时区的时间。这里的 `source_pointer` 可以指向 s09 Transcript event，也可以指向 s13 Artifact；`RetrievalEvidence` 则保存一次已选检索结果的来源与裁决元数据。两者都让压缩后的上下文仍能回到原始证据。

```text
可压缩 messages                     不可有损 durable state
----------------                    -----------------------
旧对话细节                          已确认事实
重复文件读取                        未决事项
大工具结果的上下文副本              source pointer
探索过程                            last_confirmed_at
已召回正文的重复表述                retrieval source / score / rank / conflict
```

生成式摘要即使遗漏任务，甚至错误地把“SQLite WAL”写成“JSON 文件”，也只能污染一次 conversation summary，不能修改 `DurableContextState`。下一轮 Prompt 由 `render_durable_context()` 重新注入原始结构化事实。

---

## 工作原理

### Token 计数与阈值检查

每次 API 调用前，先估算当前 `messages` 的 token 数量。超过阈值就触发压缩：

```python
TOKEN_THRESHOLD = 100_000  # 触发压缩的阈值

def estimate_tokens(messages: list) -> int:
    """粗略估算 messages 的 token 数。

    生产级 harness 常用 tiktoken 精确计数。
    教学版用 4 字符 ≈ 1 token 的粗略估算。
    """
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content) // 4
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(json.dumps(block)) // 4
                else:
                    total += len(str(block)) // 4
    return total
```

### Layer 1: 工具结果截断

工具返回大块输出（比如读了一个 5000 行的文件）是上下文膨胀的主要原因。第一层策略：截断超大工具结果。

```python
MAX_TOOL_RESULT_TOKENS = 5000

def truncate_tool_results(messages: list) -> list:
    """Layer 1: 截断超过 5000 token 的工具结果。"""
    for msg in messages:
        if msg["role"] != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                result = block.get("content", "")
                tokens = len(str(result)) // 4
                if tokens > MAX_TOOL_RESULT_TOKENS:
                    # 这里只做有界截断；真正摘要属于 Layer 4
                    truncated = str(result)[:MAX_TOOL_RESULT_TOKENS * 4]
                    block["content"] = (
                        truncated +
                        f"\n\n[... 已截断, 原始长度 {len(str(result))} 字符 ...]"
                    )
    return messages
```

### Layer 2: 文件内容去重

同一个文件被读多次（agent 先读了全文，改了几行后又读了一次确认）——旧的读取结果是冗余的，只留最新的。

```python
def dedup_file_reads(messages: list) -> list:
    """Layer 2: 同一文件多次读取, 只保留最新一次。"""
    # 找到每个文件路径最后一次读取的位置
    last_read = {}  # path -> (msg_index, block_index)
    for mi, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if (isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("_tool_name") == "read_file"):
                path = block.get("_tool_input", {}).get("path", "")
                last_read[path] = (mi, bi)

    # 删除非最新的文件读取结果
    to_remove = set()
    for path, (mi, bi) in last_read.items():
        # 找所有更早的同文件读取
        for mi2, msg in enumerate(messages[:mi]):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for bi2, block in enumerate(content):
                if (isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("_tool_name") == "read_file"
                    and block.get("_tool_input", {}).get("path") == path):
                    to_remove.add((mi2, bi2))

    # 执行删除
    for mi, bi in sorted(to_remove, reverse=True):
        del messages[mi]["content"][bi]

    return messages
```

### Layer 3: 消息历史修剪

前两层不够时，开始删除旧消息。保留最近 N 轮对话，旧消息直接删除：

```python
KEEP_RECENT_TURNS = 6  # 保留最近 6 轮

def prune_old_messages(messages: list) -> list:
    """Layer 3: 保留最近 N 轮, 删除旧消息。

    注意: 不能删除中间的 tool_result 而留下 tool_use —
    那会导致 API 报错。必须成对删除。
    """
    if len(messages) <= KEEP_RECENT_TURNS:
        return messages

    # 保留最近 N 条消息
    kept = messages[-KEEP_RECENT_TURNS:]

    # 确保不以孤立的 tool_result 开头
    while kept and isinstance(kept[0].get("content"), list):
        first = kept[0]["content"][0] if kept[0]["content"] else None
        if isinstance(first, dict) and first.get("type") == "tool_result":
            kept = kept[1:]
        else:
            break

    return messages[:1] + kept  # 保留第一条用户消息作为上下文
```

### Layer 4: 全对话摘要

最激进的策略——用模型生成旧对话摘要，同时保留最近消息。它只总结 conversation messages，不接收 `DurableContextState`。如果模型调用失败或返回空摘要，函数保留原消息并报告节省 0 token，不能用“摘要失败”字符串覆盖历史：

```python
def generate_summary(messages: list, summarizer) -> tuple[list, int]:
    """Layer 4: 生成对话摘要替换历史。

    调用模型总结到目前为止的对话,
    用摘要替换旧消息, 保留最近几轮。
    """
    old_messages = messages[:-4]  # 保留最近 4 条
    recent = messages[-4:]

    try:
        summary = summarizer(json.dumps(old_messages)).strip()
    except Exception:
        return messages, 0
    if not summary:
        return messages, 0

    summarized = [
        {"role": "user", "content": f"[对话摘要]\n{summary}"},
        {"role": "assistant", "content": "好的, 我已了解之前的对话内容。"},
    ] + recent
    return summarized, estimate_tokens(messages) - estimate_tokens(summarized)
```

### 在循环中的位置

```python
def agent_loop(messages: list, durable_state: DurableContextState):
    while True:
        result = compact_context(messages, durable_state)
        messages = result.messages
        durable_context = render_durable_context(result.durable_state)

        response = client.messages.create(
            system=SYSTEM + "\n\n" + durable_context,
            messages=messages,
            ...,
        )
        # ... 正常循环 ...
```

`compact_context()` 返回 `CompactionResult`，同时记录压缩前后 token 和实际触发的层。它会深拷贝输入 messages，因此原始 Transcript 回放视图不随 L1/L2 的就地整理发生变化。兼容入口 `compact_if_needed()` 仍只返回 messages，方便前面章节的调用方式保持简单。

---

## 教学版的触发与停止条件

本章只有一个公开触发条件：`estimate_tokens(messages) >= TOKEN_THRESHOLD`。触发后按 L1 → L4 依次尝试；每层结束都重新估算，低于阈值就立即停止。本章没有声称某个外部产品采用特定百分比、内部 Agent 名称或环境变量。

```text
低于 80,000 tokens ──> 不压缩，返回深拷贝
达到 80,000 tokens ──> L1 → 检查 → L2 → 检查 → L3 → 检查 → L4
                         └──────── 任一层达标即停止 ────────┘
```

`HARD_LIMIT` 是后续练习可使用的紧急上限常量，当前调度器尚未为它实现第二套策略，因此不能把它描述成已经存在的 emergency 模式。

---

## 压缩不是 Memory 写入

结构化摘要可以尽量保留意图、关键操作和当前进度，但它仍是模型生成的、有损的 Prompt 视图，不能作为长期事实的唯一副本：

```text
Transcript events ──派生──> messages ──有损压缩──> compacted messages
Memory records ───────────> DurableContextState ──无损渲染──> system context
```

两条路径只在下一次模型请求时汇合。摘要说“任务已完成”不会自动关闭 pending item；只有经过 Memory 自己的确认与写入流程，durable state 才能改变。这也是为什么本章保留 source pointer：压缩后仍能回到 Transcript 或 Artifact 核验证据。

### 已选 MemoryHit：压缩正文，不压缩选择依据

检索结果的正文可能已经出现在旧消息里，L3/L4 可以删除或摘要这段正文；但如果来源、分数和冲突裁决也只存在于旧消息中，压缩后就无法回答“这条记忆为什么进入上下文”。本章把两部分拆开：

```text
Recall candidates
      │  scope → confidence → dedupe → conflict → top-k / budget
      ▼
selected hits ──capture_retrieval_evidence()──> immutable RetrievalEvidence
      │                                              │
      └── recalled text 进入 Prompt                  └── 绕过 L1–L4
```

`capture_retrieval_evidence()` 接收的是**已经选中的** hit，而不是全部候选。它用结构属性同时适配 S12 的 `RecallHit.rank` 和 S15 的 `MemoryContextCandidate.source_rank`，复制 `memory_id`、完整 provenance、原 score、rank 与可选 `conflict_key`。压缩层不重新排序，也不允许冲突败者因为窗口变化而回填；否则同一次检索会在压缩前后产生两套决策。

`conflict_key` 存在时，渲染结果明确标成 `conflict_winner=<key>`；不存在则标成 `conflict=none`。它不是让摘要模型再次判断冲突，而是保存上游裁决的可审计标记。正文可以变短，选择依据必须保持原值。

---

## 生产化时还要补什么

本章刻意保持 clean-room 教学实现。生产 harness 通常还要根据自己的模型与协议补齐：

| 关注点 | 本章做法 | 生产化方向 |
|--------|----------|------------|
| token 计数 | 4 字符约 1 token | 使用目标模型 tokenizer，并计入 system 与 tools |
| 工具结果 | 保留有界前缀 | 按内容类型保留头尾，或外置为 Artifact |
| 协议完整性 | 避免以孤立 tool result 开头 | 按 tool-use ID 成对裁剪完整调用组 |
| 摘要失败 | 原消息原样保留 | 加超时、重试预算和可观测失败原因 |
| 长期事实 | durable state 旁路 | 接入带版本、冲突处理和来源校验的 Memory store |
| 检索证据 | 已选 hit 的不可变元数据 | 持久化 query/decision trace，并校验 source 可访问性 |

这些是可验证的设计方向，不代表任何特定闭源产品的内部实现。

---

## 代码 walkthrough

`code.py` 实现了完整的四层压缩管线：

1. **`DurableFact` / `PendingItem`** — 不可变的事实与未决事项，强制携带 source pointer 和带时区的最近确认时间
2. **`RetrievalEvidence` / `capture_retrieval_evidence()`** — 从已选 hit 冻结来源、分数、排名与冲突标记，不复制召回正文
3. **`DurableContextState`** — 在有损管线旁路传递的 Memory 输入，并按各自 ID 域拒绝重复条目
4. **`render_durable_context()`** — 把 durable state 独立渲染进 system context，不混入生成式摘要
5. **`estimate_tokens()`** — 粗略估算 messages 的 token 数（4 字符 ≈ 1 token）
6. **`truncate_tool_results()`** — Layer 1: 截断超过 5000 token 的工具结果
7. **`dedup_file_reads()`** — Layer 2: 同一文件多次读取，只留最新
8. **`prune_old_messages()`** — Layer 3: 保留最近 N 轮，删除旧消息
9. **`generate_summary()`** — Layer 4: 用模型生成摘要替换历史；失败或空摘要时保留原历史
10. **`compact_context()`** — 深拷贝 messages，依次尝试四层，并返回 `CompactionResult`
11. **Agent 循环** — 每次 API 调用前压缩 disposable messages，再把 durable state 独立注入

运行后会看到压缩日志——每层触发时打印 `[compact]` 消息，可以看到哪些层在什么时候被触发。

---

## 运行

```bash
python s14_context_compact/code.py
```

试试持续对话，观察 token 计数增长和压缩触发。可以输入 `stats` 查看当前上下文使用情况。

---

## 练习

1. 给 pending item 增加状态机（open / blocked / done）。思考：完成状态由谁确认，如何避免摘要中的一句“已完成”越权修改 durable state？
2. 当前 Layer 4 的摘要是一次性生成。实现增量摘要：每次只摘要新增的 messages，与之前的摘要合并；然后设计测试证明 durable state 不参与摘要合并。
3. 为 source pointer 增加解析器，分别定位 `transcript:` 与 `artifact:` 来源。思考：来源已被清理或权限不足时，Prompt 应怎样呈现而不是伪造证据？
4. `estimate_tokens` 用 4 字符 ≈ 1 token 估算。安装 tokenizer 做精确计数，对比中英文和结构化 tool result 的偏差。

---

## 下一课

上下文满了能压缩。但系统提示本身也可能很大——WorkBuddy 的系统提示是运行时从十几个片段组装出来的。怎么组装？什么时候重新组装？

s15 Prompt Assembly → 运行时分段拼接 + 身份注入。
