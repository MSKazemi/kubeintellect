# app/api/v1/endpoints/tools.py
"""
Tool management endpoints.

Provides lifecycle management for runtime-generated tools, including
listing, status updates, and promoting a runtime tool to static tool format.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.services.tool_registry_service import ToolRegistryService
from app.services.tool_storage_service import ToolStorageService
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class ToolSummary(BaseModel):
    tool_id: str
    name: str
    description: str
    status: str
    created_at: str
    file_path: str


class PromoteToolResponse(BaseModel):
    tool_id: str
    tool_name: str
    suggested_filename: str
    promoted_code: str
    instructions: str


class UpdateStatusRequest(BaseModel):
    status: str
    reason: Optional[str] = None


class CreatePRResponse(BaseModel):
    tool_id: str
    tool_name: str
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    branch_name: Optional[str] = None
    status: str   # "created" | "skipped" | "error"
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_promoted_code(tool_meta: dict, raw_code: str) -> str:
    """
    Build static tools_lib-format code from registry metadata and raw function code.
    Extracted so both the promote endpoint and the create-pr endpoint can reuse it.
    """
    tool_name = tool_meta["name"]
    description = tool_meta["description"]
    function_name = tool_meta["function_name"]
    tool_id = tool_meta["tool_id"]
    pydantic_class = tool_meta.get("pydantic_class_name", f"{function_name.title().replace('_', '')}Input")
    instance_var = tool_meta.get("tool_instance_variable_name", f"{function_name}_tool")
    input_schema = tool_meta.get("input_schema", {})

    field_lines = []
    for param_name, param_info in input_schema.items():
        param_type = param_info.get("type", "str")
        param_desc = param_info.get("description", "")
        field_lines.append(
            f'    {param_name}: {param_type} = Field(..., description="{param_desc}")'
        )
    fields_block = "\n".join(field_lines) if field_lines else "    pass"

    return f'''\
# app/agents/tools/tools_lib/{tool_name}_tools.py
"""
Static tool: {tool_name}

Promoted from runtime-generated tool (ID: {tool_id}).
Original description: {description}
"""

from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool

from app.agents.tools.tools_lib._base import get_k8s_clients, handle_k8s_error
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


# ---------------------------------------------------------------------------
# Input schema
# ---------------------------------------------------------------------------

class {pydantic_class}(BaseModel):
{fields_block}


# ---------------------------------------------------------------------------
# Tool function
# (Extracted from runtime-generated code — review and adapt as needed)
# ---------------------------------------------------------------------------

{raw_code}


# ---------------------------------------------------------------------------
# Tool instance
# ---------------------------------------------------------------------------

{instance_var} = StructuredTool.from_function(
    func={function_name},
    name="{tool_name}",
    description="{description}",
    args_schema={pydantic_class},
)


# ---------------------------------------------------------------------------
# Exported list
# ---------------------------------------------------------------------------

{tool_name}_tools = [{instance_var}]
'''


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/tools",
    response_model=list[ToolSummary],
    tags=["Tools"],
    summary="List runtime-generated tools",
    description="Returns all runtime-generated tools, optionally filtered by status.",
)
async def list_tools(status: Optional[str] = None):
    """List all runtime tools from the registry."""
    try:
        registry = ToolRegistryService()
        tools = registry.list_tools(status=status)
        return [
            ToolSummary(
                tool_id=t["tool_id"],
                name=t["name"],
                description=t["description"],
                status=t["status"],
                created_at=t["created_at"],
                file_path=t["file_path"],
            )
            for t in tools
        ]
    except Exception as e:
        logger.error(f"Failed to list tools: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.patch(
    "/tools/{tool_id}/status",
    tags=["Tools"],
    summary="Update tool status",
    description="Set a tool's status to 'enabled', 'disabled', or 'deprecated'.",
)
async def update_tool_status(tool_id: str, body: UpdateStatusRequest):
    """Update the status of a runtime tool."""
    valid_statuses = {"enabled", "disabled", "deprecated"}
    if body.status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{body.status}'. Must be one of: {sorted(valid_statuses)}",
        )
    try:
        registry = ToolRegistryService()
        tool = registry.get_tool(tool_id)
        if not tool:
            raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found")

        if body.status == "deprecated":
            registry.deprecate_tool(tool_id, body.reason)
        else:
            registry.update_tool_status(tool_id, body.status, body.reason)

        return {"tool_id": tool_id, "status": body.status, "reason": body.reason}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update tool status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/tools/{tool_id}/promote",
    response_model=PromoteToolResponse,
    tags=["Tools"],
    summary="Promote a runtime tool to static tool format",
    description=(
        "Reads a runtime-generated tool's code and metadata, then reformats it as a proper "
        "static tools_lib/*.py file following KubeIntellect conventions (Input schema class, "
        "tool function, StructuredTool instance, exported list). Returns the formatted code "
        "ready for code review and merging into the static tool library."
    ),
)
async def promote_tool(tool_id: str):
    """
    Export a runtime-generated tool as a static tools_lib/*.py file.

    The returned code follows the project's static tool conventions and can be
    placed in app/agents/tools/tools_lib/<resource>_tools.py and imported in
    app/agents/tools/kubernetes_tools.py.
    """
    try:
        registry = ToolRegistryService()
        storage = ToolStorageService()

        tool_meta = registry.get_tool(tool_id)
        if not tool_meta:
            raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found")

        raw_code = storage.load_tool_file(tool_id)
        if not raw_code:
            raise HTTPException(
                status_code=404,
                detail=f"Tool file not found on PVC for tool '{tool_id}'",
            )

        tool_name = tool_meta["name"]
        tool_id = tool_meta["tool_id"]

        promoted_code = _build_promoted_code(tool_meta, raw_code)
        suggested_filename = f"{tool_name}_tools.py"

        instructions = (
            f"0. Deprecate the runtime tool (ID: {tool_id}) via PATCH /v1/tools/{tool_id}/status "
            f"(status: deprecated) to prevent it shadowing the static version before or after your PR merges.\n"
            f"1. Save this file to: app/agents/tools/tools_lib/{suggested_filename}\n"
            f"2. Review and adapt the function body — remove any runtime scaffolding if present.\n"
            f"3. Import in app/agents/tools/kubernetes_tools.py:\n"
            f"       from app.agents.tools.tools_lib.{tool_name}_tools import {tool_name}_tools\n"
            f"4. Add to the relevant agent's tool list in app/orchestration/workflow.py.\n"
            f"5. Once merged, the runtime tool (ID: {tool_id}) can be disabled or deleted."
        )

        logger.info(f"Promoted tool '{tool_name}' (ID: {tool_id}) to static format")

        return PromoteToolResponse(
            tool_id=tool_id,
            tool_name=tool_name,
            suggested_filename=suggested_filename,
            promoted_code=promoted_code,
            instructions=instructions,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to promote tool {tool_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/tools/{tool_id}/create-pr",
    response_model=CreatePRResponse,
    tags=["Tools"],
    summary="Create GitHub PR for a runtime tool",
    description=(
        "Generates the static tool code, creates a feature branch on GitHub, and opens a PR "
        "targeting GITHUB_PR_TARGET_BRANCH. PR metadata (url, number, status) is persisted back "
        "to the registry. Requires GITHUB_PR_ENABLED=true and valid GITHUB_TOKEN / GITHUB_REPO."
    ),
)
async def create_pr_for_tool(tool_id: str):
    """Manually trigger GitHub PR creation for a registered runtime tool."""
    from app.core.config import settings as _settings

    if not _settings.GITHUB_PR_ENABLED:
        return CreatePRResponse(
            tool_id=tool_id,
            tool_name="",
            status="skipped",
            message="GitHub PR creation is disabled. Set GITHUB_PR_ENABLED=true to enable.",
        )

    try:
        import asyncio
        from app.services.github_pr_service import create_github_pr_for_tool

        registry = ToolRegistryService()
        storage = ToolStorageService()

        tool_meta = registry.get_tool(tool_id)
        if not tool_meta:
            raise HTTPException(status_code=404, detail=f"Tool '{tool_id}' not found")

        raw_code = storage.load_tool_file(tool_id)
        if not raw_code:
            raise HTTPException(
                status_code=404,
                detail=f"Tool file not found on PVC for tool '{tool_id}'",
            )

        promoted_code = _build_promoted_code(tool_meta, raw_code)

        # PyGitHub is synchronous — run in a thread to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, create_github_pr_for_tool, tool_meta, promoted_code
        )

        tool_name = tool_meta["name"]

        if result.success:
            registry.update_pr_metadata(
                tool_id=tool_id,
                pr_url=result.pr_url,
                pr_number=result.pr_number,
                pr_status="open",
            )
            logger.info(f"PR #{result.pr_number} created for tool '{tool_name}': {result.pr_url}")
            return CreatePRResponse(
                tool_id=tool_id,
                tool_name=tool_name,
                pr_url=result.pr_url,
                pr_number=result.pr_number,
                branch_name=result.branch_name,
                status="created",
                message=f"PR #{result.pr_number} created: {result.pr_url}",
            )
        else:
            logger.error(f"PR creation failed for tool '{tool_name}': {result.error}")
            return CreatePRResponse(
                tool_id=tool_id,
                tool_name=tool_name,
                status="error",
                message=result.error or "PR creation failed — check server logs.",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in create-pr for tool {tool_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
