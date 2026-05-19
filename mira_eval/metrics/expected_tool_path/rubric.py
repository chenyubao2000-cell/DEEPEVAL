"""ExpectedToolPath rubric — ConversationalGEval driven by metadata.expected_tools.

History (2026-05-15): replaced the old ToolUseMetric → LeanToolUseMetric
chain with a single ConversationalGEval. The reference path is bootstrapped
once by ``cli/bootstrap.py`` and lives in ``data/goldens.json`` under
``_expected_tools``; the driver copies it onto ``metadata.expected_tools``
so the judge sees it via ``MultiTurnParams.METADATA``.
"""
from __future__ import annotations

from deepeval.metrics import ConversationalGEval
from deepeval.test_case import MultiTurnParams

from ...models import ClaudeCliJudge


_JUDGE = ClaudeCliJudge()


EXPECTED_TOOL_PATH_METRIC = ConversationalGEval(
    name="ExpectedToolPath",
    criteria=(
        "评估 agent 实际调用的工具序列是否达成用户目标。"
        "参考标准答案 metadata['expected_tools']（已经过 bootstrap 生成 + 人工 review 的理想路径）"
        "与 agent 实际在各 turn 中执行的 TOOLS_CALLED 做语义对比。\n"
        "\n"
        "宽容规则（不扣分）：\n"
        "  1. 工具名不同但功能等价（例如用 search 替代 company_search 拿到相同信息；"
        "用 sb_file_create 替代 sb_docx_create 实现同等交付）\n"
        "  2. input_parameters 措辞不同但语义相同（例如 query='上海AI' 和 query='上海人工智能'）\n"
        "  3. 调用了额外辅助工具（write_todos、view_skill、clarify_question 等）\n"
        "  4. 工具调用顺序与 expected 不一致\n"
        "  5. expected 列了多个工具，实际只用了少数但功能上等价完成了目标\n"
        "  6. 实际工具调用总数与 expected 的步数不一致 —— expected 的 3-8 步只是"
        "bootstrap 的参考量级，实际多于或少于都可以接受，重点是完成了功能而非匹配步数\n"
        "  7. 同一个工具被多次重复调用（尤其 search、company_search、people_search "
        "这类检索工具）默认算正确 —— 不同 input_parameters 通常是 agent 在做"
        "多维度补充查询（如先按行业再按城市），是合理行为而非冗余\n"
        "\n"
        "扣分维度：\n"
        "  - 漏掉 expected 中明确的关键功能步骤（如 expected 要求生成 Excel 但实际未生成）\n"
        "  - 调用了与目标无关的工具且偏离主线\n"
        "  - 关键工具的 input_parameters 跟用户目标完全不匹配"
        "（如用户问上海但 query 写成了北京）\n"
        "  - assistant 显式声明无法访问数据 / 没调用任何工具就 complete\n"
        "\n"
        "评分（请按 0-10 整数返回，后端会映射到 0-1）：\n"
        "  10：全部关键功能目标达成；\n"
        "  7-9：少量瑕疵但不影响交付；\n"
        "  4-6：漏 1-2 个关键步骤；\n"
        "  0-3：完全跑偏或没有有效工具调用。"
    ),
    evaluation_params=[
        MultiTurnParams.TOOLS_CALLED,
        MultiTurnParams.METADATA,    # expected_tools lives here
        MultiTurnParams.SCENARIO,
    ],
    model=_JUDGE,
    threshold=0.7,
    async_mode=False,
)
