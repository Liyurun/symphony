

## Conversational Style

- Keep answers short and concise.
- No emojis in commits, task comments, PR comments, SQL, or DDL.
- No fluff or cheerful filler text (e.g., write "已修改", not "非常感谢，马上帮您搞定！").
- Technical prose only, be direct. Technical terms (table names, field names, task names, partition names, SQL keywords) stay in English.
- When the user asks a question, answer it first, then generate DDL/SQL or run metadata/write commands.
- When responding to user feedback or an analysis, explicitly state whether you agree or disagree before saying what you changed.

## Metadata First (No Guessing)

- Never fabricate tables, tasks, fields, field types, comments, or partitions. Before any create/alter/task change, read real metadata and task SQL first. Do not infer schema from naming conventions or search snippets.
- Before wide-ranging changes (or when asked to investigate/audit lineage), read the full upstream and target DDL and the full task SQL, not fragments.
- Resolve identifiers by priority, never skip a step: user-provided → default naming rule → lineage/produced-by task → task SQL search. If any object cannot be uniquely confirmed, show candidates and ask; do not proceed.
- Confirm real types and function support via the platform/engine (e.g. `coral_hive_table_info`, `coral_hive_table_ddl`, `read_table`); do not guess types or supported syntax.
- All field comments must be written in English. If a source comment is in Chinese, translate it before writing to DDL / schema / IDL. `cross_region` / `merge` field comments inherit from the upstream `local` / `cross_region` table and must not be left empty.

## SQL & DDL Quality

- No `SELECT *`. Always list columns explicitly. `SELECT` field order must match the DDL non-partition field order exactly.
- If the user does not specify, default to the SG data center (i18n).
- Use `CASE WHEN`, not `IF()`.
- Every upstream table must have a partition filter in `WHERE`.
- Use `LEFT JOIN` to preserve main-table completeness; use `COUNT(DISTINCT ...)` where dedup is required.
- Use `INSERT OVERWRITE TABLE <target> PARTITION (<partition_column>='<date_param>')`; never mix the partition column into the `SELECT` list.
- DDL goes up to the `PARTITIONED BY` clause and ends with a semicolon — no `STORED AS` / `TBLPROPERTIES` unless the user asks. Use `BIGINT` for 0/1 flags.
- Adding fields: append to the end of the DDL non-partition field list, never reorder existing fields. The new field must pass through every CTE / `SELECT` level from source to final `INSERT`. A dimension field entering a CTE with `GROUP BY` must also be added to that `GROUP BY`.
- Modifying fields: do not reorder fields; update the DDL only when the name/type/comment changes; update the logic in the correct CTE layer and add inline comments for complex changes.
- Never change an existing field's type, drop an existing field, or alter existing `detect_uv` / privacy logic just to "make it work". Ask first.
- Non-additive metrics (`ratio`/`rate`/`avg`/`mean`/`ctr`/`cvr`/`percent`/`pct`/`share`/`score`) must never default to `SUM`; confirm the aggregation. Additive metrics use `SUM(<field>) AS <field>`.
- Validate final SQL with the platform parser (e.g. `dorado_parse_hsql`). Fix all errors except table-permission errors and "column count mismatch" when the DDL is not yet applied. Stop fixing immediately (fail fast) on any other syntax/schema error.
- IDL / schema validation may not be downgraded (`verify_idl` stays `on`). When a compliance annotation is uncertain, leave it empty and explicitly tell the user which fields are un-annotated.

## Blocking Questions

Ask first in the following cases; do not generate or execute any unconfirmed final change:

- Missing or unparseable `local` table name or DB name.
- Missing `local` schema and metadata is unreadable.
- `cross_region` or `merge` table/task cannot be uniquely confirmed.
- New field is not in the `local` schema (confirm whether to change `local` first).
- New field type conflicts with an existing downstream field type.
- A region source lacks the new field.
- Missing default-lossless / `detect_uv` decision, or `detect_uv` kept without a source.
- A new/merge field looks non-additive but the aggregation is unclear.
- Decc field type conflicts with `cross_region`.
- No `data_id` found (route to new-table Bootstrap after confirmation).

Attach discovered candidates to each question. If any blocking item exists, output only the location results, candidates, and questions — no unconfirmed final SQL.

## Output Format

- Default Chinese output; keep technical nouns in English.
- New-link tasks/artifacts always output the complete SQL/DDL, not fragments; field-change tasks output the complete modified SQL or a clearly marked diff.
- Organize output with numbered sections: (1) blocking questions / metadata-location results, (2) assumptions, (3) naming results, (4) target DDL, (5) `cross_region` task SQL, (6) `merge` view/table SQL, (7) `merge` task SQL, (8) Decc alignment, (9) dependency config, (10) dimension/metric notes. Omit sections that don't apply.

## Files & Artifacts

- Target DDL → `src/code/ddl/` with suffix `_Global.ddl.sql`; table YAML → `src/config/table/` with suffix `_Global_table.yml`; core SQL → `src/code/sql/` with suffix `_Global.sql`.
- Table YAML: set `fieldConfig.fields` and `fieldConfig.partitionKeys` to `[]`; fill `${OWNER}`/`${BUSINESS_CONTACT}`/`${TTL}` from `config.yml`; `alias`/`description` from spec context.
- Core SQL skeleton: a single `WITH` block (`cleaning_layer`, `join_layer`, `metric_layer` as three pure `SELECT` CTEs) → `INSERT OVERWRITE ... PARTITION (...)` → final `SELECT` aligned to the DDL non-partition fields.
- Record decisions and compliance notes in `research.md`; keep audit artifacts (`run_dir`) after each write step.

## default setting
project = global_1156 或者 project = sg_150000021