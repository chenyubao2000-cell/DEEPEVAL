# Mira eval data assets

| File | What's in it | Edited by |
|---|---|---|
| `goldens.json` | The customer goldens used for every evaluation run | Humans (or `mira-eval-bootstrap` for `_expected_tools`) |
| `tool_dependencies.json` | Constraint catalog the `ToolDependencyMetric` judges against | Humans (porting from Mira_Validation) |

## `goldens.json` schema

```jsonc
{
  "goldens": [
    {
      "scenario": "短描述（也用作 ID 前 60 字符）",
      "user_inputs": ["第一轮 user 消息", "第二轮 user 消息", ...],
      "expected_outcome": "判官参照的预期",
      "chatbot_role": "Mira 角色描述（RoleAdherenceMetric 用）",

      // Optional category & tier — drive _CATEGORY_DEFAULT_SKIPS and tier filter
      "_category": "crm" | "voice" | "ci_email" | "ci_dingding" | null,
      "_tier": "light" | "heavy" | null,

      // Optional: bootstrap_expected_tools 写进来的工具调用参考路径
      "_expected_tools": [
        {"tool_name": "...", "input_parameters": {...}},
        ...
      ],

      // Optional: 接受多条合法路径之一就算 PASS
      "_acceptable_paths": ["路径 A 描述", "路径 B 描述"],

      // Optional: 跳过 / 追加跳过 指标
      "_skip_metrics": ["BiasMetric", "GEval/ExpectedToolPath"],
      "_skip_metrics_extra": [...],

      // Optional: 文件附件（绝对路径或 data/-relative）
      "_attachments": ["/abs/path/to/upload.xlsx"]
    }
  ]
}
```

## `tool_dependencies.json` schema

Free-form constraint list consumed verbatim by `ToolDependencyMetric`'s
judge prompt. Each constraint is a sentence describing a forbidden / required
tool sequence. The judge returns `{ violations: [...] }`; the metric scores
``(totalSteps - violatingSteps) / totalSteps``.

Update when:

- A new Mira tool ships with sequencing rules (e.g. "must call X before Y").
- Mira_Validation's own constraint catalog evolves — keep parity by hand.
