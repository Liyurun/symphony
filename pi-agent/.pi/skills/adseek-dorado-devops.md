---
name: adseek-dorado-devops
description: "Diagnose and operate Dorado/DataLeap tasks for adseek workflows: read task details and SQL, validate HSQL drafts, inspect instances, download logs, analyze failures, recommend dependencies, run safe debug/test flows, and prepare backfill plans with explicit confirmation. Use after Hive producer tasks are found or when users provide Dorado task/instance IDs."
---

# adseek Dorado DevOps

面向广告数据研发的 Dorado/DataLeap 任务诊断与调试 skill。默认只做只读诊断；涉及 debug run、rerun、backfill、commit、online 等会改变执行状态的操作，必须先输出操作计划并等待用户明确确认。

## 适用场景

- 用户提供 Dorado task id / instance id / DataLeap URL，需要查看任务详情、SQL、依赖、告警、实例状态。
- 从 `adseek-hive-explorer` 定位到 producer Dorado task 后，需要读取完整生产 SQL 或分析字段来源。
- HSQL 语法校验、权限错误定位、失败实例诊断、慢任务初步分析。
- 准备 debug run、rerun、backfill，但执行前必须确认。

## 安全策略

| 操作类型 | 是否可直接执行 | 说明 |
|---|---:|---|
| task get / task code / task diff / instance get / instance list | 是 | 只读 |
| task-draft explain / dts-draft explain / dep-recommendations | 是 | 校验和推荐，不改变线上状态 |
| download-instance-log | 是 | 如需 session，先提示登录 |
| task-draft test / adhoc exec | 否 | 需要用户确认 SQL、日期、队列、task id |
| task rerun / backfill create | 否 | 必须确认 task id、project id、日期范围、region、影响范围 |
| task update / commit / online / transfer-owner | 否 | 必须显式确认，不能自动执行 |

## 前置检查

```bash
bytedcli --version
bytedcli auth status
export BYTEDCLI_NETWORK_PROFILE=prod
```

如果需要页面态能力：

```bash
bytedcli auth login --session
```

## 标准工作流：读取任务和 SQL

```bash
bytedcli --json dorado task get <task_id> --region sg
bytedcli dorado task code --task-id <task_id> --region sg
```

重点提取：

- task id / task name / owner / project id
- task type / typeGroup
- output tables / input tables
- dependencies
- schedule / queue / cluster
- SQL code

## 标准工作流：HSQL 校验

已知 task id 时：

```bash
bytedcli --json dorado task-draft explain <task_id> --region sg
```

如果需要校验线上版本：

```bash
bytedcli --json dorado task-draft explain <task_id> --online --region sg
```

如果错误是权限问题，输出缺失权限的库表和建议申请路径；不要用 IAM 替代 Coral 权限申请。

## 标准工作流：失败实例诊断

### 1. 查询实例

```bash
bytedcli --json dorado instance get <instance_id> --region sg
```

或按 task 查询实例：

```bash
bytedcli --json dorado instance list --project-id <project_id> --task-id <task_id> --region sg
```

### 2. 查询 Spark History / Megatron 诊断

```bash
bytedcli --json dorado get-spark-history --instance-id <instance_id> --region sg
```

### 3. 无 YARN application 时下载日志

```bash
bytedcli auth login --session
bytedcli dorado download-instance-log --instance-id <instance_id> --project-id <project_id> --region sg -o temp/instance_<instance_id>.log
```

优先检查关键错误：

```bash
grep -nE 'NoPrivilegeException|permission|privilege|CalciteContextException|SemanticException|ParseException|AnalysisException|Number of INSERT target columns|TQS 查询失败|FAILED|ERROR' temp/instance_<instance_id>.log | head -n 200
```

## 标准工作流：依赖推荐

```bash
bytedcli --json dorado task dep-recommendations <task_id> --region sg
```

用于根据 SQL 推荐上游依赖任务，适合字段变更、补依赖、产出链路修复。

## Debug / Backfill 执行前确认模板

执行任何非只读操作前，先输出：

```markdown
## 待确认操作
- 操作类型：debug run / rerun / backfill / online / commit
- Task ID：
- Project ID：
- Region：
- 日期范围：
- 队列/集群：
- 是否使用草稿：
- 影响范围：
- 回滚/停止方式：

请确认是否执行。
```

用户确认后再执行对应命令。

## 标准输出格式

```markdown
## 1. 任务概览
- Task ID：
- Project ID：
- Region：
- Owner：
- Type：

## 2. SQL / 配置摘要
- 输出表：
- 输入表：
- 关键 CTE：

## 3. 校验 / 诊断结果
- 语法：
- 权限：
- 实例状态：
- 根因：

## 4. 建议动作
- 可直接做：
- 需要确认后做：
```

## 与 Hive Explorer 的配合

当 `adseek-hive-explorer` 在 `ad_dim.dim_overseas_creative` 或其他表上查到 producer task id 后，使用本 skill 继续读取任务 SQL、校验 SQL、诊断实例或准备回灌。

如果 `adseek-hive-explorer` 没有拿到明确的 `task_id`，本 skill 不能单独推断“哪一个 Dorado 任务就是加工逻辑任务”。此时只应接收以下任一输入后继续：

- 明确的 Dorado `task_id`
- 明确的 Dorado `project_id`
- 来自 Hive Explorer 的候选任务列表

没有这些输入时，不要编造任务归属。
