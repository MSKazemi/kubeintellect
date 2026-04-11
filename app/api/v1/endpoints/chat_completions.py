from fastapi import APIRouter, HTTPException, Body, Request as FastAPIRequest
from fastapi.responses import StreamingResponse
from openai import OpenAIError
from app.orchestration.workflow import run_kubeintellect_workflow
from app.core.config import settings
from app.utils.logger_config import setup_logging, request_id_var, log_audit_event
from app.utils.metrics import hitl_decisions_total, stream_completions_total, summary_cache_hit_total, summary_cache_miss_total
from app.utils.postgres_checkpointer import get_checkpointer
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from app.core.llm_gateway import get_llm_with_params, get_langfuse_client
from app.modules.query_processor import query_processor

import asyncio
import uuid
import time
import json
from pydantic import BaseModel, Field
from typing import List, Optional, AsyncGenerator, Dict, Any

logger = setup_logging(app_name="kubeintellect")

pg_checkpointer = get_checkpointer()

# Configuration for short-term memory window
SHORT_TERM_MEMORY_WINDOW = settings.SHORT_TERM_MEMORY_WINDOW

# --- Pydantic Models ---
class ChatMessage(BaseModel):
    role: str = Field(..., examples=["system", "user", "assistant"])
    content: str = Field(..., min_length=1, description="The content of the message. Must not be empty.")

class ChatCompletionRequest(BaseModel):
    model: Optional[str] = Field(default="gpt-3.5-turbo", description="Model name, can be used to select different configurations if needed.")
    messages: List[ChatMessage]
    stream: Optional[bool] = Field(default=False, description="Whether to stream back partial progress. If True, SSE will be used.")
    conversation_id: Optional[str] = Field(None, description="Conversation ID for HITL checkpointing and session management")
    conversationId: Optional[str] = Field(None, description="Alternative conversation ID field (camelCase)")
    user: Optional[str] = Field(None, description="User ID for user-level operations")
    user_id: Optional[str] = Field(None, description="Alternative user ID field (snake_case)")
    userId: Optional[str] = Field(None, description="Alternative user ID field (camelCase)")
    resume: Optional[bool] = Field(default=False, description="Whether this is a resume request for HITL (Human-in-the-Loop) workflow.")
    action_id: Optional[str] = Field(None, description="Action ID from a pending HITL event — sent by CLI on approve/deny to correlate audit log entry")
    class Config:
        extra = "allow"

class ResponseMessage(BaseModel):
    role: str = "assistant"
    content: Optional[str] = None

class Choice(BaseModel):
    index: int
    message: ResponseMessage
    finish_reason: Optional[str] = "stop"
    hitl_required: bool = False
    action_id: Optional[str] = None

class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: Optional[int] = 0
    total_tokens: int = 0

class ChatCompletionResponse(BaseModel):
    id: str = Field(..., examples=["chatcmpl-xxxxxxxxxxxxxxxxxxxxxxx"])
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[Choice]
    usage: Optional[UsageInfo] = None
    system_fingerprint: Optional[str] = None

class ChatCompletionStreamDelta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None

class ChatCompletionStreamChoice(BaseModel):
    index: int
    delta: ChatCompletionStreamDelta
    finish_reason: Optional[str] = None
    hitl_required: bool = False
    action_id: Optional[str] = None

class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChatCompletionStreamChoice]
    usage: Optional[UsageInfo] = None
    system_fingerprint: Optional[str] = None

class ErrorDetail(BaseModel):
    type: str
    message: str
    code: Optional[str] = None
    param: Optional[str] = None

class ErrorResponse(BaseModel):
    error: ErrorDetail

class HITLResumeRequest(BaseModel):
    conversation_id: str = Field(..., description="Conversation ID for the HITL session")
    user_id: Optional[str] = Field(None, description="User ID (optional, for user-level operations)")
    approved: bool = Field(..., description="Whether the user approved the CodeGenerator action")
    original_query: Optional[str] = Field(None, description="Original user query (optional)")

router = APIRouter()

def get_effective_model_name(requested_model: Optional[str]) -> str:
    if settings.LLM_PROVIDER == "azure":
        return settings.SUPERVISOR_AZURE_DEPLOYMENT_NAME
    elif settings.LLM_PROVIDER == "openai":
        return settings.SUPERVISOR_LLM_MODEL
    return requested_model or "kubeintellect-default"

async def validate_chat_completion_request(request: ChatCompletionRequest) -> tuple[str, bool]:
    if not request.messages:
        raise HTTPException(status_code=400, detail={"error": {"type": "invalid_request_error", 
                                                               "message": "messages is a required property and cannot be empty.", 
                                                               "param": "messages"}})
    
    # Check for title generation requests (LibreChat sends system-only messages for title generation)
    # Title generation requests typically have:
    # - Only system messages (no user messages)
    # - Short max_tokens (if available)
    # - System message contains title generation prompt
    has_user_message = any(msg.role == "user" for msg in request.messages)
    has_system_message = any(msg.role == "system" for msg in request.messages)
    
    # Check if this looks like a title generation request
    is_title_generation = False
    
    # Check max_tokens from model_extra (Pydantic extra fields)
    max_tokens = None
    if hasattr(request, 'model_extra') and isinstance(request.model_extra, dict):
        max_tokens = request.model_extra.get('max_tokens') or request.model_extra.get('maxTokens')
    # Also try direct attribute access
    if max_tokens is None:
        max_tokens = getattr(request, 'max_tokens', None) or getattr(request, 'maxTokens', None)
    
    if max_tokens and max_tokens <= 20:
        is_title_generation = True
    elif not has_user_message and has_system_message:
        # System-only message is likely a title generation request
        system_msg = next((msg for msg in request.messages if msg.role == "system"), None)
        if system_msg and system_msg.content.strip():
            # Check if system message contains title generation patterns
            content_lower = system_msg.content.lower()
            if any(keyword in content_lower for keyword in ["title", "generate", "concise", "5-word"]):
                is_title_generation = True
    
    # For title generation, allow system-only messages and extract content from system message
    if is_title_generation and not has_user_message:
        system_message = next((msg for msg in request.messages if msg.role == "system"), None)
        if system_message and system_message.content.strip():
            # Return the system message content for title generation
            # The workflow will handle it appropriately
            logger.info("Detected title generation request - allowing system-only message")
            return system_message.content.strip(), True
    
    # Normal validation: require a user message
    user_message = next((msg for msg in reversed(request.messages) if msg.role == "user"), None)
    if not user_message or not user_message.content.strip():
        raise HTTPException(status_code=400, detail={"error": {"type": "invalid_request_error", 
                                                               "message": "Valid user message with content is required.", 
                                                               "param": "messages"}})
    return user_message.content.strip(), False

async def check_for_hitl_approval_response(
    user_query: str, conversation_id: str, user_id: str = None
) -> tuple[bool, bool, str]:
    """
    Check if the user query is an approval/denial response for CodeGenerator HITL.

    Returns: (is_approval_response, approved, original_query)
      - is_approval_response: True when an active checkpoint exists and this message is a response to it
      - approved: True for approval keywords
      - original_query: the human message that triggered the original workflow
    """
    if not conversation_id:
        return False, False, ""

    query_lower = user_query.lower().strip()

    # First check if there's an active checkpoint - only then we check for approval/denial
    # primary_id mirrors workflow.py: conversation_id takes precedence over user_id.
    # This is the key used by save_checkpoint / delete_checkpoint in workflow.py.
    primary_id = conversation_id or user_id
    thread_id = f"thread_{conversation_id}" if conversation_id else (
        f"thread_{user_id}" if user_id else None
    )

    if not thread_id:
        return False, False, ""

    try:
        checkpoint = pg_checkpointer.load_checkpoint(primary_id, thread_id)
        logger.debug(f"Checkpoint loaded for approval detection: primary_id={primary_id}, thread_id={thread_id}, checkpoint_exists={checkpoint is not None}")
    except Exception as e:
        logger.error(f"Error loading checkpoint from Postgres for approval detection: {e}", exc_info=True)
        checkpoint = None

    if not checkpoint:
        logger.debug(f"No checkpoint found for approval detection: primary_id={primary_id}, thread_id={thread_id}")
        return False, False, ""
    
    # Check for approval keywords - more strict matching
    approval_keywords = ["approve", "approved", "yes", "y", "continue", "proceed", "ok", "okay"]
    denial_keywords = ["deny", "denied", "no", "n", "reject", "cancel", "stop"]
    
    logger.debug(f"Checking approval keywords for query: '{query_lower}'")
    
    # For very short responses, be more strict
    if len(query_lower.split()) <= 3:
        # Exact matches or simple phrases for short responses
        is_approval = (
            query_lower in approval_keywords or
            any(query_lower == keyword for keyword in approval_keywords) or
            any(query_lower.startswith(keyword + " ") for keyword in ["approve", "yes", "continue", "proceed"])
        )
        is_denial = (
            query_lower in denial_keywords or
            any(query_lower == keyword for keyword in denial_keywords) or
            any(query_lower.startswith(keyword + " ") for keyword in ["deny", "no", "reject", "cancel"])
        )
    else:
        # For longer responses, check if it contains the keywords
        is_approval = any(keyword in query_lower for keyword in approval_keywords)
        is_denial = any(keyword in query_lower for keyword in denial_keywords)
    
    # Parse checkpoint to extract original query
    checkpoint_data = {}
    original_query = ""
    try:
        _cfg, _state = checkpoint
        checkpoint_data = _state if isinstance(_state, dict) else {}

        # Extract original query from checkpoint
        if "accumulated_state" in checkpoint_data and "messages" in checkpoint_data["accumulated_state"]:
            for msg in checkpoint_data["accumulated_state"]["messages"]:
                if hasattr(msg, 'content') and msg.content and not msg.content.startswith("🛑") and not msg.content.startswith("I do not have"):
                    original_query = msg.content
                    break
        if not original_query and "langgraph_state" in checkpoint_data and checkpoint_data["langgraph_state"]:
            langgraph_state = checkpoint_data["langgraph_state"]
            if "messages" in langgraph_state:
                for msg in langgraph_state["messages"]:
                    if hasattr(msg, 'content') and msg.content and not msg.content.startswith("🛑") and not msg.content.startswith("I do not have"):
                        original_query = msg.content
                        break
    except Exception as e:
        logger.error(f"Error parsing checkpoint for approval detection: {e}", exc_info=True)
        logger.error(f"Checkpoint structure: {type(checkpoint)}, length={len(checkpoint) if hasattr(checkpoint, '__len__') else 'N/A'}")

    if is_approval or is_denial:
        logger.info(f"Extracted original query for approval: '{original_query[:100] if original_query else 'N/A'}...'")
        logger.info(f"Approval detection result: is_approval_response=True, approved={is_approval}, original_query_length={len(original_query)}")
        return True, is_approval, original_query

    logger.debug(f"No approval/denial keywords matched for query: '{query_lower}'")
    return False, False, ""

class _NullCtx:
    def __enter__(self): return self
    def __exit__(self, *_): pass


def _build_title_langfuse_ctx(conversation_id: str = None, user_id: str = None):
    """Langfuse v4 context for title-generation LLM calls.

    Title generation is a direct LLM call that bypasses the LangGraph workflow,
    so it needs its own propagate_attributes() context to get a meaningful trace name.
    """
    from app.core.config import settings as _s
    if not _s.LANGFUSE_ENABLED:
        return _NullCtx()
    try:
        import langfuse as _lf
        kwargs: dict = {"trace_name": "title_generation", "tags": ["title_generation"]}
        if conversation_id:
            kwargs["session_id"] = f"thread_{conversation_id}"
        if user_id:
            kwargs["user_id"] = user_id
        return _lf.propagate_attributes(**kwargs)
    except Exception:
        return _NullCtx()


async def generate_title_directly(
    title_prompt: str,
    max_tokens: int = 16,
    temperature: float = 0.2,
    conversation_id: str = None,
    user_id: str = None,
) -> str:
    """
    Generate a conversation title using a direct LLM call, bypassing the workflow.

    Args:
        title_prompt: The system message containing the title generation prompt
        max_tokens: Maximum tokens for the title (default: 16) - used for truncation
        temperature: Temperature for title generation (default: 0.2)
        conversation_id: Used to set Langfuse session_id for this trace.
        user_id: Used to set Langfuse user_id for this trace.

    Returns:
        The generated title string
    """
    _lf_ctx = _build_title_langfuse_ctx(conversation_id=conversation_id, user_id=user_id)
    try:
        with _lf_ctx:
            llm = get_llm_with_params(temperature=temperature, max_tokens=max_tokens)
            message = HumanMessage(content=title_prompt)
            response = llm.invoke([message])

        if hasattr(response, 'content'):
            title = response.content.strip()
        else:
            title = str(response).strip()

        if title.startswith('"') and title.endswith('"'):
            title = title[1:-1]
        if title.startswith("'") and title.endswith("'"):
            title = title[1:-1]

        words = title.split()
        max_words = int(max_tokens / 1.3)
        if len(words) > max_words:
            title = " ".join(words[:max_words])

        logger.info(f"Generated title: '{title}'")
        return title if title else "Kubernetes Query"

    except Exception as e:
        logger.error(f"Error generating title: {e}", exc_info=True)
        return "Kubernetes Query"

def format_non_streaming_response(
    request_id: str,
    workflow_result: str,
    model_name: str,
    usage_info: Optional[UsageInfo] = None,
    hitl_required: bool = False,
    action_id: Optional[str] = None,
) -> ChatCompletionResponse:
    response_message = ResponseMessage(role="assistant", content=workflow_result)
    choice = Choice(
        index=0,
        message=response_message,
        finish_reason="stop",
        hitl_required=hitl_required,
        action_id=action_id,
    )
    return ChatCompletionResponse(
        id=request_id,
        created=int(time.time()),
        model=model_name,
        choices=[choice],
        usage=usage_info
    )

def _handle_openai_error(e: OpenAIError) -> HTTPException:
    logger.error(f"OpenAI API Error: {e}", exc_info=True)
    error_type = "api_error"
    status_code = getattr(e, 'status_code', 503)
    if hasattr(e, 'type'):
        error_type = e.type
    elif status_code:
        status_code_map = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error", 429: "rate_limit_error"}
        error_type = status_code_map.get(status_code, "api_error")
    return HTTPException(
        status_code=status_code,
        detail={"error": {"type": error_type, "message": str(e), "code": getattr(e, 'code', None)}}
    )

def _handle_generic_error(e: Exception) -> HTTPException:
    logger.error(f"Unexpected error: {e}", exc_info=True)
    return HTTPException(
        status_code=500,
        detail={"error": {"type": "internal_server_error", "message": f"An unexpected server error occurred: {str(e)}"}}
    )

async def extract_identifiers_from_request(request: ChatCompletionRequest, fastapi_request: FastAPIRequest) -> tuple[Optional[str], Optional[str], bool]:
    """
    Extract conversation_id, user_id, and resume flag from request.
    Priority: conversation_id for HITL, user_id for user-level operations.
    """
    conversation_id = None
    user_id = None
    resume = False
    
    # Try to get from request object attributes first
    conversation_id = getattr(request, "conversation_id", None) or getattr(request, "conversationId", None)
    user_id = getattr(request, "user", None) or getattr(request, "user_id", None) or getattr(request, "userId", None)
    
    # Try from model_extra if Pydantic parsed it
    if hasattr(request, 'model_extra') and isinstance(request.model_extra, dict):
        conversation_id = conversation_id or request.model_extra.get("conversation_id") or request.model_extra.get("conversationId")
        user_id = user_id or request.model_extra.get("user") or request.model_extra.get("user_id") or request.model_extra.get("userId")
        resume = resume or request.model_extra.get('resume', False)
    
    # Try from raw request body as fallback
    if not conversation_id or not user_id:
        try:
            raw_body = getattr(fastapi_request, "_body", None)
            if not raw_body:
                raw_body = await fastapi_request.body()
            if raw_body:
                raw_json = json.loads(raw_body.decode())
                conversation_id = conversation_id or raw_json.get("conversation_id") or raw_json.get("conversationId")
                user_id = user_id or raw_json.get("user") or raw_json.get("user_id") or raw_json.get("userId")
                resume = resume or raw_json.get("resume", False) or raw_json.get("hitl_resume", False)
        except Exception as e:
            logger.warning(f"Could not parse raw request body for identifiers: {e}")
    
    # Try from headers as fallback
    if not conversation_id or not user_id:
        headers = dict(fastapi_request.headers)
        conversation_id = conversation_id or headers.get("x-conversation-id") or headers.get("x-thread-id")
        user_id = user_id or headers.get("x-user-id")
    
    # Also check for resume flag in the request object
    if hasattr(request, 'resume'):
        resume = request.resume
    
    logger.info(f"Extracted identifiers - conversation_id: {conversation_id}, user_id: {user_id}, resume: {resume}")
    return conversation_id, user_id, resume

def convert_to_lc_messages(messages):
    """
    Convert OpenAI-style message dicts to LangChain message objects.
    
    Args:
        messages: List of message dicts with 'role' and 'content' keys
        
    Returns:
        List of LangChain message objects (HumanMessage, AIMessage)
    """
    lc_msgs = []
    for m in messages:
        if m["role"] == "user":
            lc_msgs.append(HumanMessage(content=m["content"]))
        elif m["role"] == "assistant":
            lc_msgs.append(AIMessage(content=m["content"]))
        elif m["role"] == "system":
            lc_msgs.append(SystemMessage(content=m["content"]))
    return lc_msgs


async def _audit(
    query: str,
    outcome: str,
    user_id: str = None,
    conversation_id: str = None,
    agents_invoked: list = None,
    latency_ms: int = None,
    action_id: str = None,
    decision: str = None,
) -> None:
    """Fire-and-forget audit log write. Never raises."""
    try:
        await asyncio.to_thread(
            pg_checkpointer.write_audit_log,
            query=query,
            outcome=outcome,
            user_id=user_id,
            conversation_id=conversation_id,
            agents_invoked=agents_invoked,
            action_id=action_id,
            decision=decision,
            latency_ms=latency_ms,
        )
    except Exception as e:
        logger.debug(f"Audit log write skipped: {e}")


async def _summarize_older_messages(messages: List[dict]) -> str:
    """Summarize older conversation messages into a concise context string for the supervisor.

    Called only when the conversation exceeds CONVERSATION_SUMMARY_THRESHOLD messages.
    Uses a fast LLM call capped at 400 tokens. Failures are non-fatal (returns empty string).
    """
    try:
        lines = []
        for m in messages:
            role = m["role"].upper()
            content = m["content"][:600]
            lines.append(f"{role}: {content}")
        conversation_text = "\n".join(lines)
        prompt = (
            "Summarize this Kubernetes operations conversation. "
            "Your output MUST have two sections:\n\n"
            "1. **Facts established** (key=value, one per line): list every concrete fact confirmed in this conversation — "
            "e.g. deployment=mohsen-test-webapp, namespace=default, image=nginx, replicas=3, port=80. "
            "Include every resource name, namespace, image, port, or config value that was mentioned or confirmed. "
            "If a fact was corrected later (e.g. namespace changed from 'default' to 'production'), use the final value.\n\n"
            "2. **Narrative** (2-4 sentences): what was asked, which Kubernetes resources were created/modified/deleted, "
            "and the current cluster state.\n\n"
            "Conversation:\n" + conversation_text + "\n\nOutput:"
        )
        llm = get_llm_with_params(temperature=0, max_tokens=400)
        result = await llm.ainvoke(prompt)
        return result.content.strip()
    except Exception as e:
        logger.debug(f"Conversation summarization failed (non-critical): {e}")
        return ""


async def _cache_summary(
    conversation_id: str,
    total_message_count: int,
    older_messages: List[dict],
) -> None:
    """Background task: summarize older_messages and write the result to the Postgres summary cache.

    Cache key: (conversation_id, total_message_count).
    Failures are non-fatal — logged at DEBUG and counted as cache misses.
    """
    try:
        summary_text = await _summarize_older_messages(older_messages)
        if summary_text:
            pg_checkpointer.write_summary_cache(conversation_id, total_message_count, summary_text)
    except Exception as exc:
        logger.debug("summary cache background task failed: %s", exc)
        summary_cache_miss_total.inc()


async def prepare_messages_for_workflow(
    request_messages: List[ChatMessage],
    window_size: int = None,
    is_title_generation: bool = False,
    pinned_context_msg: Optional[str] = None,
    conversation_id: Optional[str] = None,
    conversation_context: Optional[Dict[str, Any]] = None,
) -> List:
    """
    Prepare messages for workflow by trimming to window size and converting to LangChain objects.

    When the conversation exceeds CONVERSATION_SUMMARY_THRESHOLD messages, the older portion
    (beyond the SHORT_TERM_MEMORY_WINDOW) is summarized via LLM and prepended as a
    SystemMessage so the supervisor retains full-conversation context without blowing the
    token budget.

    Args:
        request_messages: List of ChatMessage objects from the request
        window_size: Number of recent messages to keep (default: SHORT_TERM_MEMORY_WINDOW)
        is_title_generation: If True, convert system messages to user messages for title generation
        conversation_id: Used for summary cache keying (optional — skips caching if None)
        conversation_context: Already-loaded working context dict (namespace, resource_name, etc.)
            If provided and the conversation is being summarised, appends a context line to summary_text.

    Returns:
        List of LangChain message objects ready for workflow
    """
    if window_size is None:
        window_size = SHORT_TERM_MEMORY_WINDOW

    # Convert to dicts for processing
    message_dicts = [{"role": msg.role, "content": msg.content} for msg in request_messages]

    # For title generation, convert system messages to user messages
    if is_title_generation:
        for msg_dict in message_dicts:
            if msg_dict["role"] == "system":
                msg_dict["role"] = "user"

    total = len(message_dicts)

    # Summarize older messages when the conversation is long enough
    if (
        settings.CONVERSATION_SUMMARY_ENABLED
        and total > settings.CONVERSATION_SUMMARY_THRESHOLD
    ):
        older = message_dicts[:-window_size]
        recent = message_dicts[-window_size:]

        # Try the summary cache before making a live LLM call.
        summary_text = None
        cache_key = total - window_size  # matches the key written by the previous request
        if conversation_id:
            cached = pg_checkpointer.read_summary_cache(conversation_id, cache_key)
            if cached:
                summary_text = cached
                summary_cache_hit_total.inc()
                logger.debug("Summary cache hit for conversation %s (key=%d)", conversation_id, cache_key)
            else:
                summary_cache_miss_total.inc()

        if not summary_text:
            summary_text = await _summarize_older_messages(older)
            # Fire-and-forget: cache the summary for the next request.
            if conversation_id and summary_text:
                asyncio.create_task(_cache_summary(conversation_id, total, older))

        # Fix 3: append working context to summary so supervisor retains namespace/resource
        # across the full conversation even when older messages have been summarised.
        if summary_text and conversation_context:
            ns = (conversation_context.get("namespace") or "").strip()
            res = (conversation_context.get("resource_name") or "").strip()
            if ns or res:
                parts = []
                if ns:
                    parts.append(f"namespace={ns}")
                if res:
                    parts.append(f"resource={res}")
                summary_text = summary_text + "\n[Context: " + ", ".join(parts) + "]"

        if summary_text:
            summary_msg = {"role": "system", "content": f"[Conversation summary: {summary_text}]"}
            trimmed_messages = [summary_msg] + recent
            logger.info(
                f"Summarized {len(older)} older messages into context summary "
                f"(trimmed from {total} total messages)"
            )
        else:
            trimmed_messages = recent
            logger.info(f"Prepared {len(recent)} messages for workflow (trimmed from {total} total messages)")
    else:
        if total > window_size:
            recent = message_dicts[-window_size:]
            # Anchor Fix: ensure the first HumanMessage is always present so the
            # supervisor retains the original user intent across long conversations.
            first_human = next(
                (m for m in message_dicts if m.get("role") == "user"), None
            )
            if first_human and first_human not in recent:
                recent = [first_human] + recent
            trimmed_messages = recent
            logger.info(f"Prepared {len(trimmed_messages)} messages for workflow (trimmed from {total} total)")
        else:
            trimmed_messages = message_dicts
            logger.info(f"Prepared {len(trimmed_messages)} messages for workflow (no trimming)")

    # Inject the pinned working context AFTER all trimming/summarisation so it is
    # never evicted by the window slice.  Placed at index-0 so it survives even
    # when the conversation summary exists (summary will be at index 1).
    if pinned_context_msg:
        trimmed_messages.insert(0, {"role": "system", "content": pinned_context_msg})
        logger.debug("Injected pinned K8s context message: %.120s", pinned_context_msg)

    # Detect truncated assistant messages — LibreChat can cut off long responses
    # before sending them back as conversation history. A truncated prior message
    # confuses the supervisor (it looks like the previous response is incomplete)
    # and can cause the user to repeat the request or the supervisor to misroute.
    for i, msg in enumerate(trimmed_messages[:-1]):  # skip the final (user) message
        if msg.get("role") == "assistant":
            content = msg.get("content") or ""
            if len(content) > 500:
                last_chars = content[-20:]
                # Heuristic: no terminal punctuation or newline → likely truncated
                if not any(c in last_chars for c in ".!?\n"):
                    logger.warning(
                        "Possible truncated assistant message at history index %d "
                        "(ends with: %r) — appending truncation note",
                        i, content[-40:],
                    )
                    msg["content"] = content + "\n[Note: previous response may be truncated]"

    lc_messages = convert_to_lc_messages(trimmed_messages)
    return lc_messages

@router.post(
    "/chat/completions",
    response_model=None,
    responses={
        200: {
            "description": "Successful response. Can be a ChatCompletionResponse or a stream of ChatCompletionChunk.",
            "content": {
                "application/json": {
                    "schema": ChatCompletionResponse.model_json_schema()
                },
                "text/event-stream": {
                    "schema": {"type": "string", "example": "data: {...}\n\ndata: [DONE]\n\n"}
                }
            }
        },
        400: {"model": ErrorResponse, "description": "Bad Request"},
        401: {"model": ErrorResponse, "description": "Unauthorized (e.g. invalid API key)"},
        429: {"model": ErrorResponse, "description": "Rate Limit Exceeded"},
        500: {"model": ErrorResponse, "description": "Internal Server Error"},
        503: {"model": ErrorResponse, "description": "Service Unavailable / AI Workflow Error"}
    },
    summary="Creates a model response for the given chat conversation.",
    tags=["Chat Completions"]
)
async def create_chat_completion(
    fastapi_request: FastAPIRequest,
    request: ChatCompletionRequest = Body(...)
):
    request_id = f"chatcmpl-{uuid.uuid4()}"
    request_id_var.set(request_id)
    created_time = int(time.time())
    effective_model_name = get_effective_model_name(request.model)

    # --- Extract user_id for HITL ---
    conversation_id, user_id, resume = await extract_identifiers_from_request(request, fastapi_request)

    # (Optional: You can log extra HITL info here.)

    # === COMPREHENSIVE DEBUG LOGGING START ===
    # (You can keep your previous debug logging here as in your last version.)
    # === COMPREHENSIVE DEBUG LOGGING END ===

    try:
        user_query_content, is_title_generation = await validate_chat_completion_request(request)
        logger.info(f"Request ID {request_id}: Extracted user query: '{user_query_content[:100]}...'")

        # Handle title generation requests separately - bypass workflow
        if is_title_generation:
            logger.info(f"Request ID {request_id}: Handling title generation request directly")
            
            # Get max_tokens and temperature from request if available
            max_tokens = 16  # Default for title generation
            temperature = 0.2  # Default temperature for title generation
            
            if hasattr(request, 'model_extra') and isinstance(request.model_extra, dict):
                max_tokens = request.model_extra.get('max_tokens') or request.model_extra.get('maxTokens') or 16
                temperature = request.model_extra.get('temperature', 0.2)
            else:
                max_tokens = getattr(request, 'max_tokens', None) or getattr(request, 'maxTokens', None) or 16
                temperature = getattr(request, 'temperature', 0.2)
            
            # Generate title using direct LLM call
            title = await generate_title_directly(
                user_query_content,
                max_tokens=max_tokens,
                temperature=temperature,
                conversation_id=conversation_id,
                user_id=user_id,
            )
            
            # Return the title as a response
            if request.stream:
                async def title_stream_generator() -> AsyncGenerator[str, None]:
                    delta = ChatCompletionStreamDelta(role="assistant", content=title)
                    choice = ChatCompletionStreamChoice(index=0, delta=delta, finish_reason="stop")
                    chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[choice])
                    yield f"data: {chunk.model_dump_json()}\n\n"
                    
                    final_delta = ChatCompletionStreamDelta()
                    final_choice = ChatCompletionStreamChoice(index=0, delta=final_delta, finish_reason="stop")
                    final_chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[final_choice])
                    yield f"data: {final_chunk.model_dump_json()}\n\n"
                    yield "data: [DONE]\n\n"
                
                return StreamingResponse(title_stream_generator(), media_type="text/event-stream")
            else:
                response = format_non_streaming_response(
                    request_id=request_id,
                    workflow_result=title,
                    model_name=effective_model_name,
                )
                return response

        # --- Scope check: reject clearly off-topic queries before the workflow ---
        scope = query_processor.check_scope(user_query_content)
        if not scope.in_scope:
            logger.info(f"Request ID {request_id}: Out-of-scope query rejected by QueryProcessor.")
            if request.stream:
                async def oos_stream_generator() -> AsyncGenerator[str, None]:
                    delta = ChatCompletionStreamDelta(role="assistant", content=scope.rejection_message)
                    choice = ChatCompletionStreamChoice(index=0, delta=delta, finish_reason="stop")
                    chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[choice])
                    yield f"data: {chunk.model_dump_json()}\n\n"
                    final_delta = ChatCompletionStreamDelta()
                    final_choice = ChatCompletionStreamChoice(index=0, delta=final_delta, finish_reason="stop")
                    final_chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[final_choice])
                    yield f"data: {final_chunk.model_dump_json()}\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(oos_stream_generator(), media_type="text/event-stream")
            else:
                return format_non_streaming_response(
                    request_id=request_id,
                    workflow_result=scope.rejection_message,
                    model_name=effective_model_name,
                )

        # Load persistent working context (namespace, resource) for this conversation.
        # The context is injected as a pinned system message that survives message-window
        # trimming, so agents never have to re-ask the user for the namespace.
        #
        # Scope-aware suppression: skip injection when the query is clearly global/cross-namespace.
        # Injecting a saved namespace into a cross-scope query over-constrains the agent and causes
        # wrong answers (e.g. "show logs for pod with MOST restarts" should not be pinned to a
        # previously active namespace). Validated by the window=1 ablation experiment (2026-04-06).
        _GLOBAL_SCOPE_SIGNALS = (
            "all namespaces", "across namespaces", "every namespace",
            "cluster-wide", "across the cluster", "in the cluster",
            "across all", "in all", "most restarts", "most cpu", "most memory",
            "highest cpu", "highest memory", "highest restart",
        )
        _query_is_global = any(
            sig in user_query_content.lower() for sig in _GLOBAL_SCOPE_SIGNALS
        )

        _pinned_ctx_msg: Optional[str] = None
        _loaded_ctx: Optional[Dict[str, Any]] = None
        if conversation_id and settings.CONVERSATION_CONTEXT_ENABLED and not _query_is_global:
            try:
                import app.orchestration.workflow as _wf_mod
                from app.services.conversation_context_service import (
                    load_context as _load_ctx,
                    format_context_pinned_message as _fmt_ctx,
                )
                if _wf_mod._langgraph_pool:
                    _loaded_ctx = await _load_ctx(conversation_id, _wf_mod._langgraph_pool)
                    _pinned_ctx_msg = _fmt_ctx(_loaded_ctx) if _loaded_ctx else None
                    if _pinned_ctx_msg:
                        logger.debug(
                            "Loaded pinned K8s context for conversation %s: %s",
                            conversation_id, _pinned_ctx_msg[:100],
                        )
            except Exception as _ctx_load_exc:
                logger.debug("Could not load conversation context (non-critical): %s", _ctx_load_exc)
        elif _query_is_global:
            logger.debug(
                "Skipping context injection for global-scope query (conversation %s): %s",
                conversation_id, user_query_content[:80],
            )

        # Prepare messages for workflow with short-term memory (and optional summarization)
        lc_messages = await prepare_messages_for_workflow(
            request.messages,
            is_title_generation=is_title_generation,
            pinned_context_msg=_pinned_ctx_msg,
            conversation_id=conversation_id,
            conversation_context=_loaded_ctx,
        )
        logger.info(f"Request ID {request_id}: Prepared {len(lc_messages)} messages for workflow")

        # Check if this is an approval/denial response for HITL (CodeGenerator)
        is_approval_response, approved, original_query = \
            await check_for_hitl_approval_response(user_query_content, conversation_id, user_id)

        if is_approval_response:
            logger.info(f"Request ID {request_id}: Detected HITL approval response: approved={approved}")
            hitl_decisions_total.labels(decision="approved" if approved else "denied").inc()
            log_audit_event(
                logger,
                "hitl_approval_response",
                actor=user_id,
                outcome="approved" if approved else "denied",
                details=f"conversation_id={conversation_id}",
            )
            # Record the human decision as a Langfuse score on the session so it
            # appears in cost/quality dashboards filtered by session_id.
            _thread_id = f"thread_{conversation_id}" if conversation_id else None
            try:
                _lf = get_langfuse_client()
                if _lf and _thread_id:
                    _lf.create_score(
                        session_id=_thread_id,
                        name="hitl_decision",
                        value=1.0 if approved else 0.0,
                        data_type="BOOLEAN",
                        comment="approved" if approved else "denied",
                    )
            except Exception as _e:
                logger.debug(f"Langfuse HITL score failed (non-critical): {_e}")

            _hitl_action_id = getattr(request, "action_id", None)
            if not _hitl_action_id and hasattr(request, "model_extra") and isinstance(request.model_extra, dict):
                _hitl_action_id = request.model_extra.get("action_id")

            if not approved:
                # User denied/cancelled — audit then cleanup and return message
                asyncio.ensure_future(_audit(
                    query=original_query or user_query_content,
                    outcome="hitl_denied",
                    user_id=user_id,
                    conversation_id=conversation_id,
                    action_id=_hitl_action_id,
                    decision="denied",
                ))
                primary_id = conversation_id or user_id
                thread_id = f"thread_{conversation_id}" if conversation_id else (
                    f"thread_{user_id}" if user_id else None
                )
                if thread_id:
                    try:
                        pg_checkpointer.delete_checkpoint(primary_id, thread_id)
                        logger.info(f"Request ID {request_id}: Cleaned up checkpoint after user denial")
                    except Exception as e:
                        logger.warning(f"Request ID {request_id}: Could not clear checkpoint after user denial: {e}")

                # Generate a verbal reflection so the supervisor avoids repeating the
                # same mistake in subsequent turns of this session.
                try:
                    from app.utils.reflexion import generate_reflection
                    from app.core.llm_gateway import get_code_gen_llm as _get_cg_llm
                    _reflection_llm = _get_cg_llm()
                    _reflection = generate_reflection(
                        failed_action=original_query[:300] if original_query else "proposed action",
                        rejection_reason=user_query_content[:200],
                        llm=_reflection_llm,
                    )
                    logger.info(f"Request ID {request_id}: Stored reflection: {_reflection[:120]}")
                    # We store the reflection in the Langfuse score comment field for
                    # observability; state injection happens on the next workflow call.
                    try:
                        _lf = get_langfuse_client()
                        if _lf and thread_id:
                            _lf.create_score(
                                session_id=thread_id,
                                name="hitl_reflection",
                                value=0.0,
                                data_type="BOOLEAN",
                                comment=_reflection,
                            )
                    except Exception:
                        pass  # non-critical
                except Exception as _ref_err:
                    logger.warning(f"Request ID {request_id}: Reflection generation skipped: {_ref_err}")

                denial_message = "❌ **Action Denied**\n\nThe CodeGenerator action was denied by the user. The workflow has been stopped for security reasons.\n\nYou can continue with other requests or ask new questions."
                
                # Ensure we always return a valid response, even if there are errors
                try:
                    if request.stream:
                        async def denial_stream_generator() -> AsyncGenerator[str, None]:
                            delta = ChatCompletionStreamDelta(role="assistant", content=denial_message)
                            choice = ChatCompletionStreamChoice(index=0, delta=delta, finish_reason="stop")
                            chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[choice])
                            yield f"data: {chunk.model_dump_json()}\n\n"
                            
                            final_delta = ChatCompletionStreamDelta()
                            final_choice = ChatCompletionStreamChoice(index=0, delta=final_delta, finish_reason="stop")
                            final_chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[final_choice])
                            yield f"data: {final_chunk.model_dump_json()}\n\n"
                            yield "data: [DONE]\n\n"
                        
                        return StreamingResponse(denial_stream_generator(), media_type="text/event-stream")
                    else:
                        response = format_non_streaming_response(
                            request_id=request_id,
                            workflow_result=denial_message,
                            model_name=effective_model_name,
                        )
                        return response
                except Exception as e:
                    # Fallback error handling - ensure we never crash
                    logger.error(f"Request ID {request_id}: Error formatting denial response: {e}", exc_info=True)
                    # Return a simple error response
                    error_message = "Action was denied. You can continue with other requests."
                    if request.stream:
                        async def error_stream() -> AsyncGenerator[str, None]:
                            delta = ChatCompletionStreamDelta(role="assistant", content=error_message)
                            choice = ChatCompletionStreamChoice(index=0, delta=delta, finish_reason="stop")
                            chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[choice])
                            yield f"data: {chunk.model_dump_json()}\n\n"
                            yield "data: [DONE]\n\n"
                        return StreamingResponse(error_stream(), media_type="text/event-stream")
                    else:
                        return format_non_streaming_response(
                            request_id=request_id,
                            workflow_result=error_message,
                            model_name=effective_model_name,
                        )
            
            # User approved - audit the decision then resume the workflow
            asyncio.ensure_future(_audit(
                query=original_query or user_query_content,
                outcome="hitl_approved",
                user_id=user_id,
                conversation_id=conversation_id,
                action_id=_hitl_action_id,
                decision="approved",
            ))
            logger.info(f"Request ID {request_id}: User approved, resuming workflow")
            
            if request.stream:
                async def approval_stream_generator() -> AsyncGenerator[str, None]:
                    # First, send an acknowledgment
                    ack_message = "✅ **Approved** - Resuming workflow...\n\n"
                    delta = ChatCompletionStreamDelta(role="assistant", content=ack_message)
                    choice = ChatCompletionStreamChoice(index=0, delta=delta, finish_reason=None)
                    chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[choice])
                    yield f"data: {chunk.model_dump_json()}\n\n"
                    
                    # Then resume the workflow using the SAME user_id to ensure thread continuity
                    try:
                        resume_last_content = ""
                        # For resume, we still need to pass the original query as a single message
                        resume_messages = [HumanMessage(content=original_query or user_query_content)]
                        async for workflow_event in run_kubeintellect_workflow(
                            messages=resume_messages,
                            conversation_id=conversation_id,  # Use conversation_id for HITL checkpointing
                            user_id=user_id,  # Keep user_id for user-level operations
                            stream=request.stream,
                            resume=True,
                        ):
                            if workflow_event.get("type") == "error":
                                error_content = workflow_event["data"]
                                try:
                                    error_delta = ChatCompletionStreamDelta(content=f"\n\n[AI Workflow Error]: {error_content}")
                                    error_choice = ChatCompletionStreamChoice(index=0, delta=error_delta, finish_reason="error")
                                    error_chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[error_choice])
                                    yield f"data: {error_chunk.model_dump_json()}\n\n"
                                except Exception as yield_error:
                                    logger.critical(f"Request ID {request_id}: Failed to yield error message: {yield_error}", exc_info=True)
                                return
                            elif workflow_event.get("type") == "content_delta":
                                # Stream each token immediately.
                                delta_content = str(workflow_event["data"])
                                resume_last_content += delta_content
                                delta = ChatCompletionStreamDelta(content=delta_content)
                                choice = ChatCompletionStreamChoice(index=0, delta=delta, finish_reason=None)
                                chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[choice])
                                yield f"data: {chunk.model_dump_json()}\n\n"
                            elif workflow_event.get("type") == "workflow_complete":
                                # Only send final content if nothing was streamed yet.
                                final_content = workflow_event.get("data") or resume_last_content
                                if not resume_last_content and final_content:
                                    delta = ChatCompletionStreamDelta(content=final_content)
                                    choice = ChatCompletionStreamChoice(index=0, delta=delta, finish_reason=None)
                                    chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[choice])
                                    yield f"data: {chunk.model_dump_json()}\n\n"
                                break
                        
                        # Send final chunk
                        final_delta = ChatCompletionStreamDelta()
                        final_choice = ChatCompletionStreamChoice(index=0, delta=final_delta, finish_reason="stop")
                        final_chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[final_choice])
                        yield f"data: {final_chunk.model_dump_json()}\n\n"
                        
                    except Exception as e:
                        logger.error(f"Request ID {request_id}: Error during approval resume: {e}", exc_info=True)
                        try:
                            error_delta = ChatCompletionStreamDelta(content="\n\nAn error occurred while resuming the workflow. Please try again or start a new request.")
                            error_choice = ChatCompletionStreamChoice(index=0, delta=error_delta, finish_reason="error")
                            error_chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[error_choice])
                            yield f"data: {error_chunk.model_dump_json()}\n\n"
                        except Exception as yield_error:
                            logger.critical(f"Request ID {request_id}: Failed to yield error message: {yield_error}", exc_info=True)
                    finally:
                        yield "data: [DONE]\n\n"
                
                return StreamingResponse(approval_stream_generator(), media_type="text/event-stream")
            
            else:
                # Non-streaming approval
                ack_parts = ["✅ **Approved** - Resuming workflow...\n\n"]
                
                # For resume, we still need to pass the original query as a single message
                resume_messages = [HumanMessage(content=original_query or user_query_content)]
                async for workflow_event in run_kubeintellect_workflow(
                    messages=resume_messages,
                    conversation_id=conversation_id,  # Use conversation_id for HITL checkpointing
                    user_id=user_id,  # Keep user_id for user-level operations
                    stream=request.stream,
                    resume=True,
                ):
                    if workflow_event.get("type") == "error":
                        error_message = workflow_event["data"]
                        raise HTTPException(
                            status_code=500, 
                            detail={"error": {"type": "internal_server_error", "message": f"Error during HITL resume: {error_message}"}}
                        )
                    elif workflow_event.get("type") == "content_delta":
                        ack_parts.append(str(workflow_event["data"]))
                    elif workflow_event.get("type") == "workflow_complete":
                        if workflow_event.get("data"):
                            ack_parts.append(str(workflow_event["data"]))
                        break
                
                final_content = "".join(ack_parts)
                response = format_non_streaming_response(
                    request_id=request_id,
                    workflow_result=final_content,
                    model_name=effective_model_name,
                )
                return response

        # Continue with regular workflow if not an approval response
        _wf_start = time.perf_counter()
        if request.stream:
            async def stream_generator() -> AsyncGenerator[str, None]:
                logger.info(f"Request ID {request_id}: Starting stream response generation.")
                stream_started_time = time.perf_counter()
                idx = 0
                accumulated_content_for_log = ""
                _stream_agents: list = []
                try:
                    async for workflow_event in run_kubeintellect_workflow(
                        messages=lc_messages,
                        conversation_id=conversation_id,
                        user_id=user_id,
                        stream=request.stream,
                        resume=resume,
                    ):
                        if await fastapi_request.is_disconnected():
                            logger.warning(f"Request ID {request_id}: Client disconnected during stream.")
                            break
                        
                        if workflow_event.get("type") == "error":
                            error_content = workflow_event["data"]
                            logger.error(f"Request ID {request_id}: Workflow yielded an error message: {error_content}")
                            try:
                                delta = ChatCompletionStreamDelta(content=f"\n\n[AI Workflow Error]: {error_content}")
                                choice = ChatCompletionStreamChoice(index=idx, delta=delta, finish_reason="error")
                                chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[choice])
                                yield f"data: {chunk.model_dump_json()}\n\n"
                            except Exception as yield_error:
                                logger.critical(f"Request ID {request_id}: Failed to yield error message: {yield_error}", exc_info=True)
                            return

                        elif workflow_event.get("type") == "breakpoint":
                            breakpoint_message = workflow_event["data"]
                            hitl_action_id = str(uuid.uuid4())
                            logger.info(f"Request ID {request_id}: Workflow paused for HITL approval: {breakpoint_message} action_id={hitl_action_id}")
                            # Send the breakpoint message as a content delta
                            delta = ChatCompletionStreamDelta(
                                role="assistant" if idx == 0 else None,
                                content=f"\n\n{breakpoint_message}"
                            )
                            choice = ChatCompletionStreamChoice(index=0, delta=delta, finish_reason="stop")
                            chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[choice])
                            yield f"data: {chunk.model_dump_json()}\n\n"
                            # Send final chunk — hitl_required + action_id let CLI detect HITL without emoji scan
                            final_delta = ChatCompletionStreamDelta()
                            final_choice = ChatCompletionStreamChoice(
                                index=0, delta=final_delta, finish_reason="stop",
                                hitl_required=True, action_id=hitl_action_id,
                            )
                            final_chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[final_choice])
                            yield f"data: {final_chunk.model_dump_json()}\n\n"
                            return

                        elif workflow_event.get("type") == "plan_preview":
                            # Supervisor committed a multi-step plan — stream the preview text
                            # so the user sees what's about to execute before any agent runs.
                            _preview_content = str(workflow_event.get("data") or "")
                            if _preview_content:
                                delta = ChatCompletionStreamDelta(
                                    role="assistant" if idx == 0 else None,
                                    content=_preview_content,
                                )
                                choice = ChatCompletionStreamChoice(index=0, delta=delta, finish_reason=None)
                                chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[choice])
                                yield f"data: {chunk.model_dump_json()}\n\n"
                                idx += 1

                        elif workflow_event.get("type") == "content_delta":
                            # Stream each token immediately as it arrives from the agent LLM.
                            delta_content = str(workflow_event["data"])
                            accumulated_content_for_log += delta_content
                            delta = ChatCompletionStreamDelta(
                                role="assistant" if idx == 0 else None,
                                content=delta_content,
                            )
                            choice = ChatCompletionStreamChoice(index=0, delta=delta, finish_reason=None)
                            chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[choice])
                            yield f"data: {chunk.model_dump_json()}\n\n"
                            idx += 1

                        elif workflow_event.get("type") == "workflow_complete":
                            _stream_agents = workflow_event.get("agents") or []
                            final_content = workflow_event.get("data") or accumulated_content_for_log
                            logger.info(
                                "workflow_complete",
                                extra={
                                    "event": "workflow_complete",
                                    "request_id": request_id,
                                    "accumulated_preview": accumulated_content_for_log[:100],
                                },
                            )
                            if final_content and accumulated_content_for_log and final_content[:100] != accumulated_content_for_log[:100]:
                                logger.debug(
                                    "stream_state_divergence",
                                    extra={
                                        "event": "stream_state_divergence",
                                        "request_id": request_id,
                                        "streamed_preview": accumulated_content_for_log[:100],
                                        "graph_state_preview": final_content[:100],
                                    },
                                )
                            asyncio.ensure_future(_audit(
                                query=user_query_content,
                                outcome="success",
                                user_id=user_id,
                                conversation_id=conversation_id,
                                agents_invoked=_stream_agents,
                                latency_ms=int((time.perf_counter() - _wf_start) * 1000),
                            ))
                            # Only send final_content if nothing was already streamed token-by-token.
                            # This covers the supervisor-only path (e.g. out-of-scope, welcome messages)
                            # where no content_delta events are emitted during the workflow.
                            if idx == 0 and final_content:
                                delta = ChatCompletionStreamDelta(role="assistant", content=final_content)
                                choice = ChatCompletionStreamChoice(index=0, delta=delta, finish_reason=None)
                                chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[choice])
                                yield f"data: {chunk.model_dump_json()}\n\n"
                                idx += 1
                                accumulated_content_for_log = final_content

                    if not await fastapi_request.is_disconnected():
                        final_delta = ChatCompletionStreamDelta()
                        final_choice = ChatCompletionStreamChoice(index=0, delta=final_delta, finish_reason="stop")
                        final_chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[final_choice])
                        yield f"data: {final_chunk.model_dump_json()}\n\n"
                        logger.info(
                            "stream_complete",
                            extra={
                                "event": "stream_complete",
                                "request_id": request_id,
                                "duration_ms": int((time.perf_counter() - stream_started_time) * 1000),
                                "chunks": idx,
                                "chars": len(accumulated_content_for_log),
                            },
                        )
                        stream_completions_total.labels(status="success").inc()

                except Exception as e:
                    logger.error(
                        "stream_error",
                        extra={
                            "event": "stream_error",
                            "request_id": request_id,
                            "duration_ms": int((time.perf_counter() - stream_started_time) * 1000),
                            "error": str(e),
                        },
                        exc_info=True,
                    )
                    stream_completions_total.labels(status="error").inc()
                    try:
                        error_delta = ChatCompletionStreamDelta(content="\n\n[Internal Server Error]: An unexpected error occurred while processing your stream.")
                        error_choice = ChatCompletionStreamChoice(index=idx, delta=error_delta, finish_reason="error")
                        error_chunk = ChatCompletionChunk(id=request_id, created=created_time, model=effective_model_name, choices=[error_choice])
                        yield f"data: {error_chunk.model_dump_json()}\n\n"
                    except Exception:
                        pass
                finally:
                    if not await fastapi_request.is_disconnected():
                        yield "data: [DONE]\n\n"
                        logger.debug(f"Request ID {request_id}: Sent [DONE] marker.")
                    else:
                        logger.warning(f"Request ID {request_id}: Client disconnected, [DONE] marker not sent.")

            return StreamingResponse(stream_generator(), media_type="text/event-stream")

        else:
            logger.info(f"Request ID {request_id}: Starting non-streaming response generation.")
            full_content_parts = []
            final_consolidated_message_from_workflow = ""
            error_message_from_workflow = None
            breakpoint_message_from_workflow = None

            _nonstream_agents: list = []
            async for workflow_event in run_kubeintellect_workflow(
                messages=lc_messages,
                conversation_id=conversation_id,
                user_id=user_id,
                stream=request.stream,
                resume=resume,
            ):
                if workflow_event.get("type") == "error":
                    error_message_from_workflow = workflow_event["data"]
                    logger.error(f"Request ID {request_id}: Workflow error for non-streaming: {error_message_from_workflow}")
                    break
                elif workflow_event.get("type") == "breakpoint":
                    breakpoint_message_from_workflow = workflow_event["data"]
                    logger.info(f"Request ID {request_id}: Workflow paused for HITL approval: {breakpoint_message_from_workflow}")
                    break
                elif workflow_event.get("type") == "content_delta":
                    full_content_parts.append(str(workflow_event["data"]))
                elif workflow_event.get("type") == "workflow_complete":
                    final_consolidated_message_from_workflow = str(workflow_event.get("data", ""))
                    _nonstream_agents = workflow_event.get("agents") or []
                    break

            if error_message_from_workflow:
                status_code = 503 if "not available" in error_message_from_workflow else 500
                raise HTTPException(status_code=status_code, detail={"error": {"type": "internal_server_error" if status_code == 500 else "service_unavailable", "message": f"AI workflow error: {error_message_from_workflow}"}})

            if breakpoint_message_from_workflow:
                # Handle breakpoint in non-streaming mode
                hitl_action_id = str(uuid.uuid4())
                logger.info(f"Request ID {request_id}: Returning breakpoint response for non-streaming. action_id={hitl_action_id}")
                breakpoint_response = f"\n\n{breakpoint_message_from_workflow}"
                chat_completion_response = format_non_streaming_response(
                    request_id=request_id,
                    workflow_result=breakpoint_response,
                    model_name=effective_model_name,
                    hitl_required=True,
                    action_id=hitl_action_id,
                )
                return chat_completion_response

            final_workflow_content = "".join(full_content_parts)
            if not final_workflow_content and final_consolidated_message_from_workflow:
                final_workflow_content = final_consolidated_message_from_workflow
            elif not final_workflow_content:
                final_workflow_content = "Task processed successfully, but no specific textual output was generated by the AI."

            logger.info(f"Request ID {request_id}: Aggregated final content for non-streaming: '{final_workflow_content[:200]}...'")
            chat_completion_response = format_non_streaming_response(
                request_id=request_id,
                workflow_result=final_workflow_content,
                model_name=effective_model_name,
            )
            asyncio.ensure_future(_audit(
                query=user_query_content,
                outcome="success",
                user_id=user_id,
                conversation_id=conversation_id,
                agents_invoked=_nonstream_agents,
                latency_ms=int((time.perf_counter() - _wf_start) * 1000),
            ))
            logger.info(f"Request ID {request_id}: Successfully formatted non-streaming response.")
            return chat_completion_response

    except OpenAIError as e:
        logger.warning(f"Request ID {request_id}: OpenAIError caught in main handler: {e}", exc_info=True)
        raise _handle_openai_error(e)
    except HTTPException as e:
        logger.warning(f"Request ID {request_id}: HTTPException caught: {e.detail}", exc_info=False if e.status_code < 500 else True)
        raise e
    except Exception as e:
        logger.error(f"Request ID {request_id}: Unexpected generic error caught in main handler: {e}", exc_info=True)
        raise _handle_generic_error(e)

@router.post(
    "/chat/hitl/resume",
    response_model=None,
    responses={
        200: {"description": "HITL resume response", "content": {"application/json": {"schema": ChatCompletionResponse.model_json_schema()}}},
        400: {"model": ErrorResponse, "description": "Bad Request"},
        404: {"model": ErrorResponse, "description": "No checkpoint found for user"},
        500: {"model": ErrorResponse, "description": "Internal Server Error"}
    },
    summary="Resume a paused HITL workflow after user approval/denial",
    tags=["Chat Completions", "HITL"]
)
async def resume_hitl_workflow(
    request: HITLResumeRequest = Body(...)
):
    """
    Resume a HITL workflow after user approval or denial.
    This endpoint is called when the user responds to a breakpoint.
    """
    request_id = f"hitl-resume-{uuid.uuid4()}"
    request_id_var.set(request_id)
    effective_model_name = get_effective_model_name(None)

    logger.info(f"HITL Resume Request ID {request_id}: conversation_id={request.conversation_id}, user_id={request.user_id}, approved={request.approved}")
    log_audit_event(
        logger,
        "hitl_resume_request",
        actor=request.user_id,
        outcome="approved" if request.approved else "denied",
        details=f"conversation_id={request.conversation_id}",
    )
    
    try:
        if not request.approved:
            # User denied the action - return a message and cleanup checkpoint
            # Construct thread_id from conversation_id (matching workflow.py pattern)
            effective_user_id = request.user_id or request.conversation_id
            thread_id = f"thread_{request.conversation_id}"
            try:
                pg_checkpointer.delete_checkpoint(effective_user_id, thread_id)
                logger.info(f"HITL Resume Request ID {request_id}: Cleaned up checkpoint after user denial")
            except Exception as e:
                logger.warning(f"HITL Resume Request ID {request_id}: Could not clear checkpoint after user denial: {e}")
            denial_message = "The CodeGenerator action was denied by the user. The workflow has been stopped for security reasons.\n\nYou can continue with other requests or ask new questions."
            try:
                response = format_non_streaming_response(
                    request_id=request_id,
                    workflow_result=denial_message,
                    model_name=effective_model_name,
                )
                return response
            except Exception as e:
                logger.error(f"HITL Resume Request ID {request_id}: Error formatting denial response: {e}", exc_info=True)
                # Fallback response
                return format_non_streaming_response(
                    request_id=request_id,
                    workflow_result="Action was denied. You can continue with other requests.",
                    model_name=effective_model_name,
                )
        
        # User approved - resume the workflow
        logger.info(f"HITL Resume Request ID {request_id}: User approved, resuming workflow")
        
        # Create a dummy message for resume - the actual state will be restored from checkpoint
        resume_query = request.original_query or "Resume approved action"
        resume_messages = [HumanMessage(content=resume_query)]
        
        full_content_parts = []
        final_consolidated_message = ""
        error_message = None
        
        async for workflow_event in run_kubeintellect_workflow(
            messages=resume_messages,
            conversation_id=request.conversation_id,
            user_id=request.user_id,
            stream=False,
            resume=True,  # This is the key - tells workflow to resume from checkpoint
        ):
            if workflow_event.get("type") == "error":
                error_message = workflow_event["data"]
                logger.error(f"HITL Resume Request ID {request_id}: Error during resume: {error_message}")
                break
            elif workflow_event.get("type") == "breakpoint":
                # Another breakpoint - this shouldn't happen normally but handle it
                breakpoint_message = workflow_event["data"]
                logger.warning(f"HITL Resume Request ID {request_id}: Another breakpoint during resume: {breakpoint_message}")
                response = format_non_streaming_response(
                    request_id=request_id,
                    workflow_result=breakpoint_message,
                    model_name=effective_model_name,
                )
                return response
            elif workflow_event.get("type") == "content_delta":
                full_content_parts.append(str(workflow_event["data"]))
            elif workflow_event.get("type") == "workflow_complete":
                final_consolidated_message = str(workflow_event.get("data", ""))
                break
        
        if error_message:
            raise HTTPException(
                status_code=500, 
                detail={"error": {"type": "internal_server_error", "message": f"Error during HITL resume: {error_message}"}}
            )
        
        final_content = "".join(full_content_parts)
        if not final_content and final_consolidated_message:
            final_content = final_consolidated_message
        elif not final_content:
            final_content = "The approved action has been completed successfully."
        
        logger.info(f"HITL Resume Request ID {request_id}: Resume completed successfully")
        response = format_non_streaming_response(
            request_id=request_id,
            workflow_result=final_content,
            model_name=effective_model_name,
        )
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"HITL Resume Request ID {request_id}: Unexpected error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": {"type": "internal_server_error", "message": f"Unexpected error during HITL resume: {str(e)}"}}
        )


