"""SOP REST API — CRUD operations for SOP templates."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from symphony.sop.sop_definition import NodeDefinition, SOPDefinition
from symphony.sop.sop_registry import SOPRegistry


class CreateSOPRequest(BaseModel):
    name: str
    version: str = "1.0"
    description: str = ""
    input_requirements: str = ""
    output_requirements: str = ""
    nodes: list[dict] = Field(default_factory=list)


class ValidateSOPRequest(BaseModel):
    definition: dict


class ImportSOPRequest(BaseModel):
    yaml_text: str


def create_sop_router(sop_registry: SOPRegistry) -> APIRouter:
    router = APIRouter(tags=["sop"])

    @router.get("/sop")
    async def list_sops():
        """List all SOP templates."""
        sops = await sop_registry.list_all()
        return [
            {
                "name": s.name,
                "version": s.version,
                "description": s.description,
                "input_requirements": s.input_requirements,
                "output_requirements": s.output_requirements,
                "node_count": len(s.nodes),
                "nodes": [n.model_dump() for n in s.nodes],
            }
            for s in sops
        ]

    @router.get("/sop/{name}")
    async def get_sop(name: str):
        """Get a SOP template by name."""
        sop = await sop_registry.get(name)
        if not sop:
            raise HTTPException(status_code=404, detail=f"SOP '{name}' not found")
        return sop.model_dump()

    @router.post("/sop")
    async def create_or_update_sop(req: CreateSOPRequest):
        """Create or update a SOP template."""
        try:
            if not req.name.strip():
                raise ValueError("name is required")
            if not req.description.strip():
                raise ValueError("description is required")
            if not req.input_requirements.strip():
                raise ValueError("input_requirements is required")
            if not req.output_requirements.strip():
                raise ValueError("output_requirements is required")

            raw_nodes = req.nodes or [
                {
                    "id": "step-1",
                    "name": req.name,
                    "description": req.description,
                    "input_requirements": req.input_requirements,
                    "output_requirements": req.output_requirements,
                    "skill": "",
                }
            ]

            nodes = []
            seen_ids: set[str] = set()
            for index, raw_node_data in enumerate(raw_nodes):
                node_data = dict(raw_node_data or {})
                node_data.setdefault("id", f"step-{index + 1}")
                node_data.setdefault("name", node_data["id"])
                node_data.setdefault("skill", "")
                node_data.setdefault("depends_on", [])
                node_data.setdefault("description", req.description if index == 0 else "")
                node_data.setdefault("input_requirements", req.input_requirements)
                node_data.setdefault("output_requirements", req.output_requirements)
                node_data.setdefault("input_artifact_type", "text")
                node_data.setdefault("output_artifact_type", "text")
                node_data.setdefault("input_conditions", "")
                node_data.setdefault("output_conditions", "")
                if node_data["id"] in seen_ids:
                    raise ValueError(f"duplicate node id: {node_data['id']}")
                seen_ids.add(node_data["id"])
                if not str(node_data.get("input_requirements", "")).strip():
                    raise ValueError(f"node '{node_data.get('id', '')}' input_requirements is required")
                if not str(node_data.get("output_requirements", "")).strip():
                    raise ValueError(f"node '{node_data.get('id', '')}' output_requirements is required")
                for dep_id in node_data.get("depends_on", []) or []:
                    if dep_id == node_data["id"]:
                        raise ValueError(f"node '{node_data['id']}' cannot depend on itself")
                nodes.append(NodeDefinition.model_validate(node_data))

            known_ids = {node.id for node in nodes}
            for node in nodes:
                for dep_id in node.depends_on:
                    if dep_id not in known_ids:
                        raise ValueError(f"node '{node.id}' depends on unknown node '{dep_id}'")
            _assert_acyclic(nodes)

            sop = SOPDefinition(
                name=req.name,
                version=req.version,
                description=req.description,
                input_requirements=req.input_requirements,
                output_requirements=req.output_requirements,
                nodes=nodes,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid SOP definition: {e}")

        await sop_registry.register(sop)
        return {"status": "saved", "name": sop.name, "version": sop.version}

    @router.post("/sop/validate")
    async def validate_sop(req: ValidateSOPRequest):
        """Validate a SOP definition without saving."""
        try:
            sop_registry.validate(req.definition)
            return {"valid": True}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.delete("/sop/{name}")
    async def delete_sop(name: str):
        """Delete a SOP template."""
        sop = await sop_registry.get(name)
        if not sop:
            raise HTTPException(status_code=404, detail=f"SOP '{name}' not found")
        await sop_registry.delete(name)
        return {"status": "deleted"}

    return router


def _assert_acyclic(nodes: list[NodeDefinition]) -> None:
    """Reject cyclic SOP node dependencies early with a readable error."""
    by_id = {node.id: node for node in nodes}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str, path: list[str]) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            cycle = " -> ".join(path + [node_id])
            raise ValueError(f"node dependency cycle detected: {cycle}")
        visiting.add(node_id)
        for dep_id in by_id[node_id].depends_on:
            visit(dep_id, path + [node_id])
        visiting.remove(node_id)
        visited.add(node_id)

    for node in nodes:
        visit(node.id, [])
