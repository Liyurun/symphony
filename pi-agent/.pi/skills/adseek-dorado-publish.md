---
name: adseek-dorado-publish
description: "Prepare and execute Dorado/DataLeap task publish flows for adseek workflows: online tasks, submit approval packages, inspect deploy diffs, and approve/reject deploys. Use when users explicitly ask to publish, submit for approval, bring a Dorado task online, or review a Dorado deploy package."
---

# adseek Dorado Publish

面向广告数据研发的 Dorado/DataLeap 发布节点。用于把已测试通过的 Dorado 任务草稿提交上线、提交审批、查看发布包 diff、审批或拒绝发布包。

发布是有线上副作用的操作。任何 `online`、`commit-approval`、`deploy approve`、`deploy reject`、`node submit` 动作都必须先输出发布计划，等待用户明确确认后才能执行。

## 适用场景

- 用户明确要求发布 Dorado 任务、上线任务、提交审批、创建发布包。
- `adseek-dorado-code-test` 已输出“可以进入发布节点”。
- 用户提供 deploy id，需要查看发布包 SQL diff、审批或拒绝。
- python / notebook / spark 节点需要 submit 或 submit-approval。
- realtime stream 任务需要 online 或 stream-online。

## 输入要求

发布任务前必须确认：

- `task_id` 或 `node_id` 或 `deploy_id`。
- `project_id`。
- `region`，未提供时默认 `sg`。
- 发布类型：直接上线 / 提交审批 / 批量审批包 / 审批发布包 / 拒绝发布包。
- 发布说明 `message`。
- 审批场景必须由用户显式提供 `review_policy_id` 和 `review_users`。
- 可选：`baseline_ids`、`custom_alarm_rule_ids`、`skip_codes`、`agent_config`。

从 URL 解析时：

- `/dorado/development/node/<taskId>?groupName=<region>&project=<region>_<projectId>` 中，路径里的 `<taskId>` 是 task id。
- `groupName` 是 `region`。
- `project` 去掉 `<region>_` 前缀后是 `project_id`。
- 发布包详情页中的 deploy id 只能用于 `dorado deploy ...` 命令，不要混入 `task diff` 语义。

## 禁止事项

1. 不要自动猜测 `review_policy_id` 或 `review_users`。
2. 不要在没有用户明确确认时执行 `--yes` 或任何发布写操作。
3. 不要把发布包读取或审批流程混入 `task` 命令；发布包统一使用 `dorado deploy` 命令组。
4. 不要用 `node submit` 处理 SQL 类任务。SQL 类任务使用 `task online` / `task commit-approval`。
5. 不要用普通 `node submit` 加额外字段来模拟 `node submit-approval`。
6. 不要用普通 `task online` 加额外字段来模拟 `task commit-approval`。

## 前置检查

```bash
bytedcli --version
bytedcli auth status
export BYTEDCLI_NETWORK_PROFILE=prod
```

如果 Dorado API 返回 403 或提示页面态：

```bash
bytedcli auth login --session
```

对于 MY-BD 环境：

```bash
bytedcli --site i18n-bd auth login --session
```

## 发布前只读检查

发布前必须先执行只读检查，除非用户已经在同一上下文提供了最新检查结果。

### 1. 读取任务详情

```bash
bytedcli --json dorado task get <task_id> --region <region>
```

核对：

- task id / task name / owner / project id
- task type / typeGroup
- output tables / input tables
- dependencies / outerDependencies
- schedule / queue / cluster / dc
- draft 是否存在

### 2. 读取代码或发布 diff

SQL 类任务：

```bash
bytedcli dorado task code --task-id <task_id> --region <region>
```

已有 deploy id 时查看发布包 diff：

```bash
bytedcli dorado deploy diff-sql --deploy-id <deploy_id> --project-id <project_id> --region <region>
```

### 3. 建议先调用测试节点

如果用户没有提供测试通过结论，先建议或执行 `adseek-dorado-code-test` 的 explain 检查：

```bash
bytedcli --json dorado task-draft explain <task_id> --project-id <project_id> --region <region>
```

如果 explain 失败，不继续发布；输出失败原因和修复建议。

## 发布确认模板

执行任何发布写操作前，必须输出：

```markdown
## 待确认发布操作
- 操作类型：task online / task commit-approval / task commit-batch-approval / deploy approve / deploy reject / node submit / node submit-approval
- Task ID / Node ID / Deploy ID：
- Project ID：
- Region：
- Task Name：
- Owner：
- 发布说明：
- Review Policy ID：
- Review Users：
- Baseline IDs：
- Custom Alarm Rule IDs：
- Skip Codes：
- 影响范围：
- 回滚/撤回方式：
- 已完成测试：是 / 否

请确认是否执行。只有你明确回复确认后，我才会执行发布命令。
```

## 标准发布命令

### 1. SQL / batch 任务直接上线

适用于不需要审批、用户明确要求直接上线的任务：

```bash
bytedcli dorado task online <task_id> --project-id <project_id> --message "<message>" --region <region>
```

如平台返回可跳过确认类错误，并且用户确认跳过：

```bash
bytedcli dorado task online <task_id> --project-id <project_id> --message "<message>" --skip-codes "<codes>" --region <region>
```

注意：realtime stream draft 会由 bytedcli 自动切到 `PUT /realtime/{taskId}/online`。

### 2. SQL / batch 任务提交审批

审批参数必须由用户显式提供：

```bash
bytedcli dorado task commit-approval <task_id> --project-id <project_id> \
  --review-policy-id <review_policy_id> \
  --review-users "<user_a,user_b>" \
  --region <region>
```

带告警、基线、agent config 的示例：

```bash
bytedcli dorado task commit-approval <task_id> --project-id <project_id> \
  --review-policy-id <review_policy_id> \
  --review-users "<user_a,user_b>" \
  --custom-alarm-rule-ids <ids> \
  --baseline-ids <ids> \
  --agent-config '<json>' \
  --region <region>
```

### 3. 批量提交审批包

适用于已有多个 commit id，需要合并为一个 deploy package：

```bash
bytedcli dorado task commit-batch-approval --project-id <project_id> \
  --name "<deploy_package_name>" \
  --message "<message>" \
  --review-policy-id <review_policy_id> \
  --review-users "<user_a,user_b>" \
  --commit-ids "<commit_id_a,commit_id_b>" \
  --region <region>
```

不要用循环单任务审批模拟批量审批包。

### 4. Stream 任务旧式发布包流程

正常 stream 任务优先用 `task online`。只有用户明确要求 deploy-package 两步流程时，使用：

```bash
bytedcli dorado task stream-online <task_id> \
  --project-id <project_id> \
  --review-users "<review_users>" \
  --review-policy-id <review_policy_id> \
  --message "<message>" \
  --region <region>
```

### 5. 发布包审批 / 拒绝

先查看 diff：

```bash
bytedcli dorado deploy diff-sql --deploy-id <deploy_id> --project-id <project_id> --region <region>
```

审批前必须确认，确认后执行：

```bash
bytedcli dorado deploy approve --deploy-id <deploy_id> --project-id <project_id> --review-message "<message>" --region <region> --yes
```

拒绝前必须确认，确认后执行：

```bash
bytedcli dorado deploy reject --deploy-id <deploy_id> --project-id <project_id> --review-message "<message>" --region <region> --yes
```

`--yes` 只能在用户明确确认后使用。

### 6. python / notebook / spark 节点提交

node 子命令只适用于 python / notebook / spark 类型任务。

无需审批：

```bash
bytedcli dorado node submit --node-id <node_id> --project-id <project_id> --message "<message>" --region <region>
```

需要审批：

```bash
bytedcli dorado node submit-approval --node-id <node_id> --project-id <project_id> \
  --message "<message>" \
  --review-policy-id <review_policy_id> \
  --review-users "<user_a,user_b>" \
  --region <region>
```

## 发布结果检查

发布命令执行后，读取任务详情或发布包状态：

```bash
bytedcli --json dorado task get <task_id> --region <region>
```

如果有 deploy id：

```bash
bytedcli dorado deploy diff-sql --deploy-id <deploy_id> --project-id <project_id> --region <region>
```

如果发布后触发实例失败，转 `adseek-dorado-devops` 做实例诊断。

## 标准输出格式

```markdown
## 1. 发布对象
- 操作类型：
- Task ID / Node ID / Deploy ID：
- Project ID：
- Region：
- Task Name：
- Owner：

## 2. 发布前检查
- 代码读取：
- Explain：
- Diff：
- 风险：

## 3. 发布确认
- 是否已得到用户确认：
- 确认内容：

## 4. 执行结果
- 命令：
- 状态：
- Deploy ID / Commit ID：
- 平台返回：

## 5. 下一步
- 需要审批：
- 需要观察：
- 失败排查入口：
```

## 与其他 adseek Skill 的配合

- `adseek-hive-explorer`：定位表的 producer Dorado task。
- `adseek-dorado-code-test`：发布前校验代码、explain、debug test。
- `adseek-dorado-devops`：发布后实例失败、日志、权限、慢任务诊断。

## 输出要求

- 默认中文输出；技术对象保持原文。
- 先回答能否发布，再给命令和结果。
- 不编造审批人、审批策略、deploy id、commit id。
- 没有明确确认时，只能输出计划，不能执行发布写操作。
