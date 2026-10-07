"""Validate explicit workflow drafts without issuing cards or running workers."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from .db import db
from .security import authorize_management, fail, session
from .task_flow_definition import MAX_FIELDS, MAX_TRANSITIONS, validate_definition

router = APIRouter()


class FlowProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["manual", "script", "webhook"] = "manual"
    parameters: list[dict] = Field(default_factory=list, max_length=MAX_FIELDS)
    outputs: list[dict] = Field(default_factory=list, max_length=MAX_FIELDS)


class FlowDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    definition: dict | None
    product: FlowProduct


def validate_draft(body, request, roles):
    authorization = session(request, roles)
    with db() as c:
        authorize_management(c, authorization, "fulfillment.configure")
    try:
        definition = validate_definition(body.definition, body.product.model_dump())
    except ValueError as exc:
        # The canonical validator uses fixed diagnostics, never submitted values.
        fail(str(exc), 422)
    nodes = definition["nodes"] if definition else []
    return {
        "ok": True,
        "definition": definition,
        "summary": {
            "version": definition["version"] if definition else None,
            "entry": definition["entry"] if definition else None,
            "node_count": len(nodes),
            "nodes": {
                kind: sum(node["kind"] == kind for node in nodes)
                for kind in ("input", "process", "display", "end")
            },
            "transition_limit": MAX_TRANSITIONS,
        },
    }


@router.post("/api/admin/task-flows/validate")
def owner_validate(body: FlowDraft, request: Request):
    return validate_draft(body, request, ("admin",))


@router.post("/api/manage/task-flows/validate")
def operator_validate(body: FlowDraft, request: Request):
    return validate_draft(body, request, ("admin", "staff"))
