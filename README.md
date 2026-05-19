# DeepEval × Mira

针对 [Mira](https://github.com/) AI 招聘助手 BFF 的端到端评测套件，基于
[DeepEval](https://github.com/confident-ai/deepeval) + 本地 `claude` CLI 作裁判模型。

特性：

- ✅ 一份数据集（`data/goldens.json`）驱动 11 个指标 × 多个用例的横向评测
- ✅ **多环境**（`--env preview|staging|prod`），各自的 BFF / Langfuse / token 隔离配置
- ✅ **工具清单从 Langfuse 真实 trace 抓取**并缓存（不再写死),首条 golden 后自动 union 更新
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
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

`pip install -e .` 会按 `pyproject.toml` 装好所有依赖，并把以下命令注册到 `.venv/bin/`：

| 命令 | 作用 |
|---|---|
| `mira-eval` | 顶层 dispatcher (`mira-eval run`/`healthcheck`/`compare` 等子命令) |
| `mira-eval-run` | 多 golden × N 指标主入口 |
| `mira-eval-healthcheck` | 单 golden 全指标体检 |
| `mira-eval-compare` | 两份 JSON → 对比 HTML |
| `mira-eval-html` | Markdown → 样式化 HTML |
| `mira-eval-replay` | 不重调 judge 刷历史 JSON |
| `mira-eval-bootstrap` | 给 golden 提名 `_expected_tools` |

> 没装 console_scripts 也行：`.venv/bin/python -m mira_eval.cli.run ...` 等价于 `mira-eval-run ...`。

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

## 4. 配置环境 `.env.<name>`

每个 Mira 部署（preview / staging / prod / …）有自己的 BFF、token、Langfuse 项目，
我们用 `.env.<name>` 一个文件一份环境。默认环境名为 `preview`。

仓库根目录创建 **`.env.preview`**（v0.2 之前的单文件 `.env` 已废弃；
`config.py` 仍保留 `.env` 作为兜底 fallback，但**不建议再用** —— 早期曾因
`.env` 静默盖过 `.env.<name>` 导致跨环境串值，见 `config.py:23` 的注释）：

```ini
# Mira BFF
MIRA_BFF_URL=https://mira-bff-preview.up.railway.app   # 或 https://mina.ciwork.cn
MIRA_SESSION_TOKEN=<your-better-auth-session-token>    # URL-encoded form
# 默认 cookie 名是 __Secure-better-auth.session_token，自定义时再设：
# MIRA_COOKIE_NAME=__Secure-better-auth.session_token

# Langfuse（**现在必填** — 用于读取 Mira 真实工具注册表）
LANGFUSE_HOST=https://us.cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
# 兼容：旧 .env 用的 LANGFUSE_BASEURL 仍被认可，会自动 alias 成 LANGFUSE_HOST
```

切换环境：

```bash
# CLI 显式指定
mira-eval run --env staging --category voice

# 或者用环境变量
export MIRA_ENV=staging
mira-eval run
```

未传 `--env` 且 `MIRA_ENV` 未设置时，按 `preview` 走。

**怎么拿 `MIRA_SESSION_TOKEN`：**

1. 浏览器登录 Mira 前端（如 `https://mina.ciwork.cn` 或 `https://mira-bff-preview.up.railway.app`）
2. DevTools → Application → Cookies → 找 `__Secure-better-auth.session_token`
3. 复制 **Value**（URL-encoded 形态原样，例如 `xxx.yyy%2Fzzz%3D`）粘进 `.env.<name>`

> ⚠ token 通常 7 天过期，过期后会看到 `http_401: UNAUTHORIZED`，按上面流程重取一次即可。

**怎么拿 Langfuse 凭据**：登录对应环境的 Langfuse 项目 → Settings → API Keys → 生成 PK/SK。
`LANGFUSE_HOST` 在 cloud.langfuse.com 上美西区是 `https://us.cloud.langfuse.com`，
欧洲区是 `https://cloud.langfuse.com`。

## 5. 握手验证

```bash
.venv/bin/python -c "from mira_eval.client import MiraSession; from mira_eval.config import load_env; load_env(); s=MiraSession(); print(s.send('请用一句话回答：你是谁？')[:200]); print('warnings:', s.warnings)"
```

成功应该看到一行 Mira 的自我介绍，`warnings: []`。

---

## 6. 工具注册表缓存（自动）

`ExpectedToolPathGEval` 等指标需要知道"本次会话有哪些工具可用"。我们不再写死这份清单——
首次跑评测时，`mira-eval run` 在**第一条 golden 跑完后**自动从 Langfuse 拉真实 trace
里的工具定义（含 description / inputSchema），union 到 `.cache/tools-<env>.json`，
后续 golden 复用缓存。

```
.cache/
  tools-preview.json   # 78 个 mira 工具 + description + schema
  tools-staging.json   # 不同环境独立缓存
```

- **TTL**：7 天软过期，过期后下次跑会自动 refresh，期间仍用旧缓存
- **强制刷新**：`--refresh-tools`
- **完全离线**：`--no-langfuse-refresh`（CI 场景，凭现有缓存跑，无 Langfuse 调用）
- **首跑没缓存**：第一条 golden 的工具相关指标会以空工具集运行（PASS/FAIL 结果不可信），
  refresh 完成后第二条及之后正常。介意可以先跑一次空 golden 把缓存预热

`tools-<env>.json` 在 `.gitignore` 里，不会污染 git。

---

## 7. 跑评测

### 7.1 一键完整报告（推荐）

```bash
# 默认跑 4 个新类目（crm / voice / ci_email / ci_dingding）共 8 条 golden × 11 指标
mira-eval run

# 切环境
mira-eval run --env staging --category voice

# 强制刷新工具注册表缓存（拉一次 Langfuse 即可，之后复用）
mira-eval run --refresh-tools --category ci_email

# CI 离线模式（绝不调 Langfuse，纯凭本地缓存）
mira-eval run --no-langfuse-refresh --category ci_email

# 只跑 voice 2 条
mira-eval run --category voice

# 跑全部 18 条 goldens
mira-eval run --all

# 按 tier / index / scenario 子串过滤
mira-eval run --tier light
mira-eval run --index 12,13
mira-eval run --scenario 钉钉

# 自定义输出位置
mira-eval run --category voice --out reports/voice-$(date +%Y%m%d-%H%M)
```

报告头部会标出当前环境、BFF、Langfuse 项目以及本次用到的工具数和缓存来源时间，
跨环境对比时一眼就能区分。

跑完会在 `reports/` 下生成：

- `<name>.md`  — Markdown 报告（含总评 / 按类别 / 按用例 / 按指标 / 每用例详情 + 非 PASS 项 reason 折叠块）
- `<name>.json` — 机器可读完整结果（score / threshold / reason / tool_calls / conv_id / warnings 全留痕）

### 7.2 转 HTML（带样式）

```bash
mira-eval html reports/<name>.md
# 生成 reports/<name>.html — 浅深双主题、表头 sticky、PASS/FAIL/ERR 上色 pill
```

### 7.3 两环境横向对比 HTML

```bash
mira-eval compare \
  --left  reports/voice-A.json --left-label  "环境 A 短名" \
  --right reports/voice-B.json --right-label "环境 B 短名" \
  -o reports/compare-AB.html
```

输出：每条 golden 一个 section，逐指标左右并列，Δ 列按 |Δ| < 0.10 / 0.10-0.30 / ≥ 0.30 上色，分别标识噪声 / 轻微漂移 / 显著进步或退步。
对比 HTML 默认带「📖 指标说明」展开表 + 末尾「每个环境的问题清单」。

### 7.4 离线刷新历史报告（无判官调用）

升级 audit 规则、metric profile、或想给老报告批量补分享链接时：

```bash
# 用最新 audit 重判定，原地更新
mira-eval replay reports/cci-mina.json

# 同时刷新 .md / .html
mira-eval replay reports/cci-mina.json --render

# 给没有 share_url 的 golden 批量补创建公开分享链接
mira-eval replay reports/cci-mina.json --add-share-urls --render --env mina

# 批量处理
mira-eval replay 'reports/cci-*.json' --render
```

**不会做**：重新调 Mira / 重新调 judge / 改变 expected_outcome。只对 JSON 里已存的
score+reason 用最新规则重判定。dataset/expected_outcome 变了仍需重跑评测。

### 7.5 单条 golden × 全部指标的快速体检

```bash
mira-eval healthcheck --golden 13       # 按 index
mira-eval healthcheck --golden "钉钉"   # 按 scenario 子串
```

### 7.6 结果持久化到 MySQL + 跨 run 分析（v0.3+）

在 `.env.<name>` 里加一行就开启数据库持久化（不写也行，本地文件依旧产出）：

```ini
MIRA_RESULTS_DB_URL=mysql+pymysql://root:<urlencoded_pw>@<host>:3306/mira_eval?charset=utf8mb4
```

每次 `mira-eval run` 完成都会把这次 run 的全部数据（meta + goldens + metric results）入库。**DB 写失败不会影响本地 JSON/MD 输出**，是非阻塞的归档动作。

**1. 看历史 run 列表**

```bash
mira-eval report --list --load-env preview
# generated_at       env       goldens  pass fail err info   rate  run_uuid
# 2026-05-19 17:46   preview         1     7    0   0    4  100.0%  b3596fac-...
```

**2. 从 DB 重建任意一次 run 的报告**

```bash
mira-eval report --latest --env preview --load-env preview                   # 最新一次
mira-eval report --run-uuid b3596fac-4620-4fef-975e-60fa3eab70f5 --load-env preview
```

写出的 .md / .json **跟原 run 当时落地的文件 byte-equal**（只差时间戳）。配合 `mira-eval html` 还能补出样式化 HTML。

**3. 单指标趋势**

```bash
mira-eval trend --metric GoalAccuracyMetric --env preview --days 30 --load-env preview
# 按 generated_at 排序的 avg_score + pass rate 表，肉眼能看出 drift
```

**4. 两次 run 的回归诊断**

```bash
mira-eval regression --against-prev --env preview --load-env preview
#   或显式: --a <uuid_a> --b <uuid_b>
```

输出 5 段诊断：

- 🔻 **REGRESSIONS** — PASS → FAIL（最关注的退步项）
- 🟢 **IMPROVEMENTS** — FAIL → PASS（修复 / 改进的项）
- 📉 **SILENT DRIFT** — 都 PASS 但 B 比 A 低 ≥ 0.10（隐性退步，gate 没发现）
- 📈 **SILENT GAINS** — 都 PASS 但 B 比 A 高 ≥ 0.10
- Only in A / Only in B（dataset 增减或 scenario 改名时）

**Schema**：`mira_eval` 库 3 张表（`eval_runs` → `eval_goldens` → `eval_metric_results`），首次写入自动建表。DDL 定义在 `mira_eval/persistence/results_db.py` 顶部用 SQLAlchemy Core 写明。

---

## 8. 项目结构

按 deepeval 上游的 **概念分层** 组织（v0.2 重构 from "all flat in repo root"）：

```
mira_eval/                      # 主包
├── config.py                   # .env.<name> 加载 + ROOT 路径
├── cli/                        # 6 个 console_script 薄入口
│   ├── main.py                 #   mira-eval (dispatcher)
│   ├── run.py / healthcheck.py / compare.py
│   ├── html.py / replay.py / bootstrap.py
├── dataset/loader.py           # 加载 data/goldens.json + tier 过滤
├── client/mira.py              # SSE 客户端 + HITL auto-approve + R2 上传
├── models/claude_cli.py        # 本地 claude CLI → DeepEval judge
├── tracing/
│   ├── langfuse.py             # 拉 trace / tokens / cost / 延迟
│   └── tool_registry.py        # 工具表抓取 + .cache 读写
├── persistence/db.py           # Mira Postgres 适配器
├── evaluate/                   # 主 runner
│   ├── pipeline.py             # 多 golden × N 指标编排（前 report.py 主体）
│   ├── driver.py               # golden → ConversationalTestCase
│   ├── audit.py                # verdict + 一致性 audit + METRIC_PROFILE
│   ├── compare.py              # 两份 JSON → 横向对比 HTML
│   └── replay.py               # 不重调 judge 刷历史 JSON
├── metrics/                    # 每指标一个小包
│   ├── registry.py / builtins.py
│   ├── _base.py                # BaseValueMetric / get_conv_id 公用
│   ├── expected_tool_path/     # ConversationalGEval：工具路径
│   ├── deliverable_match/      # ConversationalGEval：交付物 + 2 个 rubric
│   ├── tool_dependency/        # 工具约束 judge metric
│   ├── session_health/         # 三层 client+trace+db 健康门
│   ├── usage/                  # tokens + cost
│   └── perf/                   # ttft + duration + nturns + tok/s
└── report/                     # 渲染层
    ├── writers.py              # write_json + write_markdown
    └── html.py                 # Markdown → 样式化 HTML

data/                           # 数据资产顶层独立
├── goldens.json                # 18 条 customer goldens
├── tool_dependencies.json      # ToolDependencyMetric 的约束目录
└── README.md                   # 字段语义

reports/                        # 报告输出（gitignored 一部分）
.cache/                         # 运行时工具表缓存（gitignored）
.env.<env>                      # 多环境配置
pyproject.toml                  # 包定义 + console_scripts
```

### 涉及的指标

| 包路径 | 指标 |
|---|---|
| `metrics.builtins.E2E_METRICS` | ConversationCompleteness · TurnRelevancy · KnowledgeRetention · RoleAdherence · GoalAccuracy |
| `metrics.deliverable_match` | ProfessionalNoFabrication · DeliverableMatchesRequest · GroundedNoFabrication |
| `metrics.expected_tool_path` + `builtins.TOOLUSE_MULTI_EXTRA` | ExpectedToolPath · TopicAdherence · ArgumentCorrectness |
| `metrics.builtins.SAFETY_METRICS` | Bias · Toxicity · PIILeakage · RoleViolation |
| `metrics.builtins.OTHERS_METRICS` | AnswerRelevancy · PromptAlignment |
| `metrics.session_health` · `metrics.tool_dependency` · `metrics.usage` · `metrics.perf` | SessionHealth · ToolDependency · Tokens · SessionCost · TTFT · SessionDuration · NTurns · OutputTokensPerSec |

默认 `ACTIVE_METRICS` 跑 11 个；其他通过 env `MIRA_METRICS_ALL=1` 或编辑 `metrics/registry.py` 开关。

---

## 9. 关键运维注意事项

1. **判官 quota**：判官走本地 `claude` CLI，会消耗你的 Claude Code 配额。8 条 golden × 17 指标 ≈ 136 次 judge 调用 × ~30s，跑久了会撞日额度（错误形如 `claude CLI rc=1; stdout="You've hit your limit · resets 8pm (Asia/Shanghai)"`）。重置后继续跑就行。

2. **判官并发限流**：`mira_eval/models/claude_cli.py` 用 `BoundedSemaphore(4)` 限制 CLI 同时在跑的实例数。实测 N≤6 完全安全，再多会被 API rate-limit 把单 call 从 ~6s 拖到 ~30s，wall-clock 收益边际递减。如需调整，改 `_CLI_CONCURRENCY` 常量或设 `CLAUDE_JUDGE_CONCURRENCY` 环境变量。

3. **判官 prompt 走 stdin**：`claude -p` 通过 stdin 输入 prompt（不是 argv），避开 Windows 32KB 命令行长度限制。工具描述累计可达 100KB+，原 argv 模式会撞 `[WinError 206] 文件名或扩展名太长`。

4. **HITL 自动放过**：`mira_eval/client/mira.py` 自动识别 `confirm` / `clarify_question` 这类阻塞 tool 并按 default 值放过，最多 `MAX_APPROVAL_ROUNDS=8`（可用 `MIRA_MAX_APPROVAL_ROUNDS` 环境变量覆盖）。如果你要测「拒绝」路径，调用 `session.send(..., auto_approve=False)`。

5. **文件附件**：在 golden 里加 `"_attachments": ["/abs/path/file.xlsx"]`，driver 会自动用 R2 签名 URL 上传，并把 `[Uploaded File: /mnt/task/upload/<name>|<size>|<r2Key>]` marker 注入到第一条 user message 里——和前端 `task-input.tsx` 行为完全一致。

6. **Mira 非确定性**：同一条 prompt 多次跑可能走不同 agent 路径（少调或多调几个 tool、HITL 轮数变化），分数会抖动。重要结论必须**至少跑 2-3 次取均值**才算可信。

7. **省 token 的两个旋钮**（默认已开启，省 ~50% judge token / 跑）：
   - **`_MAX_TOOL_OUTPUT_CHARS`**：`mira_eval/evaluate/driver.py` 顶部，默认 **1500 字符/工具**（旧版 4000）。多轮 metric 把所有 tool output 拼进 judge prompt，heavy goldens 上 4000 × 19 工具 = 76 KB，撑爆 context。1500 够保留 jobGroupId / 错误信息 / 关键数字。临时调大：`MIRA_MAX_TOOL_OUTPUT_CHARS=4000 mira-eval run …`
   - **`_CATEGORY_DEFAULT_SKIPS`**：`mira_eval/evaluate/pipeline.py` 中间，按 `_category` 跳过结构性无信号指标。当前默认：
     - voice / ci_email / ci_dingding 各跳 5 个：Bias · Toxicity · PIILeakage · RoleViolation · KnowledgeRetention
     - crm 跳 4 个：上面去掉 PIILeakage（CRM 不必然复述 PII）
     - 旧 10 条研究类 golden（无 `_category`）全跑全部指标
   - **覆盖姿势**：在 golden JSON 里加 `"_skip_metrics": ["MetricName1", ...]` 替换默认，或 `"_skip_metrics_extra": [...]` 追加。

8. **工具缓存生命周期**：`.cache/tools-<env>.json` 7 天软过期；每次 `mira-eval run` 跑完第一条 golden 会从 Langfuse union 增量更新（不会清掉旧条目）；Mira 后端加新 MCP 后，下次跑评测自动覆盖到。CI 跑 `--no-langfuse-refresh` 用历史缓存，零外部依赖。

8. **`.gitignore` 当前内容**：
   ```
   .venv/
   __pycache__/
   *.pyc
   .deepeval-cache/
   .deepeval_telemetry.txt
   .cache/                # 工具注册表缓存，环境强相关，不入仓
   ```
   `.env.*` **不**入 ignore，团队共享配置（敏感字段如 token 仍需注意权限）。

---

## 10. 常见错误

| 现象 | 根因 | 处理 |
|---|---|---|
| `http_401: UNAUTHORIZED` | session token 过期 / cookie 跨域 | 重取 token，确认 `MIRA_BFF_URL` 跟登录的前端**同域** |
| `http_404: <!DOCTYPE html>` | `MIRA_BFF_URL` 多了 `/task` 或别的尾缀 | `MIRA_BFF_URL` 必须是 **host**，不能含 `/api/...` 或 `/task` |
| `claude CLI rc=1; stdout="You've hit your limit"` | Judge quota 用尽 | 等 quota 重置（每日定时） |
| `[WinError 206] 文件名或扩展名太长` | 老版本 judge 把 prompt 当 argv 传，被 Windows 命令行长度限制截断 | 已修（改走 stdin）；如再出，确认 `mira_eval/models/claude_cli.py` 是最新版 |
| `langfuse.api.commons.errors.unauthorized_error.UnauthorizedError` | `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` 任一不匹配 | 检查 `.env.<env>`，三个值必须同属一个 Langfuse 项目 |
| `env=...: required vars missing: LANGFUSE_HOST` | 用了旧的 `LANGFUSE_BASEURL` 又被环境变量挡住别名 promote | 改成 `LANGFUSE_HOST` 或确认 `.env` 加载顺序 |
| 首次跑 `ExpectedToolPath` 在第一条 golden 上是 NONE / 异常 | `.cache/tools-<env>.json` 还没建立 | 正常，第二条起就有了；介意可以先 `--refresh-tools` |
| Voice 场景类指标狂 FAIL | 没处理 HITL gate（旧 driver bug） | 已修；如果再出，看 `session.warnings` 是否有 `auto_approve round …` |
| `TopicAdherence` 0.00 false-negative | `mira_eval/metrics/builtins.py:RELEVANT_TOPICS` 没覆盖你的 category | 加一条对应描述进去 |
| `OSError: /mnt/task/output 不存在` 在 judge reason 里 | Mira 沙箱基础设施 bug，**不是评测问题** | 真实产品缺陷，给 Mira 团队报 |

---

## 11. 反馈

- 评测系统本身的 bug / 改进 → 直接在这个 repo 开 issue 或 PR
- Mira 产品质量问题（FAIL 项的 reason 指向 Mira 行为）→ 把 `conv_id`（在 `reports/*.json` 的 `results[].conv_id`）交给 Mira 团队，**这个 `conv_id` 同时就是 Langfuse 的 `session_id`**，他们能直接从 Langfuse 拉到完整 trace（URL 形如 `https://us.cloud.langfuse.com/sessions/<conv_id>`）
