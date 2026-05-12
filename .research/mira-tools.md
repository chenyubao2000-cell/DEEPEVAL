# Mira 工具注册表

## 静态内置工具（每次会话都可用）

| toolName | 一句话 | input schema 摘要 | 关键文件:行 |
|---|---|---|---|
| search | Web 检索 | {query: string, numResults?: number, category?: enum, searchType?: 'neural'\|'auto'\|'fast'\|'deep', includeDomains?: string[], excludeDomains?: string[], startPublishedDate?: string, endPublishedDate?: string} | apps/mira-work/lib/ai/tools/exa-tools.ts:188 |
| company_search | 公司搜索 | {query: string, numResults?: number, ...} | apps/mira-work/lib/ai/tools/exa-tools.ts:330+ |
| people_search | 人才搜索 | {query: string, numResults?: number, seniority?: enum, location?: string, ...} | apps/mira-work/lib/ai/tools/people-data-tool.ts:400+ |
| clarify_question | 澄清问题 | {field definitions} | apps/mira-work/lib/ai/tools/clarify-question.ts:50+ |
| confirm | 确认对话（HITL） | {context?: string, options?: ...} | apps/mira-work/lib/ai/tools/confirm-tool.ts:30+ |
| write_todos | 写待办事项 | {todos: array, ...} | apps/mira-work/lib/ai/tools/write-todos-tool.ts:40+ |
| complete | 任务完成标记 | {artifacts?: array, ...} | apps/mira-work/lib/ai/tools/complete-tool.ts:50+ |
| code_interpreter | Python 代码执行 | {intent: string, code: string, timeout?: number} | apps/mira-work/lib/ai/tools/code-interpreter-tool.ts:51 |
| sb_command_execute | Shell 命令执行 | {command: string, folder?: string, timeout?: number} | apps/mira-work/lib/ai/tools/shell-tools.ts:67 |
| sb_file_create | 创建文件 | {filePath: string, fileContents: string} | apps/mira-work/lib/ai/tools/files-tools.ts:52 |
| sb_file_rewrite | 完全重写文件 | {filePath: string, fileContents: string} | apps/mira-work/lib/ai/tools/files-tools.ts:150+ |
| sb_file_edit | 编辑文件 | {targetFile: string, instructions: string, codeEdit: string} | apps/mira-work/lib/ai/tools/files-tools.ts:200+ |
| sb_docx_create | 创建 Word 文档 | {filePath: string, content: string, ...} | apps/mira-work/lib/ai/tools/office-tools.ts:500+ |
| sb_pptx_create | 创建 PowerPoint | {filePath: string, content: string, ...} | apps/mira-work/lib/ai/tools/office-tools.ts:800+ |
| sb_xlsx_create | 创建 Excel 表格 | {filePath: string, content: string, ...} | apps/mira-work/lib/ai/tools/office-tools.ts:1279 |
| sb_pdf_create | 创建 PDF 文档 | {filePath: string, content: string, ...} | apps/mira-work/lib/ai/tools/office-tools.ts:900+ |
| sb_image_create | 创建图像 | {filePath: string, content: string} | apps/mira-work/lib/ai/tools/image-tools.ts:110+ |
| generate_people_data | 生成人才数据 | {taskType: string, searchResults: array, ...} | apps/mira-work/lib/ai/tools/data-generate-tool.ts:200+ |
| evaluate_people | 人才匹配评估（仅 ENABLE_PEOPLE_EVALUATION=true） | {query: string, candidates: array} | apps/mira-work/lib/ai/tools/evaluate-people-tool.ts:100+ |

## BUA 浏览器控制工具（仅 BUA_ENABLED=true，23 个）

bua_check_status / bua_request_authorization / bua_confirm_sensitive_action / bua_navigate / bua_click / bua_type / bua_scroll / bua_extract_content / bua_wait_for_user / bua_snapshot / bua_dom_tree / bua_hover / bua_fill / bua_fill_form / bua_press_key / bua_handle_dialog / bua_wait_for / bua_screenshot / bua_new_tab / bua_list_tabs / bua_select_tab / bua_close_tab / bua_evaluate

均位于 `apps/mira-work/lib/ai/tools/bua-tools.ts`。

## 动态 MCP 连接器

由 `loadConnectorTools(user, taskId)` 动态加载，配置在 `apps/mira-work/lib/ai/registry/builtin-tools.ts:174+`。
- 用户自配置 server: `mcp_user_servers` 表
- 预置 server: `mcp_preset_servers` 表（Notion/Slack/LinkedIn/Gmail 等）

## 注意事项

1. 工具条件性启用：`evaluate_people` 需 `ENABLE_PEOPLE_EVALUATION=true`；BUA 工具族需 `BUA_ENABLED=true`
2. HITL 工具（前端裁决）：`bua_request_authorization`, `confirm`
3. SSE 事件里的 `toolName` 字段与本表 `toolName` 完全一致（例如 `search`、`sb_pptx_create`）
