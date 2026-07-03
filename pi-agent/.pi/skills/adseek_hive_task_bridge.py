#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass


DEFAULT_REGIONS = [
    "gcp",
    "us-eastred",
    "us",
    "sg",
    "va",
    "mycis",
    "cn",
]


@dataclass
class RegionResult:
    region: str
    ok: bool
    data: list[dict]
    error: str | None = None


def run_bytedcli(db_name: str, table_name: str, region: str) -> RegionResult:
    cmd = [
        "bytedcli",
        "--json",
        "coral",
        "hive",
        "table",
        "dorado-tasks",
        "--db-name",
        db_name,
        "--table-name",
        table_name,
        "--region",
        region,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as exc:
        return RegionResult(region=region, ok=False, data=[], error=str(exc))

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    payload = None
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None

    if proc.returncode == 0 and isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return RegionResult(region=region, ok=True, data=data)

    error_message = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            error_message = error.get("message")
    if not error_message:
        error_message = stderr or stdout or f"exit_code={proc.returncode}"
    return RegionResult(region=region, ok=False, data=[], error=error_message)


def run_dorado_task_get(task_id: int, region: str) -> dict | None:
    cmd = [
        "bytedcli",
        "--json",
        "dorado",
        "task",
        "get",
        str(task_id),
        "--region",
        region,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception:
        return None

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if proc.returncode == 0 and isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
    return None


def extract_source_task_candidates(task_detail: dict) -> list[dict]:
    source_candidates: list[dict] = []
    seen: set[tuple[str, int]] = set()

    for item in task_detail.get("dependencies") or []:
        task_id = item.get("parentTaskId")
        if isinstance(task_id, int):
            key = (str(task_detail.get("region") or ""), task_id)
            if key not in seen:
                seen.add(key)
                source_candidates.append(
                    {
                        "region": task_detail.get("region"),
                        "task_id": task_id,
                        "source": "dependencies",
                    }
                )

    outer_raw = task_detail.get("outerDependencies")
    outer = {}
    if isinstance(outer_raw, str) and outer_raw.strip():
        try:
            outer = json.loads(outer_raw)
        except json.JSONDecodeError:
            outer = {}
    elif isinstance(outer_raw, dict):
        outer = outer_raw

    if isinstance(outer, dict):
        for region, items in outer.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                task_id = item.get("parentTaskId")
                if isinstance(task_id, int):
                    key = (str(region), task_id)
                    if key not in seen:
                        seen.add(key)
                        source_candidates.append(
                            {
                                "region": region,
                                "task_id": task_id,
                                "source": "outerDependencies",
                            }
                        )
    return source_candidates


def score_task(task: dict, db_name: str, table_name: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    name = str(task.get("taskName") or "")
    task_type = str(task.get("taskType") or "")
    query = str(task.get("query") or "")
    lowered_name = name.lower()
    lowered_query = query.lower()
    target_table = f"{db_name}.{table_name}".lower()

    if task.get("canAccess") is True:
        score += 50
        reasons.append("can_access")
    if query:
        score += 40
        reasons.append("has_query")
    if target_table in lowered_query:
        score += 35
        reasons.append("query_mentions_target_table")
    if task_type in {"global_hsql", "hsql"}:
        score += 20
        reasons.append("sql_task")

    if "sensor" in lowered_name:
        score -= 60
        reasons.append("sensor_penalty")
    if "delete" in lowered_name or lowered_name.startswith("dr-"):
        score -= 40
        reasons.append("retention_penalty")
    if "repair" in lowered_name or "restore" in lowered_name:
        score -= 10
        reasons.append("repair_penalty")

    return score, reasons


def build_output(db_name: str, table_name: str, region_results: list[RegionResult]) -> dict:
    all_candidates: list[dict] = []
    errors: list[dict] = []
    for rr in region_results:
        if rr.ok:
            for item in rr.data:
                score, reasons = score_task(item, db_name, table_name)
                all_candidates.append(
                    {
                        "region": rr.region,
                        "score": score,
                        "score_reasons": reasons,
                        "task_id": item.get("taskId"),
                        "project_id": item.get("projectId"),
                        "task_name": item.get("taskName"),
                        "task_type": item.get("taskType"),
                        "task_owner": item.get("taskOwner"),
                        "can_access": item.get("canAccess"),
                        "access_denied_msg": item.get("accessDeniedMsg"),
                        "query": item.get("query"),
                    }
                )
        else:
            errors.append({"region": rr.region, "error": rr.error})

    all_candidates.sort(key=lambda item: (-item["score"], str(item["task_id"])))

    for item in all_candidates[:10]:
        if not item.get("can_access"):
            continue
        task_id = item.get("task_id")
        region = item.get("region")
        if not isinstance(task_id, int) or not isinstance(region, str):
            continue
        detail = run_dorado_task_get(task_id, region)
        if not detail:
            continue
        item["resolved_task_region"] = detail.get("region")
        item["resolved_project_id"] = detail.get("projectId")
        source_candidates = extract_source_task_candidates(detail)
        if source_candidates:
            item["source_task_candidates"] = source_candidates

    primary_candidates = [
        item
        for item in all_candidates
        if item["score"] >= 60 and item.get("task_id") is not None
    ]

    canonical_source_candidates: list[dict] = []
    seen_source: set[tuple[str, int]] = set()
    for item in primary_candidates:
        for source in item.get("source_task_candidates") or []:
            region = source.get("region")
            task_id = source.get("task_id")
            if isinstance(region, str) and isinstance(task_id, int):
                key = (region, task_id)
                if key not in seen_source:
                    seen_source.add(key)
                    canonical_source_candidates.append(source)

    return {
        "database": db_name,
        "table": table_name,
        "searched_regions": [rr.region for rr in region_results],
        "primary_candidates": primary_candidates[:10],
        "canonical_source_candidates": canonical_source_candidates,
        "all_candidates": all_candidates[:50],
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Locate Dorado task IDs for a Hive table")
    parser.add_argument("--db", required=True, help="Hive database name")
    parser.add_argument("--table", required=True, help="Hive table name")
    parser.add_argument(
        "--regions",
        nargs="*",
        default=DEFAULT_REGIONS,
        help="Ordered Coral regions to query",
    )
    args = parser.parse_args()

    region_results = [run_bytedcli(args.db, args.table, region) for region in args.regions]
    output = build_output(args.db, args.table, region_results)
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
