# DeepEval × Mira

针对 [Mira](https://github.com/) AI 招聘助手 BFF 的端到端评测套件，基于
[DeepEval](https://github.com/confident-ai/deepeval) + 本地 `claude` CLI 作裁判模型。

特性：

- ✅ 一份数据集（`tests/evals/.dataset.json`）驱动 17 个指标 × 多个用例的横向评测
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

**一键安装（推荐）：**

```bash
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

或手动 pin：

```bash
.venv/bin/pip install \
  deepeval==4.0.0 \
  httpx==0.28.1 \
  python-dotenv==1.2.2 \
  pydantic==2.13.4 \
  pytest==9.0.3 \
  markdown-it-py==4.2.0 \
  langfuse>=4.6
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

## 4. 配置环境 `.env.<name>`

每个 Mira 部署（preview / staging / prod / …）有自己的 BFF、token、Langfuse 项目，
我们用 `.env.<name>` 一个文件一份环境。默认环境名为 `preview`。

仓库根目录创建 **`.env.preview`**（旧版用户保留的 `.env` 会作为 fallback，
即没有 `.env.<name>` 时自动用 `.env`）：

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
.venv/bin/python report.py --env staging --category voice

# 或者用环境变量（pytest 流也会读）
export MIRA_ENV=staging
.venv/bin/python report.py
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
.venv/bin/python -c "from mira_client import MiraSession; s=MiraSession(); print(s.send('请用一句话回答：你是谁？')[:200]); print('warnings:', s.warnings)"
```

成功应该看到一行 Mira 的自我介绍，`warnings: []`。

---

## 6. 工具注册表缓存（自动）

`ToolUseMetric` 需要知道 "本次会话有哪些工具可用"。我们不再写死这份清单——
首次跑评测时，`report.py` 在**第一条 golden 跑完后**自动从 Langfuse 拉真实 trace
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
- **首跑没缓存**：第一条 golden 的 `ToolUseMetric` 会以空工具集运行（PASS/FAIL 结果不可信），
  refresh 完成后第二条及之后正常。介意可以先跑一次空 golden 把缓存预热

`tools-<env>.json` 在 `.gitignore` 里，不会污染 git。

---

## 7. 跑评测

### 7.1 一键完整报告（推荐）

```bash
# 默认跑 4 个新类目（crm / voice / ci_email / ci_dingding）共 8 条 golden × 17 指标
.venv/bin/python report.py

# 切环境
.venv/bin/python report.py --env staging --category voice

# 强制刷新工具注册表缓存（拉一次 Langfuse 即可，之后复用）
.venv/bin/python report.py --refresh-tools --category ci_email

# CI 离线模式（绝不调 Langfuse，纯凭本地缓存）
.venv/bin/python report.py --no-langfuse-refresh --category ci_email

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

报告头部会标出当前环境、BFF、Langfuse 项目以及本次用到的工具数和缓存来源时间，
跨环境对比时一眼就能区分。

跑完会在 `reports/` 下生成：

- `<name>.md`  — Markdown 报告（含总评 / 按类别 / 按用例 / 按指标 / 每用例详情 + 非 PASS 项 reason 折叠块）
- `<name>.json` — 机器可读完整结果（score / threshold / reason / tool_calls / conv_id / warnings 全留痕）

### 7.2 转 HTML（带样式）

```bash
.venv/bin/python md_to_html.py reports/<name>.md
# 生成 reports/<name>.html — 浅深双主题、表头 sticky、PASS/FAIL/ERR 上色 pill
```

### 7.3 两环境横向对比 HTML

```bash
.venv/bin/python compare_reports.py \
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
.venv/bin/python replay_audit.py reports/cci-mina.json

# 同时刷新 .md / .html
.venv/bin/python replay_audit.py reports/cci-mina.json --render

# 给没有 share_url 的 golden 批量补创建公开分享链接
.venv/bin/python replay_audit.py reports/cci-mina.json --add-share-urls --render --env mina

# 批量处理
.venv/bin/python replay_audit.py 'reports/cci-*.json' --render
```

**不会做**：重新调 Mira / 重新调 judge / 改变 expected_outcome。只对 JSON 里已存的
score+reason 用最新规则重判定。dataset/expected_outcome 变了仍需重跑评测。

### 7.4 pytest 标准 CI 流（按 metric 文件分套件）

```bash
# 5 个 metric 文件，按 concern 分类
.venv/bin/python -m pytest tests/evals/test_mira_e2e.py -v
.venv/bin/python -m pytest tests/evals/test_mira_custom.py -v
.venv/bin/python -m pytest tests/evals/test_mira_tooluse.py -v
.venv/bin/python -m pytest tests/evals/test_mira_safety.py -v
.venv/bin/python -m pytest tests/evals/test_mira_others.py -v

# 按 tier 过滤
MIRA_GOLDEN_TIER=light .venv/bin/python -m pytest tests/evals -v

# 切环境（pytest 走环境变量）
MIRA_ENV=staging .venv/bin/python -m pytest tests/evals -v

# 只跑某条 golden（按 scenario 子串）
.venv/bin/python -m pytest tests/evals/test_mira_e2e.py -k "Voice 场景 2" -v
```

> pytest 模式下 `ToolUseMetric.available_tools` 直接读 `.cache/tools-<env>.json`，
> 没有 `report.py` 的自动 refresh 流。如需更新缓存，先跑一次 `report.py --refresh-tools`。

### 7.5 单条 golden × 全部 17 指标的快速体检

```bash
.venv/bin/python healthcheck.py --golden 13       # 按 index
.venv/bin/python healthcheck.py --golden "钉钉"   # 按 scenario 子串
```

---

## 8. 项目结构

```
DEEPEVAL/
├── mira_client.py              # SSE 客户端 + 文件上传 + HITL auto-approve
├── claude_cli_judge.py         # 本地 claude CLI 包装成 DeepEval judge（stdin 输入,无 argv 长度限制；默认强制中文 reason）
├── report.py                   # 多 golden × 17 指标主入口（含 metric profile / audit / 分层渲染 / share-URL）
├── compare_reports.py          # 两份 JSON → 横向对比 HTML（含指标说明 + 按环境问题清单）
├── md_to_html.py               # 单份 Markdown → 样式化 HTML
├── replay_audit.py             # 用最新 audit/verdict/share-URL 规则刷新历史 JSON 报告（不重调 judge）
├── healthcheck.py              # 单 golden 全指标体检
├── requirements.txt            # 一键 pip install -r 的依赖清单
├── .env.preview                # 默认环境配置（或保留旧 .env 作 fallback）
├── .env.staging                # 其他环境按需
├── .cache/                     # 运行时生成，每环境一份工具注册表（gitignored）
│   └── tools-<env>.json
└── tests/evals/
    ├── .dataset.json           # 18 条 customer goldens
    ├── _env.py                 # 环境加载 + 多 env 选择 + Langfuse client 工厂
    ├── _langfuse_tools.py      # 从 Langfuse trace 抓工具注册表 + 缓存读写
    ├── _driver.py              # 共享 driver；把 cached registry 注入到 tool calls
    ├── test_mira_e2e.py        # 5 个多轮内置 metric
    ├── test_mira_custom.py     # 3 个多轮 ConversationalGEval
    ├── test_mira_tooluse.py    # 2 多轮 + 1 单轮工具相关 metric（available_tools 从 cache 读）
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

## 9. 关键运维注意事项

1. **判官 quota**：判官走本地 `claude` CLI，会消耗你的 Claude Code 配额。8 条 golden × 17 指标 ≈ 136 次 judge 调用 × ~30s，跑久了会撞日额度（错误形如 `claude CLI rc=1; stdout="You've hit your limit · resets 8pm (Asia/Shanghai)"`）。重置后继续跑就行。

2. **判官串行**：`claude_cli_judge.py` 用进程级锁保证 CLI 串行执行（Claude Code 的 `~/.claude` 共享状态对并发不友好）。所以并行加速对 judge 阶段无效。

3. **判官 prompt 走 stdin**：`claude -p` 通过 stdin 输入 prompt（不是 argv），避开 Windows 32KB 命令行长度限制。工具描述累计可达 100KB+，原 argv 模式会撞 `[WinError 206] 文件名或扩展名太长`。

4. **HITL 自动放过**：`mira_client.py` 自动识别 `confirm` / `clarify_question` 这类阻塞 tool 并按 default 值放过，最多 `MAX_APPROVAL_ROUNDS=8`（可用 `MIRA_MAX_APPROVAL_ROUNDS` 环境变量覆盖）。如果你要测「拒绝」路径，调用 `session.send(..., auto_approve=False)`。

5. **文件附件**：在 golden 里加 `"_attachments": ["/abs/path/file.xlsx"]`，driver 会自动用 R2 签名 URL 上传，并把 `[Uploaded File: /mnt/task/upload/<name>|<size>|<r2Key>]` marker 注入到第一条 user message 里——和前端 `task-input.tsx` 行为完全一致。

6. **Mira 非确定性**：同一条 prompt 多次跑可能走不同 agent 路径（少调或多调几个 tool、HITL 轮数变化），分数会抖动。重要结论必须**至少跑 2-3 次取均值**才算可信。

7. **省 token 的两个旋钮**（默认已开启，省 ~50% judge token / 跑）：
   - **`_MAX_TOOL_OUTPUT_CHARS`**：`tests/evals/_driver.py` 顶部，默认 **1500 字符/工具**（旧版 4000）。多轮 metric 把所有 tool output 拼进 judge prompt，heavy goldens 上 4000 × 19 工具 = 76 KB，撑爆 context。1500 够保留 jobGroupId / 错误信息 / 关键数字。临时调大：`MIRA_MAX_TOOL_OUTPUT_CHARS=4000 .venv/bin/python report.py …`
   - **`_CATEGORY_DEFAULT_SKIPS`**：`report.py` 中间，按 `_category` 跳过结构性无信号指标。当前默认：
     - voice / ci_email / ci_dingding 各跳 5 个：Bias · Toxicity · PIILeakage · RoleViolation · KnowledgeRetention
     - crm 跳 4 个：上面去掉 PIILeakage（CRM 不必然复述 PII）
     - 旧 10 条研究类 golden（无 `_category`）全跑 17 个
   - **覆盖姿势**：在 golden JSON 里加 `"_skip_metrics": ["MetricName1", ...]` 替换默认，或 `"_skip_metrics_extra": [...]` 追加。

7. **工具缓存生命周期**：`.cache/tools-<env>.json` 7 天软过期；每次 `report.py` 跑完第一条 golden 会从 Langfuse union 增量更新（不会清掉旧条目）；Mira 后端加新 MCP 后，下次跑评测自动覆盖到。CI 跑 `--no-langfuse-refresh` 用历史缓存，零外部依赖。

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
| `[WinError 206] 文件名或扩展名太长` | 老版本 judge 把 prompt 当 argv 传，被 Windows 命令行长度限制截断 | 已修（改走 stdin）；如再出，确认 `claude_cli_judge.py` 是最新版 |
| `langfuse.api.commons.errors.unauthorized_error.UnauthorizedError` | `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` 任一不匹配 | 检查 `.env.<env>`，三个值必须同属一个 Langfuse 项目 |
| `env=...: required vars missing: LANGFUSE_HOST` | 用了旧的 `LANGFUSE_BASEURL` 又被环境变量挡住别名 promote | 改成 `LANGFUSE_HOST` 或确认 `.env` 加载顺序 |
| 首次跑 `ToolUseMetric` 在第一条 golden 上是 NONE / 异常 | `.cache/tools-<env>.json` 还没建立 | 正常，第二条起就有了；介意可以先 `--refresh-tools` |
| Voice 场景类指标狂 FAIL | 没处理 HITL gate（旧 driver bug） | 已修；如果再出，看 `session.warnings` 是否有 `auto_approve round …` |
| `TopicAdherence` 0.00 false-negative | `tests/evals/test_mira_tooluse.py:RELEVANT_TOPICS` 没覆盖你的 category | 加一条对应描述进去 |
| `OSError: /mnt/task/output 不存在` 在 judge reason 里 | Mira 沙箱基础设施 bug，**不是评测问题** | 真实产品缺陷，给 Mira 团队报 |

---

## 11. 反馈

- 评测系统本身的 bug / 改进 → 直接在这个 repo 开 issue 或 PR
- Mira 产品质量问题（FAIL 项的 reason 指向 Mira 行为）→ 把 `conv_id`（在 `reports/*.json` 的 `results[].conv_id`）交给 Mira 团队，**这个 `conv_id` 同时就是 Langfuse 的 `session_id`**，他们能直接从 Langfuse 拉到完整 trace（URL 形如 `https://us.cloud.langfuse.com/sessions/<conv_id>`）
