# DeepEval × Mira

针对 [Mira](https://github.com/) AI 招聘助手 BFF 的端到端评测套件，基于
[DeepEval](https://github.com/confident-ai/deepeval) + 本地 `claude` CLI 作裁判模型。

特性：

- ✅ 一份数据集（`tests/evals/.dataset.json`）驱动 17 个指标 × 多个用例的横向评测
- ✅ 支持 **HITL 自动放过**（`confirm` / `clarify_question` 这类阻塞 tool）
- ✅ 支持 **文件附件**（xlsx / pdf / png 等通过 R2 上传 → 注入 `[Uploaded File: …]` marker）
- ✅ 输出 Markdown / JSON / HTML 三种报告
- ✅ 一行命令生成两环境**横向对比** HTML（Δ 5 档自动上色）

---

## 1. 拿到代码

```bash
git clone git@github.com:chenyubao2000-cell/DEEPEVAL.git
cd DEEPEVAL
```

## 2. Python 环境

需要 **Python ≥ 3.11**。建议用项目内 venv：

```bash
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install \
  deepeval==4.0.0 \
  httpx==0.28.1 \
  python-dotenv==1.2.2 \
  pydantic==2.13.4 \
  pytest==9.0.3 \
  markdown-it-py==4.2.0
```

> 之后所有命令都用 `.venv/bin/python …`（避免污染系统 Python）。

## 3. 安装 `claude` CLI

裁判模型走的是本地 Claude Code CLI（**不需要单独配 `ANTHROPIC_API_KEY`**）：

```bash
# macOS
brew install claude

# 或参考 Anthropic 官方安装文档
# 装完后必须能在 shell 里跑通：
claude -p "Reply with exactly: OK"   # 应输出 "OK"
```

CLI 必须已经登录（首次运行会引导浏览器登录）。

## 4. 配置 `.env`（不入仓）

仓库根目录创建 `.env`：

```ini
# Mira BFF
MIRA_BFF_URL=https://mira-bff-preview.up.railway.app   # 或 https://mina.ciwork.cn
MIRA_SESSION_TOKEN=<your-better-auth-session-token>    # URL-encoded form
# 默认 cookie 名是 __Secure-better-auth.session_token，自定义时再设：
# MIRA_COOKIE_NAME=__Secure-better-auth.session_token

# 可选：Mira 自己写 trace 的 Langfuse（评测引擎本身不读）
# LANGFUSE_BASEURL=https://us.cloud.langfuse.com
# LANGFUSE_PUBLIC_KEY=...
# LANGFUSE_SECRET_KEY=...
```

**怎么拿 `MIRA_SESSION_TOKEN`：**

1. 浏览器登录 Mira 前端（如 `https://mina.ciwork.cn` 或 `https://mira-bff-preview.up.railway.app`）
2. DevTools → Application → Cookies → 找 `__Secure-better-auth.session_token`
3. 复制 **Value**（URL-encoded 形态原样，例如 `xxx.yyy%2Fzzz%3D`）粘进 `.env`

> ⚠ token 通常 7 天过期，过期后会看到 `http_401: UNAUTHORIZED`，按上面流程重取一次即可。

## 5. 握手验证

```bash
.venv/bin/python -c "from mira_client import MiraSession; s=MiraSession(); print(s.send('请用一句话回答：你是谁？')[:200]); print('warnings:', s.warnings)"
```

成功应该看到一行 Mira 的自我介绍，`warnings: []`。

---

## 6. 跑评测

### 6.1 一键完整报告（推荐）

```bash
# 默认跑 4 个新类目（crm / voice / ci_email / ci_dingding）共 8 条 golden × 17 指标
.venv/bin/python report.py

# 只跑 voice 2 条
.venv/bin/python report.py --category voice

# 跑全部 18 条 goldens
.venv/bin/python report.py --all

# 按 tier / index / scenario 子串过滤
.venv/bin/python report.py --tier light
.venv/bin/python report.py --index 12,13
.venv/bin/python report.py --scenario 钉钉

# 自定义输出位置
.venv/bin/python report.py --category voice --out reports/voice-$(date +%Y%m%d-%H%M)
```

跑完会在 `reports/` 下生成：

- `<name>.md`  — Markdown 报告（含总评 / 按类别 / 按用例 / 按指标 / 每用例详情 + 非 PASS 项 reason 折叠块）
- `<name>.json` — 机器可读完整结果（score / threshold / reason / tool_calls / conv_id / warnings 全留痕）

### 6.2 转 HTML（带样式）

```bash
.venv/bin/python md_to_html.py reports/<name>.md
# 生成 reports/<name>.html — 浅深双主题、表头 sticky、PASS/FAIL/ERR 上色 pill
```

### 6.3 两环境横向对比 HTML

```bash
.venv/bin/python compare_reports.py \
  --left  reports/voice-A.json --left-label  "环境 A 短名" \
  --right reports/voice-B.json --right-label "环境 B 短名" \
  -o reports/compare-AB.html
```

输出：每条 golden 一个 section，逐指标左右并列，Δ 列按 |Δ| < 0.10 / 0.10-0.30 / ≥ 0.30 上色，分别标识噪声 / 轻微漂移 / 显著进步或退步。

### 6.4 pytest 标准 CI 流（按 metric 文件分套件）

```bash
# 5 个 metric 文件，按 concern 分类
.venv/bin/python -m pytest tests/evals/test_mira_e2e.py -v
.venv/bin/python -m pytest tests/evals/test_mira_custom.py -v
.venv/bin/python -m pytest tests/evals/test_mira_tooluse.py -v
.venv/bin/python -m pytest tests/evals/test_mira_safety.py -v
.venv/bin/python -m pytest tests/evals/test_mira_others.py -v

# 按 tier 过滤
MIRA_GOLDEN_TIER=light .venv/bin/python -m pytest tests/evals -v

# 只跑某条 golden（按 scenario 子串）
.venv/bin/python -m pytest tests/evals/test_mira_e2e.py -k "Voice 场景 2" -v
```

### 6.5 单条 golden × 全部 17 指标的快速体检

```bash
.venv/bin/python healthcheck.py --golden 13       # 按 index
.venv/bin/python healthcheck.py --golden "钉钉"   # 按 scenario 子串
```

---

## 7. 项目结构

```
DEEPEVAL/
├── mira_client.py              # SSE 客户端 + 文件上传 + HITL auto-approve
├── claude_cli_judge.py         # 本地 claude CLI 包装成 DeepEval judge
├── report.py                   # 多 golden × 17 指标主入口
├── compare_reports.py          # 两份 JSON → 横向对比 HTML
├── md_to_html.py               # 单份 Markdown → 样式化 HTML
├── healthcheck.py              # 单 golden 全指标体检
└── tests/evals/
    ├── .dataset.json           # 18 条 customer goldens
    ├── _driver.py              # 共享 driver
    ├── test_mira_e2e.py        # 5 个多轮内置 metric
    ├── test_mira_custom.py     # 3 个多轮 ConversationalGEval
    ├── test_mira_tooluse.py    # 2 多轮 + 1 单轮工具相关 metric
    ├── test_mira_safety.py     # 4 个单轮安全 metric
    └── test_mira_others.py     # 2 个其它单轮 metric
```

### 涉及的 17 个指标

| 文件 | 指标 |
|---|---|
| e2e | ConversationCompleteness, TurnRelevancy, KnowledgeRetention, RoleAdherence, GoalAccuracy |
| custom (ConversationalGEval) | ProfessionalNoFabrication, DeliverableMatchesRequest, GroundedNoFabrication |
| tooluse | ToolUse, TopicAdherence, ArgumentCorrectness |
| safety | Bias, Toxicity, PIILeakage, RoleViolation |
| others | AnswerRelevancy, PromptAlignment |

---

## 8. 关键运维注意事项

1. **判官 quota**：判官走本地 `claude` CLI，会消耗你的 Claude Code 配额。8 条 golden × 17 指标 ≈ 136 次 judge 调用 × ~30s，跑久了会撞日额度（错误形如 `claude CLI rc=1; stdout="You've hit your limit · resets 8pm (Asia/Shanghai)"`）。重置后继续跑就行。

2. **判官串行**：`claude_cli_judge.py` 用进程级锁保证 CLI 串行执行（Claude Code 的 `~/.claude` 共享状态对并发不友好）。所以并行加速对 judge 阶段无效。

3. **HITL 自动放过**：`mira_client.py` 自动识别 `confirm` / `clarify_question` 这类阻塞 tool 并按 default 值放过，最多 `MAX_APPROVAL_ROUNDS=8`（可用 `MIRA_MAX_APPROVAL_ROUNDS` 环境变量覆盖）。如果你要测「拒绝」路径，调用 `session.send(..., auto_approve=False)`。

4. **文件附件**：在 golden 里加 `"_attachments": ["/abs/path/file.xlsx"]`，driver 会自动用 R2 签名 URL 上传，并把 `[Uploaded File: /mnt/task/upload/<name>|<size>|<r2Key>]` marker 注入到第一条 user message 里——和前端 `task-input.tsx` 行为完全一致。

5. **Mira 非确定性**：同一条 prompt 多次跑可能走不同 agent 路径（少调或多调几个 tool、HITL 轮数变化），分数会抖动。重要结论必须**至少跑 2-3 次取均值**才算可信。

6. **`.gitignore` 必备项**：
   ```
   .venv/
   __pycache__/
   *.pyc
   .deepeval-cache/
   .deepeval_telemetry.txt
   .env                  # 包含 session token 严禁入仓
   reports/              # 生成产物按需保留
   /tmp/claude_judge_debug/
   ```

---

## 9. 常见错误

| 现象 | 根因 | 处理 |
|---|---|---|
| `http_401: UNAUTHORIZED` | session token 过期 / cookie 跨域 | 重取 token，确认 `MIRA_BFF_URL` 跟登录的前端**同域** |
| `http_404: <!DOCTYPE html>` | `MIRA_BFF_URL` 多了 `/task` 或别的尾缀 | `MIRA_BFF_URL` 必须是 **host**，不能含 `/api/...` 或 `/task` |
| `claude CLI rc=1; stdout="You've hit your limit"` | Judge quota 用尽 | 等 quota 重置（每日定时） |
| Voice 场景类指标狂 FAIL | 没处理 HITL gate（旧 driver bug） | 已修；如果再出，看 `session.warnings` 是否有 `auto_approve round …` |
| `TopicAdherence` 0.00 false-negative | `tests/evals/test_mira_tooluse.py:RELEVANT_TOPICS` 没覆盖你的 category | 加一条对应描述进去 |
| `OSError: /mnt/task/output 不存在` 在 judge reason 里 | Mira 沙箱基础设施 bug，**不是评测问题** | 真实产品缺陷，给 Mira 团队报 |

---

## 10. 反馈

- 评测系统本身的 bug / 改进 → 直接在这个 repo 开 issue 或 PR
- Mira 产品质量问题（FAIL 项的 reason 指向 Mira 行为）→ 把 `conv_id`（在 `reports/*.json` 的 `results[].conv_id`）交给 Mira 团队，他们能从 Langfuse 拉到完整 trace
