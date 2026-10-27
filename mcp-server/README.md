# campus_tools_mcp — 校园工具 MCP 服务器

为 LLM / TeleAgent 提供校园学生场景的 6 个只读工具（校历与通知接口尚未接入）：

> ⚠️ 2026-10-25 变更：已移除 `campus_query_course_schedule`（课表查询）与 `campus_query_grades`（成绩查询）两个演示工具，待接入真实教务系统后重建；同日新增 `campus_calculate_gpa` 与 `campus_countdown_deadlines`。

| 工具 | 功能 | 只读 | 幂等 |
|---|---|---|---|
| `campus_calculator` | 安全数学计算（AST 白名单） | ✅ | ✅ |
| `campus_get_current_time` | 当前日期时间与星期 | ✅ | 否 |
| `campus_query_academic_calendar` | 学期校历关键节点 | ✅ | ✅ |
| `campus_search_notice` | 关键词检索校园通知 | ✅ | ✅ |
| `campus_calculate_gpa` | GPA 绩点计算（4.0 制 / 五级制，含加权均分与最优最差课） | ✅ | ✅ |
| `campus_countdown_deadlines` | DDL 倒计时（个人事项 + 校历节点，urgent 预警） | ✅ | 否 |

- **传输方式**：StreamableHTTP，默认 `http://127.0.0.1:8310/mcp`（无需认证）
- **依赖**：`pip install -r requirements.txt`（mcp>=1.0,<2 + pydantic）
- **数据说明**：演示校历和通知已删除；这两个 MCP 工具返回 `available=false`，可在校园助手网页知识库中查询官网资料。DDL 仅计算用户输入事项，不合并虚构校历；通用 GPA 规则不代表本校官方规则。
- **评估**：`evaluation.xml` 保留计算类问答，移除依赖旧模拟校历和通知的答案，可用于测试 LLM 能否仅凭这些工具回答复杂问题

## 启动

```
cd mcp-server
python server.py
```

或双击 `启动MCP服务.bat`。启动后日志显示 `Uvicorn running on http://127.0.0.1:8310`。

## 注册到 TeleAgent（工具设置 → MCP 配置 → 粘贴以下 JSON）

```json
{
  "mcpServers": {
    "campus-tools": {
      "url": "http://127.0.0.1:8310/mcp"
    }
  }
}
```

操作步骤：左下角头像 → 工具设置 → MCP 配置区域点击添加 → 粘贴 JSON → 保存 → 确认状态为「已连接」。

### 备选：stdio 本地子进程模式（仅当你明确需要时）

```json
{
  "mcpServers": {
    "campus-tools": {
      "command": "python",
      "args": ["C:\\Users\\Admini\\.local\\share\\TeleAgent\\TeleAgent的工作空间\\campus-agent\\mcp-server\\server.py"]
    }
  }
}
```

注意：stdio 模式下 server.py 末尾的 StreamableHTTP 启动不生效，FastMCP 默认走 stdio，可直接作为子进程运行（本服务器未做参数区分，如需固定 stdio 行为可删除 `mcp.run(...)` 的 transport 参数）。

## 常见问题

| 问题 | 原因 | 解决 |
|---|---|---|
| 状态「连接失败」/ 502 | 服务器未启动，或系统代理拦截了 127.0.0.1 | 先 `python server.py` 启动；本机若开系统代理（如 127.0.0.1:7897），需在代理软件中设置「绕过本地地址」，或设置环境变量 `NO_PROXY=127.0.0.1,localhost` |
| `ModuleNotFoundError: No module named 'mcp'` | python 解析到了另一个解释器 | 用绝对路径启动：`C:\...\runtimes\python\python.exe server.py`，或在该解释器中安装 `mcp>=1.0,<2` |
| 端口冲突 | 8310 被占用 | 修改 `server.py` 顶部 `PORT` 常量并同步更新 JSON 中的 URL |

## 已验证

- `python -m py_compile server.py` 通过
- MCP 客户端实测：tools/list 返回 **6 个工具**；新增 GPA/DDL 工具 6 组用例验证通过（standard4 加权断言、wq5 五级制、60/100 边界、urgent 预警、今天截止、include_calendar 开关、非法日期被 Pydantic 拦截）
- 历史验证：2026-10-25 移除课表/成绩后 4 工具健康检查通过；更早 6 工具 10 组调用符合预期

> AI生成
