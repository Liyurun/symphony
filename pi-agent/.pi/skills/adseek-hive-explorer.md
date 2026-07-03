---
name: adseek-hive-explorer
description: "Explore ByteDance Hive/Coral data assets for adseek workflows: table metadata, schema, partitions, lineage, producer Dorado tasks, producer SQL, field-source analysis, and downstream impact assessment. Use when users ask about Hive tables such as ad_dim.dim_overseas_creative, table ownership, DDL/schema, upstream/downstream dependencies, or change impact."
---

# adseek Hive Explorer

面向广告数据研发的 Hive/Coral 数据资产分析 skill。默认以 `sg` region / i18n 站点为优先查询环境；如果用户明确给出 region，则使用用户指定 region。部分 EU/Global 广告表实际落在 `gcp` / EU Compliance2 Coral 环境，`sg` 返回 Maat 404 或表不存在时，应优先重试 `gcp`。

## 适用场景

- 解释一张 Hive 表的用途、owner、schema、分区、TTL、project 绑定等元信息。
- 查询表的 producer Dorado task、生产 SQL、上游表、下游表。
- 分析字段来源、字段含义、字段修改影响面。
- 为新增字段、改字段、下游影响评估提供真实元数据依据。

## 基本原则

1. 不要编造表、字段、类型、注释、owner、task id 或 SQL。
2. 修改建议前必须先读取真实元数据；字段变更必须先看完整 schema 和 producer SQL。
3. 默认 region 使用 `sg`；如果 `sg` 查不到或出现 Maat Gateway 404，优先重试 `gcp`，再提示用户确认是否查 `cn` / `va` / `mycis` 等其他 region。
4. 查询类操作可直接执行；任何写操作、上线、回灌、任务调试都必须转交 `adseek-dorado-devops` 并让用户确认。
5. 输出中文，表名、字段名、SQL、任务名等技术对象保持原文。

## 前置检查

优先确认 `bytedcli` 可用：

```bash
bytedcli --version
```

如 sg region 鉴权失败，先登录：

```bash
bytedcli auth login
export BYTEDCLI_NETWORK_PROFILE=prod
```

如果提示需要页面态 session，再执行：

```bash
bytedcli auth login --session
```

## 标准工作流：解释 Hive 表

用户给出 `db.table` 后，按顺序执行。

### 0. 优先运行 task bridge 脚本拿 task_id

优先使用同目录脚本，它会按 `gcp -> us-eastred -> us -> sg -> va -> mycis -> cn` 依次尝试 Coral bridge `dorado-tasks`，并对结果做排序，直接返回候选 `task_id` 列表：

```bash
python ./adseek_hive_task_bridge.py --db <db> --table <table>
```

对于 `ad_dim.dim_overseas_creative`，这个脚本已经验证可在 `us-eastred` 拿到候选任务。脚本还会对高分候选继续执行 `dorado task get`，尝试从 `outerDependencies` / `dependencies` 里提取更接近原始生产逻辑的 `canonical_source_candidates`。

使用顺序：

1. 若 `canonical_source_candidates` 非空，优先把这些 task_id 视为真实加工逻辑任务。
2. 否则退回 `primary_candidates`，把前 1~3 个高分可访问任务作为“加工逻辑候选任务”。
3. 再交给 `adseek-dorado-devops` 读取 SQL。

### 1. 读取表详情

```bash
bytedcli --json hive detail <db> <table> --region sg
```

如果返回 Maat Gateway 404 或表不存在，优先按 `gcp` 重试：

```bash
bytedcli --json hive detail <db> <table> --region gcp
```

重点提取：

- table guid
- owner / owners
- columns / partition keys
- comments / alias / description
- storage / TTL / lifecycle
- producer Dorado task id，如返回中包含

### 2. 查询行数和分区规模

```bash
bytedcli --json hive rows <db> <table> --region sg
```

用于判断表是否有数据、分区是否正常、近期分区规模是否异常。

### 3. 查询血缘

先从 `hive detail` 结果中拿 guid，再查 lineage：

```bash
bytedcli --json hive lineage <guid> --region sg --depth 1
```

如果 `gcp` lineage 返回 OG tagging / invalid schema 403，不要编造血缘；应在输出中标记为“lineage endpoint 受 OG 限制，本次未取到”，并继续使用 `hive detail` 中的 producer 信息、表 schema、partition 信息做分析。

需要评估影响面时，depth 可以提升到 2 或 3，但要在输出中说明深度。

### 4. 定位 producer Dorado task 和 SQL

如果 `hive detail` 或 lineage 中出现 Dorado task id，读取任务详情和 SQL：

```bash
bytedcli --json dorado task get <task_id> --region sg
bytedcli dorado task code --task-id <task_id> --region sg
```

如果缺少 project id，优先使用 `task get` 自动解析；如果命令要求 project id，再从 task detail 中补充。

### 4.1 直接走 Coral dorado-tasks bridge

如果脚本不可用，手工执行等价命令：

```bash
bytedcli --json coral hive table dorado-tasks --db-name <db> --table-name <table> --region <region>
```

注意：对于海外广告表，不要只查 `gcp`。`gcp` 可能返回空数组，而 `us-eastred` 能返回实际 Dorado tasks。

## 获取“加工逻辑任务”的判定规则

当用户要求“获取某张表的加工逻辑”时，必须满足下面链路之一：

1. `hive detail` 直接返回 `producerDoradoTasks`。
2. `hive lineage` 成功返回上游 process / DoradoTask。
3. `coral hive table dorado-tasks` 或 `adseek_hive_task_bridge.py` 返回候选任务。
4. 已知 Dorado `project-id`，且能通过 Dorado 搜索在任务代码中搜到目标表。

如果四条都失败，则本 skill **不能声称已经拿到加工逻辑任务**，只能输出：

- 已确认的表元信息
- 已执行过哪些定位步骤
- 当前阻塞点（例如 `producerDoradoTasks` 为空、lineage 403、project list / search 受 OG 限制）
- 下一步所需信息（最优先是 Dorado `task_id` 或准确 `project_id`）

### Dorado 搜索回退策略

当 `producerDoradoTasks` 为空时，按下面顺序补充搜索：

```bash
python ./adseek_hive_task_bridge.py --db <db> --table <table>
bytedcli --json coral hive table dorado-tasks --db-name <db> --table-name <table> --region us-eastred
bytedcli --json dorado task list --region <region> --project-id <project_id> --keyword "<table>" --limit 10
bytedcli --json dorado task advanced-search --region <region> --project-id <project_id> -k "<table>" --search-scope content --limit 10
bytedcli --json dorado task advanced-search --region <region> --project-id <project_id> -k "<db>.<table>" --search-scope content --limit 10
```

如果已知 project 但搜不到任务，不要编造“加工任务”；应明确标记为“当前 project 范围内未搜到明显生产任务”。

如果连 `project list` / `project search` 都被 OG tagging 拦截，也不要继续猜测项目；此时应要求用户提供 Dorado `task_id` 或 `project_id`。

## 标准输出格式

```markdown
## 1. 表概览
- 表名：
- Region：
- Owner：
- 说明：
- 分区：
- 数据规模：

## 2. Schema 摘要
| 字段 | 类型 | 分区键 | 注释 |
|---|---|---|---|

## 3. Producer Dorado Task
| Task ID | Task Name | Owner | Project ID |
|---|---|---|---|

## 4. 上游依赖
| 表/实体 | 类型 | 说明 |
|---|---|---|

## 5. 下游影响
| 表/实体 | 类型 | 风险 |
|---|---|---|

## 6. 字段/变更分析
- 字段来源：
- 可能影响：
- 建议验证：

## 7. 加工逻辑任务定位结论
- 是否已定位到 Dorado task：是 / 否
- 定位依据：producerDoradoTasks / lineage / dorado-tasks bridge / Dorado 搜索
- 是否已定位到原始生产 task：是 / 否
- 原始生产 task 候选：
- 阻塞点：
- 下一步需要：task_id / project_id / lineage 权限
```

## 测试样例

可用下面的表验证核心能力：

```bash
bytedcli --json hive detail ad_dim dim_overseas_creative --region gcp
python ./adseek_hive_task_bridge.py --db ad_dim --table dim_overseas_creative
```

如果返回 guid，再继续：

```bash
bytedcli --json hive lineage <guid> --region gcp --depth 1
bytedcli --json hive rows ad_dim dim_overseas_creative --region gcp
```

该测试表当前可验证到：`hive detail` 与 `hive rows` 可用；`hive lineage` 可能因 EU OG schema/tagging 限制返回 403，遇到时按受限能力处理。

对 `ad_dim.dim_overseas_creative`，当前已验证：

- `coral hive table dorado-tasks --region us-eastred` 能返回候选 task；
- 可访问高分候选包括 `501645988` 与 `501645996`；
- 继续解析候选 task 的 `outerDependencies` 后，可得到更接近原始生产逻辑的上游 task `150870878`（region=`sg`，task 名 `[1_VA]dim_overseas_creative`）。

因此本 skill 现在已能真正拿到 `task_id`，并且能进一步逼近原始加工逻辑任务。
