# DeepEval 全量指标速查（v4.0.0，AI agent 视角）

> 数据来源：`.venv/Lib/site-packages/deepeval/metrics/` 源码逐文件读签名 + 实测。
> 评分类型说明：
> - **LLM judge** = 调用裁判模型（你项目里是 `ClaudeCliJudge`，等价于本地 `claude` CLI）
> - **规则 / 正则 / 集合比较 / Schema 校验** = 纯本地计算，不调外部模型
> - **MLLM judge** = 多模态模型裁判（图文）
>
> `base` 列说明：
> - `BaseMetric` → 单轮，吃 `LLMTestCase(input, actual_output, ...)`
> - `BaseConversationalMetric` → 多轮，吃 `ConversationalTestCase(turns=[Turn(role, content, ...)])`
> - `BaseArenaMetric` → 对比型（两份输出 PK），没有数值分
> - `BaseValueMetric` → **本仓自建**（`custom_metrics/_base.py`）。`score` 是原始数值（tokens/USD/秒等），不是 0-1；支持 `informational=True` 只采集不门禁。详见 §11
> - `requires_trace=True` → 必须配合 `@observe` 追踪树才能用

---

## 1. RAG（5 个）

| 指标 | 类型 | base | 评分原理 | 必读字段 | 关键参数 | AI agent 适用场景 |
|---|---|---|---|---|---|---|
| AnswerRelevancyMetric | LLM judge | BaseMetric | 把 `actual_output` 拆 statement，让裁判判断每条是否回应 `input`，相关数/总数 | input, actual_output | threshold, model, include_reason | 单轮回答是否切题筛查 |
| FaithfulnessMetric | LLM judge | BaseMetric | 从 `retrieval_context` 抽 truths，从 `actual_output` 抽 claims，逐条判 claim 是否被 truth 支持，支持数/总数 | actual_output, retrieval_context | threshold, model, penalize_ambiguous_claims, truths_extraction_limit | 回答是否忠实于检索文本（防瞎编） |
| ContextualRecallMetric | LLM judge | BaseMetric | 把 `expected_output` 拆句，判每句是否被 `retrieval_context` 支持，支持数/总数 | expected_output, retrieval_context | threshold, model, include_reason | 检索是否覆盖理想答案 |
| ContextualRelevancyMetric | LLM judge | BaseMetric | 对每段 `retrieval_context`，判其包含的语句是否与 `input` 相关，相关语句数/总数 | input, retrieval_context | threshold, model, include_reason | 检索结果与查询相关性 |
| ContextualPrecisionMetric | LLM judge | BaseMetric | 加权精确率@k：检查 `retrieval_context` 排序前 k 条中支持 `expected_output` 的密度 | input, expected_output, retrieval_context | threshold, model, include_reason | 检索排序质量评估 |

---

## 2. 内容质量（4 个）

| 指标 | 类型 | base | 评分原理 | 必读字段 | 关键参数 | AI agent 适用场景 |
|---|---|---|---|---|---|---|
| HallucinationMetric | LLM judge | BaseMetric | 对比 `actual_output` 与 `context` 每条陈述，矛盾比例越低分越高 | input, actual_output, **context（必填）** | threshold, model | Agent 输出是否基于给定上下文，防幻觉 |
| SummarizationMetric | LLM judge | BaseMetric | 用裁判生成评估问题，对比原文与摘要的覆盖度+一致性，取两项最小值 | input, actual_output | threshold, model, n, assessment_questions | Agent 摘要任务质量 |
| BiasMetric | LLM judge | BaseMetric | 抽 `actual_output` 中的观点，判每条是否含偏见，偏见比例越低分越高 | input, actual_output | threshold, model | 性别/种族/政治倾向性筛查 |
| ToxicityMetric | LLM judge | BaseMetric | 同 Bias 结构，判是否含有毒/仇恨/骚扰内容 | input, actual_output | threshold, model | 有害内容筛查 |

---

## 3. 安全合规（4 个）

| 指标 | 类型 | base | 评分原理 | 必读字段 | 关键参数 | AI agent 适用场景 |
|---|---|---|---|---|---|---|
| PIILeakageMetric | LLM judge | BaseMetric | 检测响应是否含个人身份信息（姓名/手机/身份证等），无则高分 | input, actual_output | threshold, model | 防 Agent 回复中泄露 PII |
| NonAdviceMetric | LLM judge | BaseMetric | 检测是否给出指定类型建议（金融/医疗/法律等），无则高分 | input, actual_output | **advice_types（必填，List[str]）**, threshold, model | 防 Agent 给出受监管建议 |
| MisuseMetric | LLM judge | BaseMetric | 检测是否存在 domain 相关的滥用（越权/越界） | input, actual_output | **domain（必填）**, threshold, model | 业务边界（如 Mira 仅限招聘场景） |
| RoleViolationMetric | LLM judge | BaseMetric | 二值打分：偏离指定 `role` 则 0，符合则 1 | input, actual_output | **role（必填）**, threshold, model | 单轮角色越界硬性检测 |

---

## 4. 自定义 / 框架（5 个）

| 指标 | 类型 | base | 评分原理 | 必读字段 | 关键参数 | AI agent 适用场景 |
|---|---|---|---|---|---|---|
| GEval | LLM judge（logprobs 加权） | BaseMetric | 让裁判按你给的 `criteria`/`evaluation_steps` 打 0-10 分，用 top_logprobs 概率加权融合 | 取决于 `evaluation_params` | **name + (criteria 或 evaluation_steps)** + evaluation_params, rubric, top_logprobs | 自定义任意单轮维度（专业度、不瞎编、可执行性等） |
| ArenaGEval | LLM judge（对比型） | BaseArenaMetric | 让裁判对两个 response 选 winner + reason，无数值分 | 两份输出 | name + (criteria 或 evaluation_steps) | A/B 对比两个 Agent 版本 |
| ConversationalGEval | LLM judge（logprobs 加权） | BaseConversationalMetric | 多轮版 GEval | turns | name + (criteria 或 evaluation_steps), evaluation_params=[CONTENT,ROLE] | 自定义对话维度（连贯性、问答逻辑） |
| DAGMetric | 组合型 LLM judge（DAG） | BaseMetric | 用有向无环图组合多个子判断节点，子节点用父节点输出做输入，最终综合打分 | name, **dag（DeepAcyclicGraph）** | threshold, async_mode | 复杂多步评估链（first-pass → detailed → 综合） |
| ConversationalDAGMetric | 组合型 LLM judge（DAG） | BaseConversationalMetric | 多轮版 DAG | turns | name, dag | 多轮分阶段评测（理解力→回应→整体） |

---

## 5. 非 LLM（2 个）

| 指标 | 类型 | base | 评分原理 | 必读字段 | 关键参数 | AI agent 适用场景 |
|---|---|---|---|---|---|---|
| ExactMatchMetric | 规则 | BaseMetric | 逐字符比对 `actual_output` 与 `expected_output`，完全相同 1，否则 0 | actual_output, expected_output | threshold | 结构化输出/命令结果硬校验 |
| PatternMatchMetric | 正则 | BaseMetric | `re.fullmatch(pattern, actual_output)`，匹配 1/否则 0 | actual_output | **pattern（必填）**, ignore_case, threshold | 输出格式规范（JSON 标识、日期格式等） |

---

## 6. 工具调用 / Task（6 个）

| 指标 | 类型 | base | 评分原理 | 必读字段 | 关键参数 | AI agent 适用场景 |
|---|---|---|---|---|---|---|
| ToolCorrectnessMetric | 集合比较（reason 用 LLM） | BaseMetric | 对比 `tools_called` 与 `expected_tools`（支持精确/有序/模糊），打集合相似度 | input, tools_called, expected_tools | available_tools（可选）, should_exact_match, should_consider_ordering | Agent 工具选择是否正确 |
| ToolUseMetric | LLM judge | BaseConversationalMetric | 多轮版：每个 user-assistant 交互判工具选择 + 参数正确性，取最小 | turns（含 tools_called） | **available_tools（必填）** | 多轮对话工具调用质量 |
| ArgumentCorrectnessMetric | LLM judge | BaseMetric | 逐个 tool call 判参数是否符合 `input` 语义，正确占比 | input, tools_called | evaluation_template（可选） | 工具入参对不对（无需 ground truth） |
| TaskCompletionMetric | LLM judge + Trace | BaseMetric | 从 trace 抽 task & outcome，裁判判 task 是否完成 | input, actual_output, **trace** | task（可选，默认从 trace 抽）, **requires_trace=True** | 端到端多步任务是否完成（要求本地 `@observe`） |
| PromptAlignmentMetric | LLM judge | BaseMetric | 逐条 `prompt_instructions` 检查 `actual_output` 是否遵守，符合占比 | input, actual_output | **prompt_instructions（必填非空 List[str]）** | Agent 是否遵守系统指令 |
| JsonCorrectnessMetric | Pydantic schema 校验 | BaseMetric | `expected_schema.model_validate_json(actual_output)`，二值 1/0 | input, actual_output | **expected_schema（必填，Pydantic BaseModel）** | 结构化 JSON 输出校验 |

---

## 7. 规划 / 目标（5 个）

| 指标 | 类型 | base | 评分原理 | 必读字段 | 关键参数 | AI agent 适用场景 |
|---|---|---|---|---|---|---|
| PlanAdherenceMetric | LLM judge + Trace | BaseMetric | 判 Agent 实际行动是否遵守计划 | input, actual_output, **trace** | threshold, model, **requires_trace=True** | 计划-执行一致性 |
| PlanQualityMetric | LLM judge + Trace | BaseMetric | 判 Agent 生成的计划是否完整/清晰/合理 | input, actual_output, **trace** | threshold, model, **requires_trace=True** | 计划本身的质量 |
| StepEfficiencyMetric | LLM judge + Trace | BaseMetric | 判 Agent 步骤数与资源消耗是否高效 | input, actual_output, **trace** | threshold, model, **requires_trace=True** | 是否绕弯路 / 调太多工具 |
| GoalAccuracyMetric | LLM judge | BaseConversationalMetric | 多轮综合判目标达成 + 计划得分，求平均 | turns | threshold, model | 多轮对话是否完成用户目标 |
| TopicAdherenceMetric | LLM judge | BaseConversationalMetric | 用 TP/TN/FP/FN 混淆矩阵判对话是否偏离预设主题 | turns | **relevant_topics（必填 List[str]）**, threshold, model | 多轮对话主题不跑偏 |

> ⚠️ `requires_trace=True` 这 3 个指标需要 Python 侧用 `@observe` 追踪。Mira 是 TS 后端，**不适用**——已在 `.research/metrics-catalog.md` 标注。

---

## 8. MCP（3 个）

| 指标 | 类型 | base | 评分原理 | 必读字段 | 关键参数 | AI agent 适用场景 |
|---|---|---|---|---|---|---|
| MCPUseMetric | LLM judge | BaseMetric | 判 Agent 是否正确选/用 MCP tools/resources/prompts，取使用度与参数正确性最小值 | input, actual_output, **mcp_servers**, mcp_tools_called, mcp_resources_called, mcp_prompts_called | **mcp_servers（必填）**, threshold, model | 单轮 MCP 工具调用质量 |
| MCPTaskCompletionMetric | LLM judge | BaseConversationalMetric | 多轮逐任务判 Agent 通过 MCP 完成用户目标的程度 | turns, **mcp_servers** | **mcp_servers（必填）** | 多轮 MCP 任务完成度 |
| MultiTurnMCPUseMetric | LLM judge | BaseConversationalMetric | 多轮判 MCP 工具选择正确性 + 参数准确性，求平均后取最小 | turns, **mcp_servers** | **mcp_servers（必填）** | 多轮 MCP 工具完整性 |

---

## 9. 对话 / 多轮（8 个）

| 指标 | 类型 | base | 评分原理 | 必读字段 | 关键参数 | AI agent 适用场景 |
|---|---|---|---|---|---|---|
| ConversationCompletenessMetric | LLM judge | BaseConversationalMetric | 抽用户意图，判对话是否满足所有意图 | turns（role, content） | window_size=3 | 对话是否覆盖用户所有需求 |
| TurnRelevancyMetric | LLM judge | BaseConversationalMetric | 滑动窗口判每个 assistant turn 与上下文相关性 | turns | window_size=10 | 每轮回复切不切题 |
| TurnFaithfulnessMetric | LLM judge | BaseConversationalMetric | 多轮版 Faithfulness：claims vs retrieval truths | turns, **retrieval_context** | window_size=10 | RAG 多轮回答忠实度 |
| TurnContextualPrecisionMetric | LLM judge | BaseConversationalMetric | 多轮版 ContextualPrecision | turns, retrieval_context, expected_outcome | window_size=10 | 多轮 RAG 检索排序 |
| TurnContextualRecallMetric | LLM judge | BaseConversationalMetric | 多轮版 ContextualRecall | turns, retrieval_context, expected_outcome | window_size=10 | 多轮 RAG 检索覆盖 |
| TurnContextualRelevancyMetric | LLM judge | BaseConversationalMetric | 多轮版 ContextualRelevancy | turns, retrieval_context | window_size=10 | 多轮 RAG 检索相关性 |
| RoleAdherenceMetric | LLM judge | BaseConversationalMetric | 检测 assistant 回复是否符合 `chatbot_role` 人设 | turns, **chatbot_role** | threshold, model | 多轮人设一致性 |
| KnowledgeRetentionMetric | LLM judge | BaseConversationalMetric | 逐 turn 检查 assistant 是否记住前文用户给出的信息 | turns | threshold, model | 长对话记忆力 |

---

## 10. 多模态（5 个，纯文本 Agent **不适用**）

| 指标 | 类型 | base | 评分原理 | 必读字段 | 关键参数 | AI agent 适用场景 |
|---|---|---|---|---|---|---|
| TextToImageMetric | MLLM judge | BaseMetric | 文生图：判生成图像与文本提示的一致性 + 感知质量，几何均值 | input(text), actual_output(MLLMImage) | model, threshold | 文生图 Agent |
| ImageEditingMetric | MLLM judge | BaseMetric | 判编辑后图像是否符合编辑指令 + 感知质量 | input(text/image), actual_output(MLLMImage) | model, threshold | 图像编辑 Agent |
| ImageCoherenceMetric | MLLM judge | BaseMetric | 判图文混排中图与文的逻辑连贯性 | actual_output（文本+MLLMImage 混排） | model, max_context_size | 图文报告生成 |
| ImageHelpfulnessMetric | MLLM judge | BaseMetric | 判混排中图对文本内容的帮助程度 | actual_output（混排） | model, max_context_size | 文档/报告辅助插图 |
| ImageReferenceMetric | MLLM judge | BaseMetric | 判图与周围文本的相关性与参考价值 | actual_output（混排） | model, max_context_size | 内容生成 Agent 图文关联 |

---

## 11. 本仓自建 Ops 指标（9 个 · `custom_metrics/`）

> 这 9 个**不是 DeepEval 官方指标**，是本仓为 Mira 评测套件移植自 `Mira_Validation/lib/runner/evaluators`
> 的 *operational* 信号：完成度 / DB 持久化 / token / cost / latency / 工具路径合规。
>
> - **不调 judge model**（除 `ToolDependencyMetric` 一个外）—— 全是确定性查询 + 计算
> - **数据源**：8 个走 Langfuse（trace/observations/usage/cost），1 个走 Mira Postgres `messages` 表
> - **定位方式**：靠 `test_case.metadata["conv_id"]`（= Langfuse `session_id`），由 `tests/evals/_driver.py:build_conversational` 在 driver 端塞入
>
> 共享基类 `custom_metrics._base.BaseValueMetric` 的 3 个约定（和 DeepEval 默认差别大）：
>
> 1. **`score` 是原始数值**（tokens / 美元 / 秒 / tok·s⁻¹），不是 0-1 分 —— `_NON_QUALITY_BUCKETS` 在 `report.py` 里把这些桶从"平均分"统计里排除
> 2. **`higher_is_better=False` 是默认**（cost/time/token 越少越好）；`OutputTokensPerSecMetric` 是唯一 `True`
> 3. **`informational=True`**（v2 新增）⇒ 永远 `is_successful()==True`，报告里渲染为 ℹ️ INFO，**不计入通过率分母**；用于"只采集不门禁"的指标
>
> 错误路径（Langfuse 拿不到 trace / DB 连接 fail / 缺 `conv_id`）：`_bail()` 把 `score=0` + `error=reason` + `success=False`，
> 但 informational 模式下 `is_successful()` 仍会返回 True —— 即报告里能看到 ERROR 文字但 pytest 不会挂。

### 11.1 Trace / DB 健康检查（2 个）

| 指标 | 类型 | base | 评分原理 | 数据源 | informational | 阈值 |
|---|---|---|---|---|---|---|
| CompletedMetric | 规则（Langfuse） | BaseMetric | 三段校验，全过 1 否则 0：① 最后 assistant turn 的 `content` 非空（兜底 `actual_output`，对应旧版 `outputMessage`）；② 至少 1 trace 有 `end_time` 或 `mira-agent` observation 完成；③ trace `level == "DEFAULT"`（非 ERROR/WARN） | Langfuse traces | ❌ gate | `1.0` |
| DatabaseStatusMetric | 规则（Postgres） | BaseMetric | 三段校验：① `user`↔`assistant` 严格成对（pair count == user count）；② 每条 assistant 的 `parts[-1]` 是合法终止态（`tool-complete` w/ `output.success=true` 或 `text` w/ `state=done` 或 `tool-clarify_question/confirm`）；③ 所有 `parts[*]` 中 `type=text` → `state=done`，`type=tool-*` 不在 `input-streaming`/`output-error`。`metadata.aborted=true` 跳过。`_db.py:get_conn` 做 `SELECT 1` pre-ping + 单次重连重试（防 Railway proxy 静默断 SSL） | Mira PG: `messages` 表（chat_id=conv_id） | ❌ gate | `1.0` |

> 旧版对应：`Mira_Validation/lib/runner/evaluators/item-level/completion-evaluators.ts` 的 `completedEvaluator` / `databaseStatusEvaluator`。
> **注意**：`CompletedMetric` 不读"产出文件内容"——文件注入是旧版 `gaiaEvaluator` 的事（对应本仓 `test_mira_custom.py` 的 G-Eval 路径），完成度只关心 BFF 是否回了文本。

### 11.2 Token / Cost / Latency / Throughput（5 个 INFO + 1 个 gate）

| 指标 | score | base | 数据源 | 关键算法 | informational | 当前阈值 / 方向 |
|---|---|---|---|---|---|---|
| TokensMetric | total tokens | BaseValueMetric | Langfuse `trace.usage` 求和 | `input + output` 跨 session 所有 trace 累加 | ✅ INFO | (内部默认 200k，渲染时 "—") |
| SessionCostMetric | USD | BaseValueMetric | Langfuse 优先 `trace.totalCost` → `calculatedTotalCost` → `cost` → observations 累加 | 4 段优先级回退 | ✅ INFO | (内部默认 0.5，渲染时 "—") |
| TimeToFirstTokenMetric | 秒 | BaseValueMetric | Langfuse generations 的 `timeToFirstToken` | `min(TTFT across LLM generations)` | ✅ INFO | (内部默认 5s) |
| SessionDurationMetric | 秒 | BaseValueMetric | Langfuse trace + observations 的 start/end | `max(endTime) − min(startTime)` 跨整个 session | ✅ INFO | (内部默认 60s) |
| OutputTokensPerSecMetric | tok·s⁻¹ | BaseValueMetric | usage.output / duration | `Σ output_tokens / duration_seconds`，`higher_is_better=True` | ✅ INFO | (内部默认 20，渲染时 "—") |
| NTurnsMetric | user 消息数 | BaseValueMetric | Langfuse 最后一次 `doStream` observation 的 `input.messages` | 统计 `role=='user'` 数 | ❌ gate | **`≤30`**（防 runaway 对话） |

> 旧版对应：`Mira_Validation/lib/runner/evaluators/item-level/{token,time}-evaluators.ts`。
> 这 5 个 INFO 指标当前**只采集不门禁**——`TokensMetric` 之前定 500k 阈值，CRM 类用例普遍 1M+ 都会假阳；改 INFO 后用作趋势监控（看 token 涨没涨、cost 飘没飘）。
> 想把任意一个重新变 gate：去掉 `informational=True` 并指定真实 `threshold=` 即可（见 `tests/evals/test_mira_ops.py:45`）。

### 11.3 工具调用路径（1 个）

| 指标 | 类型 | base | 评分原理 | 数据源 | 关键参数 | informational |
|---|---|---|---|---|---|---|
| ToolDependencyMetric | **LLM judge**（claude CLI） | BaseMetric | 1. 从 Langfuse 最后一次 `doStream` observation 抽 `role=assistant` 的 `tool-call` 步骤 (`{step, toolName, argsHint}`)；2. 加载约束 catalog（默认 `custom_metrics/data/tool-dependencies.json`，移植自 `Mira_Validation`）；3. 给 claude CLI 喂 prompt：约束列表 + 实际工具序列 + question，要求输出 `violations[]`；4. 计分 `score = (totalSteps − violatingSteps) / totalSteps`，violatingSteps 是 violations 里 `where` 字段提到的 step 去重集合。**0 tool calls 或 0 violations → 1.0**（lenient） | Langfuse observations + `tool-dependencies.json` | `constraints_path=`, `threshold` | ❌ gate（`≥0.80`） |

> 旧版对应：`Mira_Validation/lib/runner/evaluators/item-level/dependency-evaluators.ts` 的 `toolDependencyEvaluator`。
> 评分范围 0-1（旧版是 0-100，本仓归一化）。一次 `measure()` 约 30-60s（claude CLI 调用），与本仓其它 LLM judge 共用 process-wide lock 串行执行。

### Ops 9 件套小结

| 项目 | 数 | 走 Langfuse | 走 Postgres | LLM judge | informational 默认 |
|---|---:|:-:|:-:|:-:|:-:|
| `status_metrics.CompletedMetric` | 1 | ✓ | | | gate |
| `status_metrics.DatabaseStatusMetric` | 1 | | ✓ | | gate |
| `usage_metrics.{Tokens,SessionCost}Metric` | 2 | ✓ | | | **INFO** |
| `perf_metrics.{TimeToFirstToken,SessionDuration,OutputTokensPerSec}Metric` | 3 | ✓ | | | **INFO** |
| `perf_metrics.NTurnsMetric` | 1 | ✓ | | | gate |
| `path_metrics.ToolDependencyMetric` | 1 | ✓ | | ✓ | gate |

报告渲染（`report.py`）：
- **gate 类**：✅ PASS / ❌ FAIL / 🚨 ERROR / · NONE，计入通过率分母
- **INFO 类**：ℹ INFO，**不**计入通过率，数值集中显示在每条用例的"仅记录的指标"折叠块

---

## 给 Mira / 你这个项目的选型建议

> 你已经在 `tests/evals/test_mira_*.py` 用了 17 个，下面用 ✅/⚠️/❌ 标当前可用度。

### ✅ 直接可用（已在 Mira 项目中验证）
- **答非所问筛查**：`AnswerRelevancyMetric`, `TurnRelevancyMetric`
- **工具调用质量**：`ToolCorrectnessMetric`, `ArgumentCorrectnessMetric`, `ToolUseMetric`
- **多轮对话流**：`ConversationCompletenessMetric`, `RoleAdherenceMetric`, `KnowledgeRetentionMetric`, `GoalAccuracyMetric`, `TopicAdherenceMetric`
- **安全合规**：`BiasMetric`, `ToxicityMetric`, `PIILeakageMetric`, `RoleViolationMetric`
- **自定义维度**：`GEval`, `ConversationalGEval`（已在 `test_mira_custom.py`）
- **指令遵守**：`PromptAlignmentMetric`

### ⚠️ 需要补 ground truth / 上下文才能用
- `FaithfulnessMetric`, `ContextualRecall/Relevancy/Precision` 系列（要 `retrieval_context`，目前 Mira 没暴露检索结果）
- `Turn*Contextual*`/`TurnFaithfulness`（同上）
- `HallucinationMetric`（要 `context` 真理集）
- `SummarizationMetric`（要长输入文档，Mira 现有 18 条 golden 都不是摘要任务）
- `NonAdviceMetric`（要列 `advice_types`，业务上 Mira 不太碰金融/医疗，可跳过）
- `MisuseMetric`（要 `domain`，可加 `domain="hiring assistant"`）
- `ExactMatch` / `PatternMatch` / `JsonCorrectness`（Mira 输出是自由文本，没固定 schema）

### ❌ 当前不适用
- **`requires_trace=True` 三剑客**：`TaskCompletionMetric`, `PlanAdherenceMetric`, `PlanQualityMetric`, `StepEfficiencyMetric` —— 要本地 `@observe` 追踪，Mira 是 TS 后端，Python 这边没 trace（除非你像 `test_smoke_hello_observed.py` 那样自己手工搭 span 树）
- **MCP 三剑客**：`MCPUseMetric`, `MCPTaskCompletionMetric`, `MultiTurnMCPUseMetric` —— 需要把 Mira 工具调用解析成 `MCPToolCall`，目前 SSE 解析层没做这层映射
- **多模态 5 个**：Mira 纯文本，不适用
- **`ArenaGEval`**：要两个 Agent 对比，单 Mira 跑不需要
- **`DAGMetric` / `ConversationalDAGMetric`**：高阶组合，普通 metric 够用前不必上

---

## 速查口诀

```
要看 "回答对不对"      → AnswerRelevancy + GEval(自定)
要看 "调工具对不对"     → ToolCorrectness(集合) + ArgumentCorrectness(参数) + ToolUse(多轮)
要看 "RAG 检索好不好"   → Faithfulness + Contextual{Recall,Precision,Relevancy}
要看 "多轮整体好不好"   → ConversationCompleteness + RoleAdherence + GoalAccuracy + KnowledgeRetention
要看 "安不安全"         → Bias + Toxicity + PIILeakage + RoleViolation
要看 "计划/步骤好不好"  → 需 @observe Trace：Plan*/Step*/TaskCompletion
要 A/B 比较两个版本     → ArenaGEval
要任意自定义维度        → GEval / ConversationalGEval / DAGMetric
```

---

## 关于 judge model 的强提醒

⚠️ **所有 LLM judge 类型指标在构造时如果不显式传 `model=judge`，会默认实例化 OpenAI GPT-4 客户端**（读 `OPENAI_API_KEY`）。

本项目约定一切走 `ClaudeCliJudge`（本地 `claude` CLI），凡是 metric 构造签名里有 `model:` 参数，**都必须显式传**：

```python
from claude_cli_judge import ClaudeCliJudge
judge = ClaudeCliJudge()

AnswerRelevancyMetric(threshold=0.5, model=judge, async_mode=False)
ToolCorrectnessMetric(threshold=0.5, model=judge, async_mode=False)
# ...
```

判断"这个 metric 是否调 LLM"：看上面 `类型` 列。`规则 / 正则 / 集合比较 / Schema 校验` 这几类不调（但构造时仍会拉一个 model 客户端做 reason 生成兜底，保险起见同样显式传 `model=judge`）。
