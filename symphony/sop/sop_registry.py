"""SOP registry — manages SOP template loading, caching, and CRUD operations.

SOPs can be loaded from:
1. YAML files in data/sop_templates/
2. The SQLite EventLog (sop_templates table)
3. Programmatic registration at runtime
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import yaml

from symphony.core.event_log import EventLog
from symphony.sop.sop_definition import SOPDefinition

logger = logging.getLogger(__name__)


class SOPRegistry:
    """Registry for SOP templates with multi-source loading.

    Resolution order:
    1. In-memory cache (fastest)
    2. SQLite EventLog (persisted)
    3. YAML files on disk (source of truth)
    """

    def __init__(
        self,
        event_log: EventLog,
        templates_dir: str | Path = "data/sop_templates",
    ):
        self.event_log = event_log
        self.templates_dir = Path(templates_dir)
        self._cache: dict[str, SOPDefinition] = {}

    async def load_all(self) -> list[SOPDefinition]:
        """Load all SOP templates from disk and DB into cache."""
        sops: list[SOPDefinition] = []

        # Load from YAML files first (source of truth)
        if self.templates_dir.exists():
            for yaml_file in sorted(self.templates_dir.glob("*.yaml")):
                try:
                    sop = self._load_from_yaml(yaml_file)
                    if sop:
                        sops.append(sop)
                        self._cache[sop.name] = sop
                except Exception as e:
                    logger.error(f"Failed to load SOP from {yaml_file}: {e}")

            for yml_file in sorted(self.templates_dir.glob("*.yml")):
                try:
                    sop = self._load_from_yaml(yml_file)
                    if sop:
                        sops.append(sop)
                        self._cache[sop.name] = sop
                except Exception as e:
                    logger.error(f"Failed to load SOP from {yml_file}: {e}")

        # Also load from DB
        db_templates = await self.event_log.list_sop_templates()
        for tpl in db_templates:
            name = tpl["name"]
            if name not in self._cache:
                try:
                    sop = SOPDefinition.model_validate(
                        self._normalize_definition(tpl["definition"])
                    )
                    sops.append(sop)
                    self._cache[name] = sop
                except Exception as e:
                    logger.error(f"Failed to load SOP from DB: {name}: {e}")

        return sops

    async def get(self, name: str) -> SOPDefinition | None:
        """Get a SOP template by name."""
        if name in self._cache:
            return self._cache[name]

        # Try DB
        db_tpl = await self.event_log.get_sop_template(name)
        if db_tpl:
            sop = SOPDefinition.model_validate(
                self._normalize_definition(db_tpl["definition"])
            )
            self._cache[name] = sop
            return sop

        # Try disk
        yaml_path = self.templates_dir / f"{name}.yaml"
        if yaml_path.exists():
            sop = self._load_from_yaml(yaml_path)
            if sop:
                self._cache[name] = sop
                return sop

        yml_path = self.templates_dir / f"{name}.yml"
        if yml_path.exists():
            sop = self._load_from_yaml(yml_path)
            if sop:
                self._cache[name] = sop
                return sop

        return None

    async def register(self, sop: SOPDefinition) -> None:
        """Register a SOP template in memory and persist to DB."""
        self._cache[sop.name] = sop
        await self.event_log.save_sop_template(
            name=sop.name,
            version=sop.version,
            definition=sop.model_dump(),
        )

    async def delete(self, name: str) -> None:
        """Delete a SOP template."""
        self._cache.pop(name, None)
        await self.event_log.delete_sop_template(name)

    async def list_names(self) -> list[str]:
        """List all available SOP template names."""
        if not self._cache:
            await self.load_all()
        return sorted(self._cache.keys())

    async def list_all(self) -> list[SOPDefinition]:
        """List all loaded SOP definitions."""
        if not self._cache:
            await self.load_all()
        return list(self._cache.values())

    async def validate(self, definition: dict) -> SOPDefinition:
        """Validate SOP definition without saving. Raises on invalid."""
        return SOPDefinition.model_validate(definition)

    def _load_from_yaml(self, path: Path) -> SOPDefinition | None:
        """Parse a YAML file into a SOPDefinition."""
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data or "name" not in data:
            logger.warning(f"Invalid SOP YAML (missing 'name'): {path}")
            return None

        return SOPDefinition.model_validate(self._normalize_definition(data))

    @staticmethod
    def _normalize_definition(definition: str | dict) -> dict:
        """Normalize legacy persisted SOP definitions to the current schema.

        Older file-backed templates stored ``definition`` as a dict (not a JSON
        string) and did not have the four-part SOP contract fields. Loading them
        directly made startup log errors like ``json.loads(dict)`` and made the
        Web UI look like the old SOP creator. This keeps old templates readable
        while exposing the new required input/output fields to the editor.
        """
        if isinstance(definition, str):
            data = json.loads(definition)
        elif isinstance(definition, dict):
            data = dict(definition)
        else:
            raise TypeError(
                f"SOP definition must be str or dict, got {type(definition).__name__}"
            )

        description = str(data.get("description") or "")
        data.setdefault("input_requirements", "")
        data.setdefault("output_requirements", "")
        if not str(data.get("input_requirements") or "").strip():
            data["input_requirements"] = (
                "输入必须包含本次任务的完整背景、目标、涉及对象、限制条件和验收标准。"
            )
        if not str(data.get("output_requirements") or "").strip():
            data["output_requirements"] = (
                "输出必须给出完整结论、执行步骤、关键依据、风险点和可验证的交付结果。"
            )

        nodes = data.get("nodes") or []
        normalized_nodes = []
        for index, raw_node in enumerate(nodes):
            node = dict(raw_node or {})
            node.setdefault("id", f"step-{index + 1}")
            node.setdefault("name", node["id"])
            node.setdefault("skill", "")
            node.setdefault("depends_on", [])
            node.setdefault("description", description if index == 0 else "")
            if not str(node.get("input_requirements") or "").strip():
                node["input_requirements"] = data["input_requirements"]
            if not str(node.get("output_requirements") or "").strip():
                node["output_requirements"] = data["output_requirements"]
            normalized_nodes.append(node)
        data["nodes"] = normalized_nodes
        return data
