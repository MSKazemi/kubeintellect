# app/agents/tools/code_execution_tools.py
import re
import json
from time import sleep
from langchain_core.tools import StructuredTool
from langchain_core.runnables import RunnableConfig
from langchain_experimental.utilities.python import PythonREPL
from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field, ValidationError
from typing import List, Dict, Any, Optional
from app.utils.logger_config import setup_logging
from app.core.llm_gateway import get_code_gen_llm as get_llm
from app.core.config import settings
logger = setup_logging(app_name="kubeintellect")

# Langfuse @observe — gracefully no-ops when Langfuse is disabled or not installed.
try:
    from langfuse import observe as _lf_observe
except ImportError:
    def _lf_observe(*args, **kwargs):  # type: ignore[misc]
        """Fallback no-op decorator when langfuse is not installed."""
        def _dec(fn):
            return fn
        return _dec if args and callable(args[0]) else _dec

# =====================================================================================
# Initialize LLM
# =====================================================================================
llm = get_llm()
if not hasattr(llm, "invoke"):
    raise RuntimeError("LLM failed to initialize (check API keys / endpoint)")

# =====================================================================================
# Python REPL Tool
# =====================================================================================
python_repl_utility = PythonREPL()

class PythonREPLInput(BaseModel):
    code: str = Field(description="The Python code to execute. Can be a single line or multiple lines of valid Python.")

_REPL_TIMEOUT_SECONDS = 30


@_lf_observe(name="python_repl_execution", as_type="tool")
def _run_python_repl(code: str) -> str:
    """Execute Python code in the REPL and return stdout output.

    Decorated with @observe so each REPL execution appears as a named 'tool' span
    in Langfuse nested under the CodeGenerator agent's trace.
    """
    import concurrent.futures

    logger.info(f"Executing Python code in REPL:\n---\n{code}\n---")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(python_repl_utility.run, code)
            try:
                result = future.result(timeout=_REPL_TIMEOUT_SECONDS)
            except concurrent.futures.TimeoutError:
                logger.error(f"REPL execution timed out after {_REPL_TIMEOUT_SECONDS}s")
                return (
                    f"Error executing Python code:\n```python\n{code}\n```\n"
                    f"ErrorType: TimeoutError\n"
                    f"Message: Execution exceeded {_REPL_TIMEOUT_SECONDS}s limit and was aborted."
                )

        if result is None:
            result_str = "[No explicit print output captured or an internal None was returned]"
            logger.info("Python REPL execution returned None.")
        elif isinstance(result, str) and result.strip() == "":
            result_str = "[No explicit print output captured (stdout was empty or whitespace)]"
            logger.info("Python REPL execution produced no visible standard output.")
        else:
            result_str = f"\n```text\n{result}\n```"

        return (
            f"Code executed:\n```python\n{code}\n```\n"
            f"Stdout:{result_str}"
        )
    except Exception as e:
        logger.error(f"Python REPL execution failed for code:\n{code}\nError: {e}", exc_info=True)
        return (
            f"Error executing Python code:\n```python\n{code}\n```\n"
            f"ErrorType: {type(e).__name__}\n"
            f"Message: {str(e)}"
        )

python_repl_tool = StructuredTool.from_function(
    func=_run_python_repl,
    name="execute_python_code",
    description=(
        "A Python REPL that executes one or more lines of Python code and returns its standard output (stdout). "
        "Use this for testing code snippets, performing calculations, or running dynamic Python scripts. "
        "Input is a dictionary with a 'code' field containing the Python string to execute. "
        "Ensure the code is complete and valid Python."
    ),
    args_schema=PythonREPLInput
)

# =====================================================================================
# AI Generated Tool Registration Tool
# =====================================================================================
class ToolParameter(BaseModel):
    name: str = Field(description="Name of the parameter for the generated function.")
    type_hint: str = Field(description="Python type hint string for the parameter (e.g., 'str', 'int', 'Optional[str]', 'List[Dict[str, Any]]').")
    description: str = Field(description="Description of the parameter for the Pydantic model's Field.")

class RegisterAIToolInput(BaseModel):
    generated_function_code: str = Field(description="The complete Python code string for the new function.")
    function_name: str = Field(description="The exact name of the function defined in 'generated_function_code'.")
    pydantic_class_name: str = Field(description="Desired CamelCase name for the Pydantic input schema class (e.g., 'GetPodLogsInputSchema').")
    tool_instance_variable_name: str = Field(description="Desired Python variable name for the LangChain StructuredTool instance (e.g., 'get_pod_logs_tool_instance').")
    langchain_tool_name: str = Field(description="The 'name' attribute for the LangChain tool (e.g., 'kubernetes_get_pod_logs').")
    langchain_tool_description: str = Field(description="The 'description' attribute for the LangChain tool.")
    parameters: List[ToolParameter] = Field(default_factory=list, description="Parameters for the function, used for Pydantic schema. Empty if no arguments.")
    target_file_path: str = Field(
        default="app/agents/tools/ai_generated_tools/ai_generated_tools.py",
        description="Path to the Python file where AI-generated tools are stored."
    )

# Register AI Generated Tool
# =====================================================================================
def _register_ai_generated_tool(
    generated_function_code: str, 
    function_name: str, 
    pydantic_class_name: str,
    tool_instance_variable_name: str, 
    langchain_tool_name: str, 
    langchain_tool_description: str,
    parameters: List[ToolParameter], 
    target_file_path: str  # Kept for backward compatibility, but now uses PVC
    ) -> str:
    """
    Register a new AI-generated tool to PVC storage.
    
    This function now uses the PVC-based storage system instead of writing
    to the ephemeral container filesystem.
    """
    logger.info(f"Registering new AI-generated tool: {langchain_tool_name} (using PVC storage)")
    
    try:
        # Import services
        from app.services.tool_registry_service import ToolRegistryService
        from app.services.tool_storage_service import ToolStorageService
        from datetime import datetime
        import os
        
        # Initialize services
        registry_service = ToolRegistryService()
        storage_service = ToolStorageService()
        
        # Check for name conflicts
        existing_tool = registry_service.get_tool_by_name(langchain_tool_name)
        if existing_tool:
            logger.info(f"Tool '{langchain_tool_name}' already exists in registry. Returning as already registered.")
            return f"Tool '{langchain_tool_name}' is already registered and ready to use."
        
        # Generate tool ID
        tool_id = registry_service._generate_tool_id(langchain_tool_name, generated_function_code)

        # ── AST + Self-Refine + Safeguard pipeline ──────────────────────────
        try:
            from app.utils.ast_validator import validate_k8s_api_calls
            unknown_calls = validate_k8s_api_calls(generated_function_code)
            if unknown_calls:
                logger.warning(
                    f"AST validation found {len(unknown_calls)} unknown k8s client call(s) "
                    f"in '{langchain_tool_name}': {unknown_calls}. "
                    f"Self-refine will attempt to fix these."
                )
        except Exception as _ast_err:
            logger.warning(f"AST validation skipped (non-fatal): {_ast_err}")

        try:
            from app.utils.self_refine import self_refine_code, safeguard_review
            refined_code, feedback_notes = self_refine_code(
                generated_function_code,
                langchain_tool_description,
                llm,
            )
            if refined_code != generated_function_code:
                logger.info(
                    f"Self-refine updated code for '{langchain_tool_name}'. "
                    f"Notes: {feedback_notes}"
                )
                generated_function_code = refined_code
            else:
                logger.info(f"Self-refine: no changes needed for '{langchain_tool_name}'.")
        except Exception as _refine_err:
            logger.warning(f"Self-refine skipped for '{langchain_tool_name}' (non-fatal): {_refine_err}")

        try:
            from app.utils.self_refine import safeguard_review
            is_flagged, risk_annotations = safeguard_review(
                generated_function_code,
                langchain_tool_description,
            )
            if is_flagged:
                logger.warning(
                    f"Safeguard flagged '{langchain_tool_name}' with "
                    f"{len(risk_annotations)} annotation(s): {risk_annotations}"
                )
                safeguard_prefix = "\n".join(
                    f"⚠️ SAFEGUARD: {ann}" for ann in risk_annotations
                )
                langchain_tool_description = (
                    f"{safeguard_prefix}\n\n{langchain_tool_description}"
                )
        except Exception as _sg_err:
            logger.warning(f"Safeguard review skipped for '{langchain_tool_name}' (non-fatal): {_sg_err}")
        # ── End pipeline ─────────────────────────────────────────────────────

        # Build Pydantic schema code
        pydantic_schema_code = f"\n\nclass {pydantic_class_name}(BaseModel):\n"
        if not parameters:
            pydantic_schema_code += "    pass # This tool takes no arguments\n"
        else:
            for param in parameters:
                clean_param_description = param.description.replace('"', '\\"').replace('\n', ' ')
                pydantic_schema_code += f"    {param.name}: {param.type_hint} = Field(description=\"{clean_param_description}\")\n"
        
        # Build tool instantiation code
        clean_tool_description = langchain_tool_description.replace('"', '\\"').replace('\n', '\\n')
        tool_instantiation_code = (
            f"\n{tool_instance_variable_name} = StructuredTool.from_function(\n"
            f"    func={function_name},\n"
            f"    name=\"{langchain_tool_name}\",\n"
            f"    description=\"{clean_tool_description}\","
            f"\n    args_schema={pydantic_class_name}\n)\n"
        )
        
        # Build complete tool code with imports
        full_tool_code = (
            f"# Generated tool: {langchain_tool_name}\n"
            f"# Tool ID: {tool_id}\n"
            f"# Generated at: {datetime.utcnow().isoformat()}\n\n"
            f"from langchain_core.tools import StructuredTool\n"
            f"from pydantic import BaseModel, Field\n"
            f"from typing import List, Dict, Any, Optional\n"
            f"import json\n"
            f"from app.services import kubernetes_service\n"
            f"from app.utils.logger_config import setup_logging\n\n"
            f"logger = setup_logging(app_name=\"kubeintellect\")\n\n"
            f"{generated_function_code}\n"
            f"{pydantic_schema_code}\n"
            f"{tool_instantiation_code}\n"
        )
        
        # Save tool file to PVC
        file_path = storage_service.save_tool_file(tool_id, full_tool_code)

        # Compute checksum immediately after write so the registry holds a
        # trusted hash that can be verified before exec() at load time.
        file_checksum = storage_service.compute_file_checksum(tool_id)

        # Register metadata in registry
        metadata = {
            "tool_id": tool_id,
            "name": langchain_tool_name,
            "description": langchain_tool_description,
            "file_path": file_path,
            "file_checksum": file_checksum,
            "function_name": function_name,
            "pydantic_class_name": pydantic_class_name,
            "tool_instance_variable_name": tool_instance_variable_name,
            "input_schema": {p.name: {"type": p.type_hint, "description": p.description} for p in parameters},
            "output_schema": {"type": "dict"},
            "created_at": datetime.utcnow().isoformat(),
            "base_app_version": os.getenv("APP_VERSION", "unknown"),
            "status": "enabled",
            "created_by": "runtime",
            "code": generated_function_code  # Store for tool_id generation
        }
        
        registry_service.register_tool(metadata)
        
        logger.info(f"Successfully registered tool '{langchain_tool_name}' with ID {tool_id} to PVC")
        
        # Try to reload tools into the agent immediately
        try:
            from app.orchestration.workflow import reload_dynamic_tools_into_agent
            if reload_dynamic_tools_into_agent():
                logger.info(f"Tool '{langchain_tool_name}' has been loaded and is now available for use")

                # Optional: fire-and-forget GitHub PR creation if auto-create is enabled.
                # Wrapped in a broad except so PR failures never block tool registration.
                if settings.GITHUB_PR_AUTO_CREATE and settings.GITHUB_PR_ENABLED:
                    try:
                        import asyncio
                        from app.services.github_pr_service import create_github_pr_for_tool
                        from app.api.v1.endpoints.tools import _build_promoted_code

                        _tool_id = tool_id  # capture for closure

                        async def _fire_pr() -> None:
                            try:
                                _reg = registry_service.__class__()
                                _meta = _reg.get_tool(_tool_id)
                                if not _meta:
                                    logger.warning(f"PR auto-create: tool {_tool_id} not found after registration")
                                    return
                                _raw = storage_service.load_tool_file(_tool_id) or ""
                                _promoted = _build_promoted_code(_meta, _raw)
                                _result = await asyncio.get_event_loop().run_in_executor(
                                    None, create_github_pr_for_tool, _meta, _promoted
                                )
                                if _result.success:
                                    _reg.update_pr_metadata(_tool_id, _result.pr_url, _result.pr_number, "open")
                                    logger.info(f"Auto-created PR #{_result.pr_number} for tool '{langchain_tool_name}': {_result.pr_url}")
                                else:
                                    logger.warning(f"Auto PR creation failed for '{langchain_tool_name}': {_result.error}")
                            except Exception as inner_err:
                                logger.warning(f"Error inside PR auto-create coroutine: {inner_err}")

                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.ensure_future(_fire_pr())
                        else:
                            loop.run_until_complete(_fire_pr())
                    except Exception as pr_err:
                        logger.warning(f"PR auto-creation setup failed (non-fatal): {pr_err}")

                return (
                    f"Tool '{langchain_tool_name}' (ID: {tool_id}) "
                    f"registered successfully and is now available for use. "
                    f"File saved to {file_path} on PVC."
                )
            else:
                logger.warning(f"Tool '{langchain_tool_name}' registered but could not be reloaded immediately")
        except Exception as e:
            logger.warning(f"Could not reload tools after registration: {e}. Tool will be available after restart.")
        
        return (
            f"Tool '{langchain_tool_name}' (ID: {tool_id}) "
            f"registered successfully. File saved to {file_path} on PVC. "
            f"Tool will be available after application reload."
        )
        
    except ValueError as e:
        # Name conflict error
        logger.warning(f"Tool registration failed due to conflict: {e}")
        return f"Error: {str(e)}"
    except Exception as e:
        logger.error(f"Failed to register tool: {e}", exc_info=True)
        return f"Error: Could not complete tool registration. Details: {str(e)}"

register_ai_tool_definer = StructuredTool.from_function(
    func=_register_ai_generated_tool,
    name="register_new_ai_generated_tool",
    description=(
        "Takes details of a new Python function (code, name, parameters, desired LangChain tool attributes). "
        "Generates Pydantic input schema and LangChain StructuredTool code, then registers the tool to PVC "
        "via ToolRegistryService. The tool is immediately available to DynamicToolsExecutor after registration."
    ),
    args_schema=RegisterAIToolInput
)

# =====================================================================================
# Code Generation System Prompt
# =====================================================================================

CODE_GENERATOR_SYSTEM_PROMPT = """
You are a dedicated and meticulous Python code generation assistant, specializing in creating new LangChain tools for Kubernetes operations.
Your primary goal is to develop useful, robust, and safe Python functions that can be formally registered as LangChain StructuredTools.

**VERY IMPORTANT OUTPUT FORMATTING:**
Your entire response MUST BE ONLY the Python code itself, enclosed in a single Markdown code block starting with ```python and ending with ```.
Do NOT include ANY conversational preamble, explanations, apologies, introductory sentences, or any other text outside of this single Python code block.
The Python script you provide must be complete and directly executable by a Python REPL.

**IMPORTANT INSTRUCTIONS FOR THE PYTHON SCRIPT CONTENT:**
1.  **Kubernetes Client Library:** ALWAYS generate Python code that utilizes the `kubernetes` client library. Do NOT suggest or use raw `kubectl` commands.
2.  **Imports Location (CRITICAL FOR EXECUTION ENVIRONMENT):**
    * Place necessary imports like `import json`, `from kubernetes import client`, `from kubernetes import config` *INSIDE* each function that requires them, ideally at the beginning of the function's main `try` block. This helps ensure they are available in the function's scope within the testing environment.
    * A global `import json` at the very top of the script may also be needed if the final `print()` statement directly uses `json.dumps()` on a dictionary returned by your function.
3.  **Function Definition:** The script MUST define a Python function that encapsulates the core logic.
4.  **Error Handling within the Function:** Implement robust error handling using try-except blocks *within the generated function*.
    * Catch specific exceptions like `kubernetes.client.exceptions.ApiException`, `ImportError` (if an import inside the function fails), and generic `Exception`.
    * The function should return a Python dictionary. If an error occurs, this dictionary should detail the error (e.g., `{"status": "error", "message": "Descriptive error details", "error_type": "SpecificErrorName"}`).
5.  **Successful Output from the Function:** For successful operations, the function should return a Python dictionary containing the results (e.g., `{"status": "success", "data": {...}}`). The data must be JSON serializable.
6.  **Function and Delimiters (CODE STRUCTURE CRITICAL):**
    * The Python function definition MUST be wrapped with clear delimiters:
        `# --- START FUNCTION ---`
        [your function definition here, starting with "def function_name(...)"]
        `# --- END FUNCTION ---`
7.  **Example Call (Outside Function, End of Script):**
    * AFTER the `# --- END FUNCTION ---` delimiter, the script MUST include an example call to the defined function.
    * This call should be wrapped in `print(json.dumps(your_function_call(...)))`. The `json.dumps()` here is critical to ensure the final output to `stdout` is a valid JSON string. The function itself should return a dictionary.
    * Use valid, illustrative arguments for the example function call.
8.  **Code Purity:** Ensure the generated Python code uses only standard printable ASCII characters, especially for whitespace and indentation. Avoid non-breaking spaces (`\xa0`) or other unusual Unicode characters.
9.  **Idempotency & Re-generation (Learning from Feedback):** If you are asked to revise code due to feedback, analyze the feedback VERY CAREFULLY. Pay close attention to any reported Python errors (`NameError`, `SyntaxError`, `ImportError`, `TypeError`), JSON formatting issues, or missing delimiters/imports. Ensure the new code directly addresses and corrects these specific reported issues. Do not repeat previous mistakes.
10. **Function Naming:** Generate a descriptive Python function name (snake_case) for the defined function.
11. **Generality and Reusability (CRITICAL):** Functions MUST accept runtime values as parameters — NEVER hardcode specific resource names, namespace names, pod names, deployment names, label selectors, container names, or any other user-specific values inside the function body. Even when the user's request mentions a specific value (e.g., "show pods in namespace 'production'"), treat that as an *example* and make it a proper function parameter instead. Use a sensible default (e.g., `namespace: str = "default"`) only when a parameter is truly optional. The purpose of generating a tool is to create a **reusable, general-purpose function** — not a one-off script.
12. **Kubernetes Python Client API Notes (CRITICAL — avoid common mistakes):**
    * **HorizontalPodAutoscaler (HPA):** Use the `autoscaling/v2` API. The correct classes are `client.V2HorizontalPodAutoscaler`, `client.V2HorizontalPodAutoscalerSpec`, `client.V2MetricSpec`, `client.V2ResourceMetricSource`, `client.V2MetricTarget`. The autoscaling API object is obtained with `client.AutoscalingV2Api()`. Do NOT use `V1MetricSpec` or `V1HorizontalPodAutoscalerSpec` — these do not exist in the current client.
    * **ResourceQuota:** Use `client.CoreV1Api()` with `create_namespaced_resource_quota()`. Spec fields: `client.V1ResourceQuotaSpec(hard={"requests.cpu": "4", "requests.memory": "4Gi", "pods": "20"})`.
    * **LimitRange:** Use `client.CoreV1Api()` with `create_namespaced_limit_range()`. Spec: `client.V1LimitRangeSpec(limits=[client.V1LimitRangeItem(type="Container", default={"cpu": "500m"}, default_request={"cpu": "100m"})])`.
    * **NetworkPolicy:** Use `client.NetworkingV1Api()`.
    * **CronJob/Job:** Use `client.BatchV1Api()` for both CronJobs and Jobs.
    * **PersistentVolumeClaim (PVC):** Use `client.CoreV1Api()` with `create_namespaced_persistent_volume_claim()`.
    * **ServiceAccount:** Use `client.CoreV1Api()` with `create_namespaced_service_account()`.
    * **Role/ClusterRole/RoleBinding:** Use `client.RbacAuthorizationV1Api()`.
    * **Deployment rollout:** Use `client.AppsV1Api()`. For rollback, patch the deployment's `spec.template` annotations.
    * **Idempotency:** Always check if a resource already exists (try `read_namespaced_*`, catch `ApiException` with `e.status == 404` to create, or `e.status == 409` to skip/update).

Focus on the user's request to determine the function's parameters and logic, while strictly adhering to all instructions above.

**EXAMPLE OF YOUR ENTIRE RESPONSE (should be only this code block):**
```python
import json # For the final print(json.dumps(...)) call ONLY.

# --- START FUNCTION ---
def get_kubernetes_pods_example(namespace: str):
    # Imports are INSIDE the function for robustness in the REPL environment
    import json
    from kubernetes import client, config
    try:
        # Attempt to load Kubernetes config (local or in-cluster)
        try:
            config.load_kube_config()
        except config.ConfigException:
            try:
                config.load_incluster_config()
            except config.ConfigException as e:
                return {"status": "error", "message": f"Could not load Kubernetes config: {str(e)}", "error_type": "K8sConfigError"}

        v1 = client.CoreV1Api()
        pod_list = v1.list_namespaced_pod(namespace=namespace, timeout_seconds=10)
        pods = [pod.metadata.name for pod in pod_list.items]
        return {"status": "success", "data": pods} # Return a dictionary
    except client.exceptions.ApiException as e:
        error_message = f"K8s API error: {e.reason} (status: {e.status})"
        # Attempt to get more details from the error body
        if e.body:
            try:
                # This internal json.loads requires 'import json' inside the function scope
                error_details = json.loads(e.body)
                error_message += f" Details: {error_details.get('message', e.body)}"
            except json.JSONDecodeError:
                pass # If body is not JSON, use the original ApiException message
        return {"status": "error", "message": error_message, "error_type": "ApiException"}
    except ImportError as ie:
        return {"status": "error", "message": f"Import error inside function: {str(ie)}", "error_type": "ImportError"}
    except Exception as e:
        return {"status": "error", "message": str(e), "error_type": type(e).__name__}
# --- END FUNCTION ---

# Example call: The function's dictionary output is passed to json.dumps here.
# The global 'import json' at the top of the script makes 'json.dumps' available for this print statement.
# print(json.dumps(get_kubernetes_pods_example(namespace="default")))
"""



# =====================================================================================
# Metadata Generator System Prompt
# =====================================================================================
METADATA_GENERATOR_SYSTEM_PROMPT = """
You are an expert in creating metadata for LangChain tools.
Given the user's request, the generated Python function code, and its successful test output, generate the following metadata:
- function_name (string): The exact Python name of the function.
- pydantic_class_name (string): A descriptive CamelCase name for the Pydantic input schema class (e.g., 'GetKubernetesPodsInputSchema'). If no arguments, use 'NoArgumentsInputSchema'.
- tool_instance_variable_name (string): A Python variable name for the LangChain StructuredTool instance (e.g., 'get_kubernetes_pods_tool').
- langchain_tool_name (string): A unique, descriptive snake_case name for the tool (e.g., 'kubernetes_list_pods_in_namespace').
- langchain_tool_description (string): A clear, detailed description of what the tool does, its purpose, all its parameters, what it returns, and any important usage notes.
- parameters (list of dicts): Each dict with 'name', 'type_hint', 'description'. Empty list if no arguments.

Return ONLY a valid JSON object containing these fields.
Example for parameters:
[
  {"name": "namespace", "type_hint": "str", "description": "The Kubernetes namespace."},
  {"name": "all_namespaces", "type_hint": "bool", "description": "List from all namespaces."}
]
If no parameters, parameters should be [].
"""

# =====================================================================================
# Tool Generation State
# =====================================================================================

class ToolGenerationState(BaseModel):
    user_request: str = Field(..., description="The user's request to generate a new Kubernetes tool.")
    generated_code: Optional[str] = Field(None, description="The full generated code including imports, function, and metadata.")
    generated_function_only_code: Optional[str] = Field(None, description="The extracted clean function only (without extras).")
    code_to_test: Optional[str] = Field(None, description="The generated code including example/test invocation.")
    test_output: Optional[str] = Field(None, description="The stdout or error result from executing the code.")
    test_evaluation: Optional[str] = Field(None, description="Test result classification: 'success', 'failure', or 'error_in_script'.")
    error_feedback_for_codegen: Optional[str] = Field(None, description="Specific test failure message to guide re-generation.")
    code_generation_attempts: int = Field(0, description="Current attempt count for code generation.")
    max_code_generation_attempts: int = Field(3, description="Maximum allowed code generation attempts before giving up.")
    # Metadata fields based on your RegisterAIToolInput
    function_name: Optional[str] = Field(None, description="The name of the generated Python function.")
    pydantic_class_name: Optional[str] = Field(None, description="The Pydantic input class name.")
    tool_instance_variable_name: Optional[str] = Field(None, description="The variable name for the LangChain StructuredTool instance.")
    langchain_tool_name: Optional[str] = Field(None, description="The tool name used by LangChain agents.")
    langchain_tool_description: Optional[str] = Field(None, description="Detailed description of the tool's purpose and parameters.")
    parameters: Optional[List[Dict[str, str]]] = Field(None, description="List of parameter dicts with name, type_hint, and description.")

    registration_args: Optional[RegisterAIToolInput] = Field(None, description="Final metadata used for registering the tool.")
    registration_status: Optional[str] = Field(None, description="Status of the registration: success or error message.")
    final_message: Optional[str] = Field(None, description="The final message output of the workflow.")
    error_message: Optional[str] = Field(None, description="The error message output of the workflow.")



# =====================================================================================
# Generate Code Node
# =====================================================================================
def node_generate_code(state: ToolGenerationState) -> ToolGenerationState:
    global logger, llm # Assuming they are initialized globally

    logger.info(f"--- Node: Generate Code (Attempt {state.code_generation_attempts + 1} / {state.max_code_generation_attempts}) ---")
    user_request = state.user_request
    attempts = state.code_generation_attempts
    feedback = state.error_feedback_for_codegen
    
    if attempts == 0:
        sleep(0)
    
    elif attempts == 1:
        sleep(1)
    
    elif attempts == 2:
        sleep(5)
    
    elif attempts == 3:
        sleep(15)
    
    else:
        sleep(30)
    
    
    
    
    prompt_content = f"User request: {user_request}\n"
    if feedback:
        prompt_content += f"\nIMPORTANT: This is a retry. Your previous attempt failed. \nFeedback: {feedback}\n"
        prompt_content += "Please carefully review the feedback and the requirements to generate a corrected Python script. Ensure your response contains ONLY the ```python ... ``` code block.\n"
    prompt_content += "\nGenerate the complete Python script (function definition + a `print()` call that outputs a JSON string) as per the system instructions. Your entire response must be only the Python code block."

    messages = [
        ("system", CODE_GENERATOR_SYSTEM_PROMPT), # Ensure this prompt strongly asks for code-only output
        ("human", prompt_content),
    ]

    raw_llm_response_content: Optional[str] = None
    llm_error_message: Optional[str] = None
    final_code_to_test: Optional[str] = None

    try:
        if not llm or not hasattr(llm, 'invoke'):
            raise ValueError("LLM instance is not initialized or does not have an 'invoke' method.")

        response = llm.invoke(messages)
        raw_llm_response_content = response.content
        logger.info("LLM invocation successful.")

    except Exception as e:
        llm_error_message = f"LLM invocation failed: {str(e)}"
        logger.error(llm_error_message, exc_info=True)
        # No code was generated, proceed to return failure state

    if raw_llm_response_content and isinstance(raw_llm_response_content, str):
        # 1. Clean common problematic characters
        cleaned_llm_output = raw_llm_response_content.replace('\xa0', ' ')
        logger.info("Cleaned non-breaking spaces from LLM output.")
        logger.debug(f"LLM Raw Output (after initial cleaning):\n{cleaned_llm_output}")

        # 2. Extract Python code block
        code_block_match = re.search(r"```(?:python\n)?(.*?)```", cleaned_llm_output, re.DOTALL)
        if code_block_match:
            extracted_code = code_block_match.group(1).strip()
            if extracted_code: # Ensure extracted code is not empty
                final_code_to_test = extracted_code
                logger.info("Successfully extracted Python code block using Markdown fences.")
            else:
                llm_error_message = "LLM returned a ```python``` block, but it was empty after stripping."
                logger.error(llm_error_message)
        else:
            # Fallback: If no Markdown block, check if the entire output might be raw code
            # This heuristic is risky; ideally, the LLM always uses Markdown fences as instructed.
            stripped_output = cleaned_llm_output.strip()
            if (stripped_output.startswith("import ") or
                stripped_output.startswith("from ") or
                stripped_output.startswith("# --- START FUNCTION ---") or # Your delimiter
                stripped_output.startswith("def ")):
                logger.warning("No explicit ```python ... ``` block found. Assuming the entire cleaned output is raw code (this is risky).")
                final_code_to_test = stripped_output
            else:
                llm_error_message = "LLM output did not contain a recognizable Python code block (e.g., ```python ... ```) and doesn't appear to be raw code."
                logger.error(f"{llm_error_message} LLM Output was:\n{cleaned_llm_output}")
    elif not raw_llm_response_content and not llm_error_message: # LLM returned None or empty string without throwing an exception
        llm_error_message = "LLM returned empty or invalid content (None or empty string)."
        logger.error(llm_error_message)

    # 3. Check if we have usable code
    if not final_code_to_test:
        # This handles LLM call failure, empty response, or failure to extract/identify code
        current_error = llm_error_message or "Failed to obtain or extract valid Python code from LLM."
        logger.error(f"No usable code generated or extracted. Error: {current_error}")
        return ToolGenerationState(
            user_request=user_request,
            generated_code=None,
            code_to_test=None,
            test_evaluation="error_in_script",
            error_feedback_for_codegen=current_error,
            code_generation_attempts=attempts + 1,
            final_message=f"Critical failure in code generation: {current_error}",
            test_output=None,
            generated_function_only_code=None,
            max_code_generation_attempts=state.max_code_generation_attempts
        )

    logger.info(f"Code prepared for testing (attempt {attempts + 1}):\n{final_code_to_test}")

    return ToolGenerationState(
        user_request=user_request,
        generated_code=raw_llm_response_content,
        code_to_test=final_code_to_test,
        code_generation_attempts=attempts + 1,
        error_feedback_for_codegen=None,
        test_output=None,
        test_evaluation=None,
        generated_function_only_code=None,
        max_code_generation_attempts=state.max_code_generation_attempts
    )

# =====================================================================================
# Test Code Node
# =====================================================================================
def node_test_code(state: ToolGenerationState) -> ToolGenerationState:
    global logger # Assuming logger is globally available
    logger.info("--- Node: Test Code ---")
    code_to_test = state.code_to_test

    if not code_to_test: # This check is important if node_generate_code could return None for code_to_test
        logger.error("No code provided to test_code node. This might be due to an earlier LLM failure.")
        return ToolGenerationState(
            user_request=state.user_request,
            test_output="Error: No code was provided to the testing node.",
            test_evaluation="error_in_script",
            error_feedback_for_codegen=state.error_feedback_for_codegen or "Code generation failed to produce output.",
            code_generation_attempts=state.code_generation_attempts,
            max_code_generation_attempts=state.max_code_generation_attempts
        )

    global python_repl_tool

    logger.info(f"Executing code in REPL. Length: {len(code_to_test)} chars.")
    logger.debug(f"Code for REPL:\n{code_to_test}")
    try:
        tool_output = python_repl_tool.invoke({"code": code_to_test})
        logger.info("Python REPL tool invoked successfully.")
        logger.debug(f"Raw test output from REPL: {tool_output}")
    except Exception as e:
        logger.error(f"Error invoking python_repl_tool: {e}", exc_info=True)
        return ToolGenerationState(
            user_request=state.user_request,
            test_output=f"Fatal Error: Failed to invoke the Python REPL tool itself: {str(e)}",
            test_evaluation="error_in_script",
            error_feedback_for_codegen="The code testing environment (REPL tool) encountered an unexpected error.",
            code_generation_attempts=state.code_generation_attempts,
            max_code_generation_attempts=state.max_code_generation_attempts
        )
    
    return ToolGenerationState(
        user_request=state.user_request,
        test_output=tool_output,
        code_generation_attempts=state.code_generation_attempts,
        max_code_generation_attempts=state.max_code_generation_attempts,
        generated_code=state.generated_code,
        code_to_test=state.code_to_test
    )

# =====================================================================================
# Evaluate Test Results Node
# =====================================================================================
def node_evaluate_test_results(state: ToolGenerationState) -> ToolGenerationState:
    global logger
    logger.info("--- Node: Evaluate Test Results ---")
    test_output_str = state.test_output
    full_generated_script = state.generated_code

    # Initialize new_state_update by copying from the incoming state
    # This ensures all required fields like user_request are present
    new_state_update = ToolGenerationState(**state.model_dump())

    # Set initial evaluation and feedback to assume failure, will update on success
    new_state_update.test_evaluation = "failure"
    new_state_update.error_feedback_for_codegen = None # Clear previous feedback initially
    new_state_update.generated_function_only_code = None # Reset extracted code initially

    if not test_output_str:
        logger.warning("No test output received from REPL tool.")
        new_state_update.test_evaluation = "error_in_script"
        new_state_update.error_feedback_for_codegen = (
            "REPL tool did not return any output. The generated script might have crashed "
            "without printing anything or there was an issue with the testing environment."
        )
        # Return the updated state directly
        return new_state_update

    # The python_repl_tool formats its output. We need to parse that first.
    # Example output from python_repl_tool:
    # "Code executed:\n```python\n[code]\n```\nStdout:\n```text\n[stdout_content]\n```"
    # Or for errors from REPL: "Error executing Python code:\n```python\n[code]\n```\nErrorType: [type]\nMessage: [msg]"

    if "ErrorType:" in test_output_str and "Message:" in test_output_str:
        logger.warning(f"REPL tool reported an execution error: {test_output_str}")
        new_state_update.test_evaluation = "failure"
        # Extract the error message for more specific feedback
        try:
            error_message_for_llm = test_output_str.split("Message:", 1)[1].strip()
        except IndexError:
            error_message_for_llm = "The script caused an unclassified error in the REPL."
        new_state_update.error_feedback_for_codegen = (
            f"The script failed during execution in the REPL. Error details: '{error_message_for_llm}'. "
            "Please ensure the script is valid Python, all necessary libraries (like kubernetes, json) are imported within the script block, "
            "and the Kubernetes client configuration is handled correctly if needed for the test."
        )
        # Return the updated state directly
        return new_state_update


    # Assuming successful execution by REPL, parse the Stdout content
    actual_stdout = ""
    if "Stdout:" in test_output_str:
        stdout_block = test_output_str.split("Stdout:", 1)[1].strip()
        if stdout_block.startswith("```text") and stdout_block.endswith("```"):
            actual_stdout = stdout_block[len("```text"):-len("```")].strip()
        elif stdout_block.startswith("```") and stdout_block.endswith("```"): # More generic ``` block
             actual_stdout = stdout_block[len("```"):-len("```")].strip()
        else:
            actual_stdout = stdout_block # If no markdown block, take as is

        if not actual_stdout and "[No explicit print output captured" not in test_output_str : # REPL specific None/empty messages
            logger.warning("Script executed but produced no discernible standard output (stdout).")
            new_state_update.test_evaluation = "failure"
            new_state_update.error_feedback_for_codegen = (
                "The script executed without Python errors but produced no standard output (stdout). "
                "The script MUST use `print()` to output a JSON string as its final action, representing the function's return value."
            )
            # Return the updated state directly
            return new_state_update
        elif "[No explicit print output captured" in actual_stdout or not actual_stdout.strip():
             logger.warning("Script executed but REPL captured no explicit print output or it was empty.")
             new_state_update.test_evaluation = "failure"
             new_state_update.error_feedback_for_codegen = (
                "The script executed, but no explicit output was captured by `print()`. "
                "The script MUST use `print()` to output a JSON string as its final action."
             )
             # Return the updated state directly
             return new_state_update

    else: # Should not happen if REPL tool works as expected
        logger.error(f"Unrecognized REPL tool output format: {test_output_str}")
        new_state_update.test_evaluation = "error_in_script"
        new_state_update.error_feedback_for_codegen = "The output from the REPL tool was in an unexpected format. Cannot determine script success."
        # Return the updated state directly
        return new_state_update

    logger.info(f"Script stdout: {actual_stdout}")

    # Check for common Python error patterns in stdout
    # This indicates the script itself printed an error string before valid JSON.
    common_error_indicators = ["NameError(", "SyntaxError(", "ImportError(", "TypeError(", "ValueError(", "AttributeError(", "IndentationError("]
    if any(indicator in actual_stdout for indicator in common_error_indicators):
        logger.warning(f"Script execution resulted in a Python error string in stdout: {actual_stdout}")
        new_state_update.test_evaluation = "failure" # Or "error_in_script" if it's a syntax-level issue
        new_state_update.error_feedback_for_codegen = (
            f"The script's execution printed a Python error message directly to stdout: '{actual_stdout}'. "
            "This indicates a runtime or syntax error within the script itself. Please analyze this error and correct the script. "
            "Ensure all variables are defined, imports are correct and accessible, and syntax is valid. "
            "Avoid non-standard characters like non-breaking spaces in your code." # Added this hint
        )
        # Return the updated state directly
        return new_state_update


    # Now, evaluate the content of actual_stdout (which should be a JSON string)
    try:
        script_result = json.loads(actual_stdout)
        if isinstance(script_result, dict) and script_result.get("status") == "success":
            logger.info("Script reported success and output is valid JSON.")
            new_state_update.test_evaluation = "success"
            new_state_update.error_feedback_for_codegen = None # Clear feedback on success

            # --- Try to extract the delimited function code ---
            if not full_generated_script:
                logger.error("Cannot extract function code: full_generated_script is missing in state.")
                new_state_update.test_evaluation = "failure" # Demote to failure
                new_state_update.error_feedback_for_codegen = "Internal error: Original generated script not found in state for function extraction."
                new_state_update.generated_function_only_code = None
                # Return the updated state directly
                return new_state_update

            start_marker = "# --- START FUNCTION ---"
            end_marker = "# --- END FUNCTION ---"

            try:
                start_idx = full_generated_script.index(start_marker)
                # Find the end of the line for the end_marker
                # Add len(start_marker) to avoid finding the start marker itself if end_marker is a substring
                end_marker_line_start_idx = full_generated_script.index(end_marker, start_idx + len(start_marker))
                # Find the newline AFTER the start marker to get the actual code start
                code_start_after_marker = full_generated_script.index('\n', start_idx + len(start_marker)) + 1 \
                                           if '\n' in full_generated_script[start_idx + len(start_marker):] \
                                           else start_idx + len(start_marker) + 1 # Handle case where START is last line

                extracted_code = full_generated_script[code_start_after_marker:end_marker_line_start_idx].strip()

                if not extracted_code:
                    raise ValueError("Extracted function code is empty after stripping.")

                new_state_update.generated_function_only_code = extracted_code
                logger.info("Successfully extracted function code using delimiters.")
                logger.debug(f"Extracted function code:\n{extracted_code}")

            except ValueError as ve: # Delimiters not found or issue with extraction
                logger.warning(f"Failed to extract function code using delimiters: {ve}")
                new_state_update.test_evaluation = "failure" # Demote to failure
                new_state_update.error_feedback_for_codegen = (
                    f"The script executed successfully, but the required code delimiters "
                    f"'{start_marker}' and/or '{end_marker}' were not found or the content between them was empty ({ve}). "
                    "Please ensure the function definition is correctly wrapped as per instructions."
                )
                new_state_update.generated_function_only_code = None
                # Return the updated state directly
                return new_state_update
            # --- End function code extraction ---

        elif isinstance(script_result, dict) and script_result.get("status") == "error":
            error_detail = script_result.get('message', 'No error message provided by the script.')
            error_type = script_result.get('error_type', '')

            # Distinguish between operational/API errors (code is working correctly) and
            # actual code bugs (NameError, ImportError, etc.).
            # ApiException means the Kubernetes API was reached successfully — the code runs.
            # K8sConfigError means config loaded (or tried) — code structure is fine.
            # These should NOT trigger code regeneration; they are runtime conditions.
            OPERATIONAL_ERROR_TYPES = {"ApiException", "K8sConfigError"}
            if error_type in OPERATIONAL_ERROR_TYPES:
                logger.info(
                    f"Script returned operational error (not a code bug): {error_detail} "
                    f"(error_type={error_type}). Treating code as valid — extracting function."
                )
                # Extract the function code so the tool can be registered.
                # The ApiException is a legitimate runtime response; the function itself is correct.
                new_state_update.test_evaluation = "success"
                new_state_update.error_feedback_for_codegen = None

                if not full_generated_script:
                    logger.error("Cannot extract function code: full_generated_script is missing.")
                    new_state_update.test_evaluation = "failure"
                    new_state_update.error_feedback_for_codegen = "Internal error: original script not in state."
                    return new_state_update

                start_marker = "# --- START FUNCTION ---"
                end_marker = "# --- END FUNCTION ---"
                try:
                    start_idx = full_generated_script.index(start_marker)
                    end_marker_line_start_idx = full_generated_script.index(end_marker, start_idx + len(start_marker))
                    code_start_after_marker = (
                        full_generated_script.index('\n', start_idx + len(start_marker)) + 1
                        if '\n' in full_generated_script[start_idx + len(start_marker):]
                        else start_idx + len(start_marker) + 1
                    )
                    extracted_code = full_generated_script[code_start_after_marker:end_marker_line_start_idx].strip()
                    if not extracted_code:
                        raise ValueError("Extracted function code is empty.")
                    new_state_update.generated_function_only_code = extracted_code
                    logger.info("Extracted function code from script with operational error.")
                except ValueError as ve:
                    logger.warning(f"Failed to extract function code (operational error path): {ve}")
                    new_state_update.test_evaluation = "failure"
                    new_state_update.error_feedback_for_codegen = (
                        f"Script ran but delimiters not found or empty ({ve}). "
                        "Ensure the function is wrapped with # --- START FUNCTION --- and # --- END FUNCTION ---."
                    )
            else:
                logger.warning(f"Script reported a code-level error: {error_detail} (Type: {error_type})")
                new_state_update.test_evaluation = "failure"
                new_state_update.error_feedback_for_codegen = (
                    f"The script executed but reported an error: '{error_detail}' (Type: {error_type}). "
                    "Please review the script logic to fix this error. "
                    "This is likely a code bug — check variable names, imports, and API call syntax."
                )
        else:
            logger.warning(f"Script output was JSON but not in the expected status format: {actual_stdout}")
            new_state_update.test_evaluation = "failure"
            new_state_update.error_feedback_for_codegen = (
                "The script's `print()` output was valid JSON, but it did not contain the expected "
                "`{'status': 'success', ...}` or `{'status': 'error', ...}` structure. "
                "Please ensure the script prints a JSON object with a 'status' field."
            )
    except json.JSONDecodeError:
        logger.warning(f"Script output was not valid JSON: {actual_stdout}")
        new_state_update.test_evaluation = "failure"
        new_state_update.error_feedback_for_codegen = (
            "The script's `print()` output was not a valid JSON string. "
            "Please ensure the final `print()` call in the script outputs a well-formed JSON string (e.g., using `json.dumps()`). "
            f"Received: ```{actual_stdout}```"
        )

    # Return the updated state object
    return new_state_update


# =====================================================================================
# Generate Metadata Node
# =====================================================================================
def node_generate_metadata(state: ToolGenerationState) -> ToolGenerationState:
    logger.info("--- Node: Generate Metadata ---")

    # Create a copy of the incoming state to modify
    new_state = ToolGenerationState(**state.model_dump())
    new_state.registration_args = None # Reset before attempting to generate
    new_state.error_message = None # Clear previous metadata errors

    user_request = state.user_request
    function_code = state.generated_function_only_code or state.generated_code
    test_output = state.test_output

    if not function_code:
        err = "No function code available for metadata generation"
        logger.error(err)
        new_state.error_message = err
        return new_state # Return updated state

    prompt = (
        f"User request:\n{user_request}\n\n"
        f"Clean function code:\n```python\n{function_code}\n```\n\n"
        f"Successful test stdout:\n```json\n{test_output}\n```\n\n"
        "Generate the metadata JSON object (respond with ONLY raw JSON, no markdown)."
    )

    messages = [
        ("system", METADATA_GENERATOR_SYSTEM_PROMPT),
        ("human", prompt),
    ]

    metadata_json_str = None # Initialize before try block
    try:
        response = llm.invoke(messages)
        raw = response.content or ""
        logger.debug(f"Raw metadata LLM response:\n{raw}")

        # Strip ```json … ``` or bare ``` … ``` fences, if present
        fence_match = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
        metadata_json_str = fence_match.group(1).strip() if fence_match else raw.strip()

        if not metadata_json_str:
            err = "LLM returned empty metadata."
            logger.error(err)
            new_state.error_message = err
            return new_state # Return updated state

        # Parse JSON
        metadata: Dict[str, Any] = json.loads(metadata_json_str)

        # Tight-validation block for required keys
        required_keys = {
            "function_name",
            "pydantic_class_name",
            "tool_instance_variable_name",
            "langchain_tool_name",
            "langchain_tool_description",
            "parameters",
        }
        missing_keys = required_keys - metadata.keys()
        if missing_keys:
            err = f"Metadata missing required keys: {', '.join(sorted(missing_keys))}"
            logger.error(err)
            new_state.error_message = err
            return new_state # Return updated state

        # Build registration arguments and perform Pydantic validation
        try:
            # Use .get() with default None or appropriate type for optional fields
            registration_args = RegisterAIToolInput(
                generated_function_code=function_code,
                function_name=metadata.get("function_name"),
                pydantic_class_name=metadata.get("pydantic_class_name"),
                tool_instance_variable_name=metadata.get("tool_instance_variable_name"),
                langchain_tool_name=metadata.get("langchain_tool_name"),
                langchain_tool_description=metadata.get("langchain_tool_description"),
                parameters=metadata.get("parameters", []), # Default parameters to empty list if missing
                target_file_path=metadata.get("target_file_path", "app/agents/tools/ai_generated_tools/ai_generated_tools.py") # Use default if not provided
            )
            new_state.registration_args = registration_args
        except ValidationError as ve:
            err = f"Generated metadata failed Pydantic validation: {ve}"
            logger.error(err)
            new_state.error_message = err
            # registration_args remains None
            return new_state # Return updated state

        # If we reach here, metadata generation and Pydantic validation was successful
        return new_state # Return updated state with registration_args populated

    except json.JSONDecodeError as jde:
        err = f"Metadata JSON could not be decoded. Ensure the LLM output is valid JSON. Details: {jde}\nRaw text: {metadata_json_str[:500]}..."
        logger.error(err)
        new_state.error_message = err
        return new_state # Return updated state
    except Exception as e:
        err = f"An unexpected error occurred during metadata generation: {e}"
        logger.error(err, exc_info=True)
        new_state.error_message = err
        return new_state # Return updated state


# =====================================================================================
# Register Tool Node
# =====================================================================================
def node_register_tool(state: ToolGenerationState) -> ToolGenerationState:
    logger.info("--- Node: Register Tool ---")

    if not state.registration_args:
        error_message = "Tool registration failed: Missing registration arguments."
        logger.error(error_message)
        # Assuming ToolGenerationState can be initialized with only a few fields for error states
        # or that you update specific fields on the existing state if it's mutable.
        # For simplicity, creating a new state object or updating relevant fields:
        # This part depends on how ToolGenerationState is structured and if it's mutable.
        # If it's a Pydantic model, you'd typically create a new one or use .copy(update={...})
        updated_state_dict = state.model_dump()
        updated_state_dict["registration_status"] = "Failed: Missing registration arguments."
        updated_state_dict["final_message"] = error_message
        return ToolGenerationState(**updated_state_dict)

    try:
        # Explicitly convert the Pydantic model instance to a dictionary
        args_dict = state.registration_args.model_dump()
        status = register_ai_tool_definer.invoke(args_dict)
        logger.info(f"Tool registration status: {status}")

        success_message = f"Tool registration completed: {status}"
        updated_state_dict = state.model_dump()
        updated_state_dict["registration_status"] = status
        updated_state_dict["final_message"] = success_message
        return ToolGenerationState(**updated_state_dict)

    except Exception as e:
        error_msg = f"Tool registration failed with error: {str(e)}"
        logger.error(error_msg, exc_info=True)

        updated_state_dict = state.model_dump()
        updated_state_dict["registration_status"] = "Failed"
        updated_state_dict["final_message"] = error_msg
        # Ensure error_message field is also populated if it's used by other nodes/conditions
        updated_state_dict["error_message"] = error_msg
        return ToolGenerationState(**updated_state_dict)


# =====================================================================================
# Handle Failure Node
# =====================================================================================
def node_handle_failure(state: ToolGenerationState) -> ToolGenerationState:
    logger.info("--- Node: Handle Failure ---")
    error_msg = state.error_message or "Max code generation attempts reached or other unrecoverable error."
    feedback = state.error_feedback_for_codegen or error_msg
    final_message = f"Tool generation failed. Last feedback/error: {feedback}"
    logger.error(final_message)
    
    return ToolGenerationState(
        user_request=state.user_request,
        final_message=final_message,
        error_message=error_msg,
        error_feedback_for_codegen=feedback,
        code_generation_attempts=state.code_generation_attempts,
        max_code_generation_attempts=state.max_code_generation_attempts
    )

# =====================================================================================
# Finish Node
# =====================================================================================
def node_finish(state: ToolGenerationState) -> ToolGenerationState:
    logger.info("--- Node: Finish ---")
    if not state.final_message:
        final_message = "Process finished, but no specific final message was set."
        logger.warning(final_message)
        return ToolGenerationState(
            user_request=state.user_request,
            final_message=final_message,
            code_generation_attempts=state.code_generation_attempts,
            max_code_generation_attempts=state.max_code_generation_attempts,
            registration_status=state.registration_status,
            registration_args=state.registration_args,
            generated_code=state.generated_code,
            generated_function_only_code=state.generated_function_only_code
        )
    
    logger.info(f"Workflow completed. Final message: {state.final_message}")
    return state

# =====================================================================================
# Should Retry Code Generation Condition
# =====================================================================================
def should_retry_code_generation(state: ToolGenerationState) -> str:
    print("--- Condition: Should Retry Code Generation? ---")
    attempts = state.code_generation_attempts
    max_attempts = state.max_code_generation_attempts
    test_eval = state.test_evaluation

    if test_eval == "success":
        print("Decision: Test successful, proceed to metadata generation.")
        return "generate_metadata"
    elif attempts < max_attempts:
        print(f"Decision: Test failed (attempt {attempts}/{max_attempts}), retry code generation.")
        return "generate_code"
    else:
        print(f"Decision: Test failed (max attempts {max_attempts} reached), handle failure.")
        return "handle_failure"

# =====================================================================================
# Did Metadata Succeed Condition
# =====================================================================================
def did_metadata_succeed(state: ToolGenerationState) -> str:
    print("--- Condition: Did Metadata Succeed? ---")
    if state.error_message and "Failed to parse metadata" in state["error_message"]:
        print("Decision: Metadata generation failed.")
        # You could add a retry loop for metadata too, or go straight to failure
        return "handle_failure" 
    if state.registration_args:
        print("Decision: Metadata succeeded, proceed to registration.")
        return "register_tool"
    else: # Should not happen if error_message isn't set, but as a fallback
        print("Decision: Metadata likely failed (no registration_args), handle failure.")
        return "handle_failure"







# In app/agents/tools/code_generator_tools.py
# Graph Definition
# =====================================================================================
workflow = StateGraph(ToolGenerationState)

# Add nodes
workflow.add_node("generate_code", node_generate_code)
workflow.add_node("test_code", node_test_code)
workflow.add_node("evaluate_test_results", node_evaluate_test_results)
workflow.add_node("generate_metadata", node_generate_metadata)
workflow.add_node("register_tool", node_register_tool)
workflow.add_node("handle_failure", node_handle_failure)
workflow.add_node("finish", node_finish) # Final node

# Define edges
workflow.set_entry_point("generate_code")
workflow.add_edge("generate_code", "test_code")
workflow.add_edge("test_code", "evaluate_test_results")

workflow.add_conditional_edges(
    "evaluate_test_results",
    should_retry_code_generation,
    {
        "generate_metadata": "generate_metadata",
        "generate_code": "generate_code", # Loop back with feedback
        "handle_failure": "handle_failure"
    }
)

workflow.add_conditional_edges(
    "generate_metadata",
    did_metadata_succeed,
    {
        "register_tool": "register_tool",
        "handle_failure": "handle_failure" # If metadata generation itself fails badly
    }
)
# After registration or failure, go to a final node or END
workflow.add_edge("register_tool", "finish")
workflow.add_edge("handle_failure", "finish")
workflow.add_edge("finish", END)
# Compile the graph
app = workflow.compile()





# =====================================================================================
# Tool Deduplication Check
# =====================================================================================

def _check_existing_tools(user_request: str) -> tuple[bool, str]:
    """
    Simple safety check to prevent creating duplicate tools with the same name.
    
    NOTE: This function ONLY checks for exact tool name matches to prevent name collisions.
    It does NOT perform semantic matching to determine if a tool can handle a request.
    
    The DynamicToolsExecutor agent already has access to all available tools and their
    descriptions. The agent (with its LLM capabilities) is responsible for determining
    whether an existing tool can handle a user's request. If the agent determines no
    tool is available, we trust that assessment and proceed with tool creation.
    
    This function's sole purpose is to prevent creating a tool with an exact name that
    already exists (e.g., prevent creating "check_unprotected_pods" if it already exists).
    
    Returns (tool_exists, tool_name_or_message)
    """
    try:
        # Check PVC registry for runtime-generated tools (by name only)
        try:
            from app.services.tool_registry_service import ToolRegistryService
            registry_service = ToolRegistryService()
            runtime_tools = registry_service.list_tools(status="enabled")
            
            # Extract potential tool name from request (simple heuristic)
            # This is just to prevent exact name collisions, not semantic matching
            user_request_lower = user_request.lower()
            
            # For each registered tool, check if the request seems to be asking
            # for exactly this tool by name (exact or near-exact match)
            for tool_meta in runtime_tools:
                tool_name = tool_meta.get("name", "").lower()
                # Only match if tool name appears verbatim in the request
                # This prevents creating "check_unprotected_pods" when "check_unprotected_pods" already exists
                if tool_name in user_request_lower or user_request_lower in tool_name:
                    return True, f"Tool '{tool_meta['name']}' already exists: {tool_meta.get('description', '')[:200]}"
        except Exception as e:
            logger.warning(f"Could not check PVC registry for existing tools: {e}")
        
        # Also check static tools for exact name matches
        try:
            from app.agents.tools import kubernetes_tools
            static_tools = getattr(kubernetes_tools, 'all_k8s_tools', [])
            
            user_request_lower = user_request.lower()
            for tool in static_tools:
                if hasattr(tool, 'name'):
                    tool_name_lower = tool.name.lower()
                    # Only exact name matches - prevent creating duplicate tool names
                    if tool_name_lower in user_request_lower or user_request_lower in tool_name_lower:
                        return True, f"Tool '{tool.name}' already exists: {tool.description[:200] if hasattr(tool, 'description') else ''}"
        except Exception as e:
            logger.warning(f"Could not check static tools: {e}")
        
        # No exact name match found - proceed with tool creation
        # The agent will determine if the tool is actually needed
        return False, "No exact tool name match found - proceeding with tool creation"
        
    except Exception as e:
        logger.error(f"Error during tool deduplication check: {e}", exc_info=True)
        # On error, allow tool creation (fail open) - let the agent decide
        return False, f"Error checking existing tools: {str(e)}"

# =====================================================================================
# Code Generator Tool
# =====================================================================================

class CodeGeneratorToolInput(BaseModel):
    user_request: str = Field(description="The user's request to generate a new Kubernetes tool.")

def _generate_code(user_request: str) -> str:
    logger.info(f"Code generator tool invoked for request: {user_request}")
    
    # STEP 1: Simple safety check for exact tool name duplicates
    # NOTE: We trust the DynamicToolsExecutor agent's assessment. If it says
    # no tool is available, we proceed. This check only prevents exact name collisions.
    tool_exists, existing_tool_message = _check_existing_tools(user_request)
    
    if tool_exists:
        logger.info(f"Found existing tool with same name: {existing_tool_message}")
        return f"⚠️ **Tool Name Conflict Detected**\n\n{existing_tool_message}\n\nA tool with a similar name already exists. Please verify with DynamicToolsExecutor if this existing tool can handle the request, or consider using a different tool name.\n[TOOL_CREATED]"
    
    logger.info("No existing tool found, proceeding with tool generation...")
    
    # Prepare the initial state as a dictionary, ensuring all fields defined in
    # ToolGenerationState are present with default values if not explicitly set.
    initial_state_dict = {
        "user_request": user_request,
        "code_generation_attempts": 0,
        "max_code_generation_attempts": 3, # Or get from settings/config if dynamic
        "generated_code": None,
        "generated_function_only_code": None,
        "code_to_test": None,
        "test_output": None,
        "test_evaluation": None,
        "error_feedback_for_codegen": None,
        "function_name": None,
        "pydantic_class_name": None,
        "tool_instance_variable_name": None,
        "langchain_tool_name": None,
        "langchain_tool_description": None,
        "parameters": None, # Or an empty list: [] if appropriate
        "registration_args": None,
        "registration_status": None,
        "final_message": None,
        "error_message": None,
    }
    
    config = RunnableConfig(recursion_limit=50) # Consider making recursion_limit configurable
    
    try:
        logger.debug(f"Invoking code generation workflow with initial state: {initial_state_dict}")
        final_state_output = app.invoke(initial_state_dict, config=config)
        logger.debug(f"Code generation workflow completed. Final state output type: {type(final_state_output)}")
        logger.debug(f"Final state output content: {final_state_output}")

        # Access final_message using dictionary key access (.get() for safety)
        final_message_value = final_state_output.get("final_message")

        if final_message_value:
            logger.info(f"Code generator tool finished. Final message: {final_message_value}")
            # PATCH: Add [TOOL_CREATED] tag for orchestration detection
            return f"{final_message_value}\n[TOOL_CREATED]"
        else:
            logger.warning("Code generator tool finished, but workflow completed without a specific final message.")
            return "Workflow completed without a final message.\n[TOOL_CREATED]"

    except Exception as e:
        logger.error(f"Error during code generation workflow execution in _generate_code: {e}", exc_info=True)
        return f"Error in code generation process: {str(e)}"



code_generator_tool = StructuredTool.from_function(
    func=_generate_code,
    name="generate_code",
    description=(
        "Generates and registers a new LangChain StructuredTool for Kubernetes operations "
        "based on user requirements. Internally follows a robust code generation, testing, "
        "and registration workflow using the Kubernetes Python client. "
        "All logic is handled by the code generation LangGraph agent."
    ),
    args_schema=CodeGeneratorToolInput
)




if __name__ == '__main__':
    # save_langgraph_workflow(app, base_filename="compiled_workflow_cg")
    print("LLM needs to be initialized in the __main__ block or globally for nodes to use it.")

    # =====================================================================================
    # Initial State
    # =====================================================================================
    initial_state: ToolGenerationState = {
        # "user_request": "Create a Kubernetes tool to list all deployments in the given namespace.",
        "user_request": "Create a Kubernetes tool to list all events in the given namespace.",
        "code_generation_attempts": 0,
        "max_code_generation_attempts": 3,
    }

    config = RunnableConfig(recursion_limit=50) # Increase if complex loops
    final_state = app.invoke(initial_state, config=config)
    print("\n--- Final State ---")
    print(final_state.get("final_message", "Workflow completed."))
