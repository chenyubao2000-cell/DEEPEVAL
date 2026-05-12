# DeepEval 非 RAG 指标全量清单（针对 Mira，deepeval 4.0.0）

> 验证来源：官方 docs/metrics-introduction 侧栏 + 本地 `.venv/.../deepeval/metrics/` 源码逐文件读签名。
> 所有 import 路径都已用 `__init__.py` 的 re-export 验证。
> Base = `BaseMetric` → 单轮 `LLMTestCase`；`BaseConversationalMetric` → 多轮 `ConversationalTestCase(turns=[Turn(role, content, tools_called=..., scenario=...)])`。

## Agents（侧栏 6 个 + MCP 3 个）

| Metric | 类名 / import | 关键签名（必填在前） | base | LLMTestCase / Turn 必读字段 | tracing? | 对 Mira |
|---|---|---|---|---|---|---|
| Task Completion | `from deepeval.metrics import TaskCompletionMetric` | `(threshold=0.5, task=None, model, include_reason, async_mode, strict_mode, verbose_mode)` | BaseMetric | `input`, `actual_output`, `tools_called`；**`requires_trace=True`**，读 `test_case._trace_dict` | **强依赖** | **不适用**：必须 `@observe` 装饰本侧 LLM 调用；Mira 是 TS 后端，Python 这边没有 trace |
| Tool Correctness | `from deepeval.metrics import ToolCorrectnessMetric` | `(available_tools=None, threshold=0.5, evaluation_params=[], model, ..., should_exact_match=False, should_consider_ordering=False)` | BaseMetric | `input`, `tools_called`, `expected_tools`（`available_tools` 可选） | 否（纯规则，不调 judge） | **可直接用**：从 SSE `tool-input-available` 解析 `ToolCall(name, input_parameters, output)` → `tools_called`；测例里写 `expected_tools` |
| Argument Correctness | `from deepeval.metrics import ArgumentCorrectnessMetric` | `(threshold=0.5, model, include_reason, async_mode, strict_mode, verbose_mode, evaluation_template=ArgumentCorrectnessTemplate)` | BaseMetric | `input`, `tools_called` | 否，judge LLM | **可直接用**：判断 SSE 抓到的工具入参对不对，不需要 ground truth |
| Tool Use | `from deepeval.metrics import ToolUseMetric` | `(available_tools: List[ToolCall], threshold=0.5, model, ...)` **available_tools 必填** | BaseConversationalMetric | `turns[*].role/content`；内部走 `get_unit_interactions(test_case.turns)`，每个 Turn 的 `tools_called` 会被读 | 否 | **可直接用（多轮）**：每个 assistant Turn 附 `tools_called`；要传 Mira 全工具清单作 `available_tools` |
| Plan Adherence | `from deepeval.metrics import PlanAdherenceMetric` | `(threshold=0.5, model, ...)` | BaseMetric | `input`, `actual_output`；**`requires_trace=True`**，读 `_trace_dict` | **强依赖** | **不适用**（同 Task Completion） |
| Plan Quality | `from deepeval.metrics import PlanQualityMetric` | 同上 | BaseMetric | 同上 + trace | **强依赖** | **不适用** |
| Step Efficiency | `from deepeval.metrics import StepEfficiencyMetric` | `(threshold=0.5, model, ...)` | BaseMetric | `input`, `actual_output` + trace | **强依赖** | **不适用** |
| Goal Accuracy | `from deepeval.metrics import GoalAccuracyMetric` | `(threshold=0.5, model, ...)` | BaseConversationalMetric | `turns[*].role/content`，`get_unit_interactions` | 否 | **可直接用（多轮）**：评 Mira 是否完成用户目标 |
| MCP Use (single) | `from deepeval.metrics import MCPUseMetric` | `(threshold=0.5, model, ...)` | BaseMetric | `input`, `actual_output`, **`mcp_servers`**；读 `mcp_tools_called/mcp_resources_called/mcp_prompts_called` | 否 | **需扩 SSE 解析**：若把 Mira MCP 调用解析成 `MCPToolCall`，可用；否则用 ToolCorrectness 即可 |
| MCP Task Completion | `from deepeval.metrics import MCPTaskCompletionMetric` | `(...)` | BaseConversationalMetric | `turns`, `mcp_servers` | 否 | 同上（多轮版） |
| Multi-Turn MCP Use | `from deepeval.metrics import MultiTurnMCPUseMetric` | `(...)` | BaseConversationalMetric | `turns`, `mcp_servers` | 否 | 同上 |

## Chatbots / Multi-Turn

| Metric | 类名 / import | 签名 | base | Turn 必读字段 | 对 Mira |
|---|---|---|---|---|---|
| Conversation Completeness | `from deepeval.metrics import ConversationCompletenessMetric` | `(threshold=0.5, model, ..., window_size=3)` | BaseConversationalMetric | `turns[*].role/content` | **可直接用** |
| Turn Relevancy | `from deepeval.metrics import TurnRelevancyMetric` | `(threshold=0.5, model, ..., window_size=10)` | BaseConversationalMetric | `turns[*].role/content` | **可直接用** |
| Role Adherence | `from deepeval.metrics import RoleAdherenceMetric` | `(threshold=0.5, model, ...)` | BaseConversationalMetric | `turns` + **`chatbot_role`**（ConversationalTestCase 字段） | **可直接用** |
| Knowledge Retention | `from deepeval.metrics import KnowledgeRetentionMetric` | `(threshold=0.5, model, ...)` | BaseConversationalMetric | `turns` | **可直接用** |
| Topic Adherence | `from deepeval.metrics import TopicAdherenceMetric` | `(relevant_topics: List[str], threshold=0.5, model, ...)` **必填 relevant_topics** | BaseConversationalMetric | `turns` | **可直接用** |

## Safety

所有 Safety 指标都是 `BaseMetric`（单轮 `LLMTestCase`），读 `input` + `actual_output`，不需要 trace 或 ground truth。多轮 Mira 用法：拆每个 assistant turn 成单轮 case。

| Metric | 类名 / import | 签名 | 备注 |
|---|---|---|---|
| Bias | `from deepeval.metrics import BiasMetric` | `(threshold=0.5, model, ..., evaluation_template=BiasTemplate)` | 可直接用 |
| Toxicity | `from deepeval.metrics import ToxicityMetric` | 同上 | 可直接用 |
| PII Leakage | `from deepeval.metrics import PIILeakageMetric` | 同上 | 可直接用 |
| Misuse | `from deepeval.metrics import MisuseMetric` | `(domain: str, threshold=0.5, model, ...)` **domain 必填** | 设 domain="general AI assistant" |
| Non-Advice | `from deepeval.metrics import NonAdviceMetric` | `(advice_types: List[str], threshold=0.5, model, ...)` **必填**，例 `["financial","medical","legal"]` | 看业务 |
| Role Violation | `from deepeval.metrics import RoleViolationMetric` | `(threshold=0.5, role: str, model, ...)` **role 必填** | 与 RoleAdherence 区别：这个单轮、规则=避免越界 |

## Custom Metrics

| Metric | 类名 / import | 签名 | base |
|---|---|---|---|
| GEval | `from deepeval.metrics import GEval` | `(name, evaluation_params: List[LLMTestCaseParams], criteria=None, evaluation_steps=None, rubric=None, model, threshold=0.5, top_logprobs=20, async_mode, strict_mode, verbose_mode, evaluation_template=GEvalTemplate)` | BaseMetric |
| DAG | `from deepeval.metrics import DAGMetric` | `(name, dag: DeepAcyclicGraph, model, threshold=0.5, ...)` | BaseMetric |
| Conversational GEval | `from deepeval.metrics import ConversationalGEval` | `(name, evaluation_params: List[MultiTurnParams]=None, criteria=None, evaluation_steps=None, model, threshold=0.5, top_logprobs=20, rubric=None, ...)` | BaseConversationalMetric |
| Conversational DAG | `from deepeval.metrics import ConversationalDAGMetric` | `(name, dag, model, threshold=0.5, ...)` | BaseConversationalMetric |
| Arena GEval | `from deepeval.metrics import ArenaGEval` | `(name, evaluation_params, criteria=None, evaluation_steps=None, model, async_mode, verbose_mode)` | `BaseArenaMetric`（用 `ArenaTestCase`） |

## Image / Multimodal

`ImageCoherenceMetric`/`ImageHelpfulnessMetric`/`ImageReferenceMetric`/`TextToImageMetric`/`ImageEditingMetric` 都来自 `from deepeval.metrics import ...`。
需要 `MLLMTestCase`。**仅特定场景**：Mira 当前 evals 跳过。

## Others

| Metric | 类名 / import | 签名 | 必填字段 | 对 Mira |
|---|---|---|---|---|
| Hallucination | `from deepeval.metrics import HallucinationMetric` | `(threshold=0.5, model, ..., evaluation_template=HallucinationTemplate)` | `input`, `actual_output`, **`context: List[str]`** | **需 GT**：暂缓 |
| Summarization | `from deepeval.metrics import SummarizationMetric` | `(threshold=0.5, n=5, model, assessment_questions=None, ...)` | `input`(原文), `actual_output`(摘要) | **仅摘要场景** |
| Json Correctness | `from deepeval.metrics import JsonCorrectnessMetric` | `(expected_schema: BaseModel, model, threshold=0.5, ..., strict_mode=True)` | `input`, `actual_output` | **仅结构化输出** |
| Answer Relevancy | `from deepeval.metrics import AnswerRelevancyMetric` | `(threshold=0.5, model, ...)` | `input`, `actual_output` | 可用（单轮版） |
| Prompt Alignment | `from deepeval.metrics import PromptAlignmentMetric` | `(prompt_instructions: List[str], threshold=0.5, ...)` | `input`, `actual_output` | **可用**：把 Mira system prompt 拆条 |
| Exact Match | `from deepeval.metrics import ExactMatchMetric` | `(threshold=1.0, verbose_mode=False, ...)` | `actual_output`, `expected_output` | 需 GT |
| Pattern Match | `from deepeval.metrics import PatternMatchMetric` | `(pattern: str, ignore_case=False, verbose_mode=False, threshold=1.0)` | `actual_output` | 规则匹配，可用 |

---

## 推荐给开发员的 metric 阵列（按文件分）

### `test_mira_e2e.py`（多轮，每文件 ≤5 个）
1. ConversationCompletenessMetric
2. TurnRelevancyMetric
3. KnowledgeRetentionMetric
4. RoleAdherenceMetric（chatbot_role 必填）
5. GoalAccuracyMetric

### `test_mira_custom.py`（多轮自定义）
1. ConversationalGEval "ProfessionalNoFabrication"
2. ConversationalGEval "DeliverableMatchesRequest"
3. ConversationalGEval "GroundedNoFabrication"

### `test_mira_tooluse.py`（混合）
1. ToolUseMetric(available_tools=[...]) —— 多轮
2. ArgumentCorrectnessMetric() —— 单轮，judge 入参合理性
3. TopicAdherenceMetric(relevant_topics=[...]) —— 多轮
4. （可选）ToolCorrectnessMetric() —— 单轮，需 expected_tools

### `test_mira_safety.py`（单轮 per assistant turn）
1. BiasMetric
2. ToxicityMetric
3. PIILeakageMetric
4. RoleViolationMetric(role="...")

### `test_mira_others.py`
- AnswerRelevancyMetric() 单轮快测
- PromptAlignmentMetric(prompt_instructions=[...]) —— 把 Mira system prompt 拆条
- PatternMatchMetric 工具输出格式校验

### 明确放弃
- TaskCompletion / PlanAdherence / PlanQuality / StepEfficiency（trace 强依赖）
- RAG 全系（Faithfulness、Contextual*、TurnContextual*、TurnFaithfulness、Ragas）
- Image/Multimodal 全系
- Hallucination / Summarization / ExactMatch / JsonCorrectness（GT 依赖，初期暂缓）
