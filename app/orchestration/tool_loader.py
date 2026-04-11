# app/orchestration/tool_loader.py
"""
KubeIntellect tool loading.

Handles loading of both static Kubernetes tool categories and dynamically
generated runtime tools from PVC.
"""

import sys
import types

from langchain_core.tools import BaseTool

from app.agents.tools import code_generator_tools, kubernetes_tools
from app.utils.code_security import analyze_tool_code, compute_code_checksum
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")

# Maximum tools per LangChain/OpenAI call — hard limit imposed by the OpenAI API.
# See: https://platform.openai.com/docs/guides/function-calling
OPENAI_MAX_TOOLS_PER_CALL = 128


# ---------------------------------------------------------------------------
# Runtime (PVC) tool loading
# ---------------------------------------------------------------------------

def load_runtime_tools_from_pvc() -> list[BaseTool]:
    """
    Load all enabled runtime-generated tools from PVC.

    Returns:
        List of dynamically loaded tools from PVC
    """
    tools = []

    try:
        from app.services.tool_registry_service import ToolRegistryService
        from app.services.tool_storage_service import ToolStorageService

        registry_service = ToolRegistryService()
        storage_service = ToolStorageService()

        # Only load enabled tools — deprecated tools must not be passed to create_react_agent
        enabled_tools = registry_service.list_tools(status="enabled")
        tools_to_load = enabled_tools

        logger.info(
            f"Loading {len(enabled_tools)} enabled runtime tools from PVC..."
        )

        for tool_meta in tools_to_load:
            try:
                # Load tool code from PVC
                code = storage_service.load_tool_file(tool_meta["tool_id"])

                if not code:
                    logger.warning(f"Tool file not found for {tool_meta['name']} (ID: {tool_meta['tool_id']})")
                    continue

                # --- Security gate 1: checksum verification -----------------
                # Compare the SHA-256 of the file on disk against the hash
                # recorded in the registry at registration time.  A mismatch
                # means the file was modified after it was approved — skip it.
                stored_checksum = tool_meta.get("file_checksum")
                if stored_checksum:
                    actual_checksum = compute_code_checksum(code)
                    if actual_checksum != stored_checksum:
                        logger.error(
                            f"Checksum mismatch for tool '{tool_meta['name']}' "
                            f"(ID: {tool_meta['tool_id']}) — file may have been "
                            f"tampered with. Skipping load."
                        )
                        continue
                else:
                    logger.warning(
                        f"No stored checksum for tool '{tool_meta['name']}' "
                        f"(ID: {tool_meta['tool_id']}) — skipping integrity check "
                        f"(tool was registered before hardening was deployed)."
                    )

                # --- Security gate 2: static analysis -----------------------
                # Parse the code and reject any patterns that have no
                # legitimate purpose in a Kubernetes helper tool.
                is_safe, violations = analyze_tool_code(code)
                if not is_safe:
                    logger.error(
                        f"Static analysis blocked tool '{tool_meta['name']}' "
                        f"(ID: {tool_meta['tool_id']}): {violations}"
                    )
                    continue

                # Create a unique module name
                module_name = f"runtime_tool_{tool_meta['tool_id']}"

                # Create module and execute generated code in its namespace
                module = types.ModuleType(module_name)
                exec(code, module.__dict__)
                sys.modules[module_name] = module

                # Extract tool instance
                tool_var_name = tool_meta["tool_instance_variable_name"]
                if hasattr(module, tool_var_name):
                    tool = getattr(module, tool_var_name)
                    if isinstance(tool, BaseTool):
                        if tool_meta.get("status") == "deprecated":
                            tool.description = f"[DEPRECATED] {tool.description}"
                            logger.debug(f"Loaded deprecated tool: {tool_meta['name']}")
                        else:
                            logger.debug(f"Loaded tool: {tool_meta['name']}")
                        tools.append(tool)
                    else:
                        logger.warning(f"Tool variable {tool_var_name} is not a BaseTool")
                else:
                    logger.warning(f"Tool variable {tool_var_name} not found in module")

            except Exception as e:
                logger.error(f"Failed to load tool {tool_meta.get('name', 'unknown')}: {e}", exc_info=True)

        logger.info(f"Successfully loaded {len(tools)} runtime tools from PVC")

    except Exception as e:
        logger.error(f"Error loading runtime tools from PVC: {e}", exc_info=True)

    return tools


# ---------------------------------------------------------------------------
# Static tool category loading
# ---------------------------------------------------------------------------

def load_tool_category(tool_category_name: str, tool_list_attr: str) -> list[BaseTool]:
    """
    Load a specific category of tools with error handling and logging.

    Args:
        tool_category_name: Human-readable name of the tool category
        tool_list_attr: Attribute name in kubernetes_tools module

    Returns:
        List of tools for the category
    """
    try:
        tools = getattr(kubernetes_tools, tool_list_attr, [])
        logger.info(f"{tool_category_name} tools: {len(tools)} tools loaded")

        if len(tools) == 0:
            logger.warning(
                f"No {tool_category_name.lower()} tools available - "
                f"agent will report missing tools and trigger CodeGenerator"
            )

        return tools

    except AttributeError as e:
        logger.error(f"{tool_category_name} tools not found in kubernetes_tools.py: {e}")
        return []


def _deduplicate_tools(tools: list[BaseTool]) -> list[BaseTool]:
    """
    Remove duplicate tools based on tool name, keeping the first occurrence.

    Args:
        tools: List of tools that may contain duplicates

    Returns:
        List of unique tools
    """
    seen_names = set()
    unique_tools = []
    duplicates = []

    for tool in tools:
        if tool.name not in seen_names:
            seen_names.add(tool.name)
            unique_tools.append(tool)
        else:
            duplicates.append(tool.name)

    if duplicates:
        logger.warning(f"Removed {len(duplicates)} duplicate tools: {set(duplicates)}")
        logger.info(f"Tool count reduced from {len(tools)} to {len(unique_tools)} after deduplication")

    return unique_tools


def _cap_tools_to_limit(
    dynamic_tools: list[BaseTool],
    static_tools: list[BaseTool],
    limit: int = OPENAI_MAX_TOOLS_PER_CALL,
) -> list[BaseTool]:
    """
    Build the DynamicToolsExecutor tool list respecting the OpenAI 128-tool limit.

    Static tools win on name conflict — they are reviewed, tested, and version-controlled.
    Dynamic tools fill the remaining slots up to the limit.
    """
    # Deduplicate within each category first, then across them.
    deduped_dynamic = _deduplicate_tools(dynamic_tools)
    deduped_static = _deduplicate_tools(static_tools)

    # Static wins on name conflict — drop any dynamic tool whose name collides.
    static_names = {t.name for t in deduped_static}
    filtered_dynamic = [t for t in deduped_dynamic if t.name not in static_names]
    for t in deduped_dynamic:
        if t.name in static_names:
            logger.warning(
                f"Dynamic tool '{t.name}' dropped — conflicts with static tool of same name "
                f"(static always wins on name conflict)."
            )

    combined = filtered_dynamic + deduped_static
    if len(combined) > limit:
        dropped = combined[limit:]
        logger.warning(
            f"DynamicToolsExecutor: capping {len(combined)} tools to {limit} "
            f"(dropped {len(dropped)} static tools: "
            f"{[t.name for t in dropped[:5]]}{'...' if len(dropped) > 5 else ''})"
        )
        combined = combined[:limit]

    logger.info(
        f"DynamicToolsExecutor tool list: {len(combined)} tools "
        f"({len(deduped_dynamic)} dynamic + {len(combined) - len(deduped_dynamic)} static)"
    )
    return combined


def _log_tool_diagnostics(tool_categories: dict[str, list[BaseTool]]) -> None:
    """Log duplicate detection, counts, and availability summary for loaded tools."""
    try:
        logger.info(f"Main Static Tools: {len(tool_categories['main_static'])} tools loaded")
        logger.info(f"Dynamic Tools: {len(tool_categories['dynamic'])} tools loaded")

        dynamic_tool_names = {tool.name for tool in tool_categories['dynamic']}
        static_tool_names = {tool.name for tool in tool_categories['main_static']}
        duplicates = dynamic_tool_names & static_tool_names
        if duplicates:
            logger.warning(
                f"Found {len(duplicates)} duplicate tool names between dynamic and static tools: {duplicates}"
            )

        total_tools = len(tool_categories['dynamic']) + len(tool_categories['main_static'])
        logger.info(
            f"DynamicToolsExecutor will have {total_tools} tools "
            f"({len(tool_categories['dynamic'])} dynamic + {len(tool_categories['main_static'])} static)"
        )
        if total_tools > OPENAI_MAX_TOOLS_PER_CALL:
            logger.info(
                f"DynamicToolsExecutor raw count ({total_tools}) exceeds {OPENAI_MAX_TOOLS_PER_CALL} — "
                f"_cap_tools_to_limit() will drop {total_tools - OPENAI_MAX_TOOLS_PER_CALL} static tools at agent creation."
            )

        logger.info(f"Tool availability: {kubernetes_tools.get_tool_availability_status()}")
        summary = kubernetes_tools.print_tool_summary()
        if summary:
            logger.info(f"Tool summary: {summary}")
    except Exception as e:
        logger.error(f"Error logging tool diagnostics: {e}")


def load_all_tool_categories() -> dict[str, list[BaseTool]]:
    """Load all tool categories and return as a dictionary."""
    tool_categories = {
        'dynamic': load_runtime_tools_from_pvc(),
        'main_static': getattr(kubernetes_tools, 'all_k8s_tools', []),
        'code_gen': [code_generator_tools.code_generator_tool],
        'apply': load_tool_category('Apply', 'k8s_apply_tools'),
        'logs': load_tool_category('Logs', 'k8s_logs_tools'),
        'configs': load_tool_category('Config', 'k8s_config_tools'),
        'metrics': load_tool_category('Metrics', 'k8s_metrics_tools'),
        'security': load_tool_category('Security', 'k8s_security_tools'),
        'rbac': load_tool_category('RBAC', 'k8s_rbac_tools'),
        'lifecycle': load_tool_category('Lifecycle', 'k8s_lifecycle_tools'),
        'execution': load_tool_category('Execution', 'k8s_execution_tools'),
        'deletion': load_tool_category('Deletion', 'k8s_deletion_tools'),
        'advancedops': load_tool_category('AdvancedOps', 'k8s_advancedops_tools')
    }
    _log_tool_diagnostics(tool_categories)
    return tool_categories
