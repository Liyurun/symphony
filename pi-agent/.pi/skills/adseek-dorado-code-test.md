---
name: adseek-dorado-code-test
description: "Test and validate Dorado/DataLeap task code for adseek workflows: inspect task drafts, run explain checks, prepare debug test runs, and diagnose validation failures. Use when users ask to test Dorado code, validate HSQL/DTS drafts, or check whether a Dorado task can run before publish."
---

# adseek Dorado Code Test

面向广告数据研发的 Dorado/DataLeap 代码测试节点。用于发布前校验任务草稿、检查 SQL/配置、执行 explain、准备 debug test，并输出可作为发布前门禁的测试结论。

默认只直接执行只读或低风险校验；`task-draft test` / debug run 会创建调试运行，必须先输出确认计划并等待用户明确确认。

## 适用场景

- 用户提供 Dorado task id / DataLeap 任务 URL，需要测试当前草稿是否能通过语法和平台校验。
- `adseek-hive-explorer` 或 `adseek-dorado-devops` 已定位到 producer task，需要在发布前验证代码。
- 检查 HSQL / DTS / stream SQL / python / notebook / spark 节点的草稿状态、语法错误、权限错误、依赖缺失。
- 为后续 `adseek-dorado-publish` 输出是否可发布的结构化结论。

## 输入要求

优先要求用户提供：

- `task_id` 或 Dorado/DataLeap 任务 URL。
- `region`，未提供时默认 `sg`。
- `project_id`，如果命令可自动从 task detail 解析则可省略；如果解析失败再追问。
- 是否测试草稿或线上版本，默认测试草稿。
- 如果要执行 debug test，必须提供或确认日期参数、队列/集群、输入参数覆盖。

从 URL 解析时：

- `/dorado/development/node/<taskId>?groupName=<region>&project=<region>_<projectId>` 中，路径里的 `<taskId>` 是 task id。
- `groupName` 是 `region`。
- `project` 去掉 `<region>_` 前缀后是 `project_id`。

## 安全策略

| 操作类型 | 是否可直接执行 | 说明 |
|---|---:|---|
| `dorado task get` / `task code` / `task alarms` / `task history` | 是 | 只读，用于确认任务、代码、owner、project、类型 |
| `dorado task-draft explain` / `dts-draft explain` | 是 | 平台语法/配置校验，不发布、不改线上 |
| `dorado task dep-recommendations` | 是 | 依赖推荐，不改变线上状态 |
| `dorado task-draft test` | 否 | 会发起 debug run，执行前必须确认 |
| `dorado adhoc exec` / `task rerun` / `backfill create` | 否 | 不属于本 skill 的默认动作，转 `adseek-dorado-devops` 并确认 |
| `task update` / `commit` / `online` / `deploy approve` | 否 | 发布动作必须转 `adseek-dorado-publish` |

## 前置检查

```bash
bytedcli --version
bytedcli auth status
export BYTEDCLI_NETWORK_PROFILE=prod
```

如果 Dorado API 返回 403 或提示需要页面态：

```bash
bytedcli auth login --session
```

对于 MY-BD 环境，使用：

```bash
bytedcli --site i18n-bd auth login --session
```

## 标准工作流

### 1. 读取任务详情

```bash
bytedcli --json dorado task get <task_id> --region <region>
```

提取并核对：

- task id / task name / owner / project id
- task type / typeGroup
- schedule / queue / cluster / dc
- input tables / output tables
- dependencies / outerDependencies
- draft 是否存在、是否已部署、最近版本

如果用户只给了 task 名称和 project：

```bash
bytedcli --json dorado task list --region <region> --project-id <project_id> --keyword "<keyword>" --limit 10
```

多个候选时先让用户确认，不要猜测。

### 2. 读取代码

SQL 类任务：

```bash
bytedcli dorado task code --task-id <task_id> --region <region>
```

python / notebook / spark 节点如只有 task id，先解析 node uid：

```bash
bytedcli --json dorado node resolve-uid --task-id <task_id> --project-id <project_id> --region <region>
```

再读取节点草稿：

```bash
bytedcli --json dorado node get --node-id <node_id> --project-id <project_id> --region <region>
```

### 3. 执行 explain 校验

HSQL / SQL 类草稿：

```bash
bytedcli --json dorado task-draft explain <task_id> --project-id <project_id> --region <region>
```

如果需要校验线上版本：

```bash
bytedcli --json dorado task-draft explain <task_id> --project-id <project_id> --online --region <region>
```

DTS reader SQL：

```bash
bytedcli --json dorado dts-draft explain <task_id> --project-id <project_id> --region <region>
```

解释结果时必须区分：

- 语法错误：给出错误位置、原始报错、建议修复。
- 表/字段不存在：先确认真实 metadata，不要臆造字段。
- 权限错误：输出缺失权限对象，建议转 `bytedance-coral` / Coral 权限申请；不要用 `bytedcli hive` 或 `bytedcli iam` 替代权限申请。
- OG / tagging / session 错误：提示登录或 region 鉴权问题，不要声称代码错误。

### 4. 依赖推荐

```bash
bytedcli --json dorado task dep-recommendations <task_id> --region <region>
```

用于检查代码引用的上游表是否有推荐依赖。只输出建议，不自动修改依赖。

### 5. 准备 debug test

执行前必须先输出确认计划：

```markdown
## 待确认 Debug Test
- Task ID：
- Project ID：
- Region：
- 测试对象：草稿 / 线上版本
- 日期参数：
- input params：
- 队列/集群：
- 预计影响：创建一次 Dorado debug run，不影响线上调度
- 停止/排查方式：

请确认是否执行 debug test。
```

用户明确确认后，才执行：

```bash
bytedcli --json dorado task-draft test <task_id> --project-id <project_id> --region <region>
```

如需参数覆盖：

```bash
bytedcli --json dorado task-draft test <task_id> --project-id <project_id> --region <region> --input-params '[{"name":"date","debugVal":"2026-07-01"}]'
```

拿到 debug id 后，查询状态/结果：

```bash
bytedcli --json dorado adhoc status --debug-id <debug_id> --region <region>
bytedcli --json dorado adhoc result --debug-id <debug_id> --region <region>
bytedcli dorado adhoc log --debug-id <debug_id> --region <region>
```

## 发布前测试结论格式

```markdown
## 1. 任务概览
- Task ID：
- Project ID：
- Region：
- Task Name：
- Owner：
- Type：

## 2. 代码读取结果
- 是否成功读取代码：
- 输出表：
- 输入表：
- 关键逻辑摘要：

## 3. Explain 校验
- 结论：通过 / 不通过 / 被权限或鉴权阻塞
- 原始错误：
- 定位：
- 修复建议：

## 4. 依赖检查
- 推荐依赖：
- 当前依赖：
- 差异与风险：

## 5. Debug Test
- 是否执行：
- Debug ID：
- 状态：
- 结果摘要：

## 6. 是否建议进入发布节点
- 结论：可以进入 `adseek-dorado-publish` / 暂不建议发布
- 发布前必须处理：
- 可接受风险：
```

## 与其他 adseek Skill 的配合

- 从 `adseek-hive-explorer` 获取 producer task 后，用本 skill 做发布前代码校验。
- 本 skill 只负责测试与验证，不执行发布。
- 如果测试通过且用户要求发布，转 `adseek-dorado-publish`。
- 如果测试失败且需要查实例日志、权限、慢任务，转 `adseek-dorado-devops`。

## 输出要求

- 默认中文输出；技术对象如 task id、project id、region、SQL、字段名保持原文。
- 不编造 task、字段、SQL、依赖或校验结果。
- 出现多个候选 task 时，先列候选并提问，不继续执行测试。
- 非只读动作必须先输出确认计划，得到用户明确确认后再执行。
