# app/api/v1/debug_middleware.py
import json
from typing import Callable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="debug_middleware")

class DebugRequestMiddleware(BaseHTTPMiddleware):
    """Middleware to debug incoming requests by logging detailed information"""
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Only debug chat completions endpoint
        if request.url.path.endswith("/chat/completions"):
            logger.debug("=" * 80)
            logger.debug(f"DEBUG: Incoming request to {request.method} {request.url.path}")

            # Note: headers are already logged as a structured dict by main.py (http_request_headers).
            # No per-header dump here to avoid duplicate log lines.

            # Log query parameters
            if request.query_params:
                logger.debug("QUERY PARAMETERS:")
                for name, value in request.query_params.items():
                    logger.debug(f"  {name}: {value}")

            # Read and log body
            body_bytes = await request.body()
            if body_bytes:
                try:
                    body_json = json.loads(body_bytes)
                    logger.debug("REQUEST BODY (JSON):")
                    logger.debug(json.dumps(body_json, indent=2))

                    # Specifically log fields that might contain thread/user info
                    # Check both snake_case and camelCase variants (LibreChat sends camelCase)
                    _conv_id = body_json.get('conversation_id') or body_json.get('conversationId', 'NOT PROVIDED')
                    _user_id = body_json.get('user_id') or body_json.get('userId') or body_json.get('user', 'NOT PROVIDED')
                    logger.debug("EXTRACTED FIELDS:")
                    logger.debug(f"  model: {body_json.get('model', 'NOT PROVIDED')}")
                    logger.debug(f"  stream: {body_json.get('stream', 'NOT PROVIDED')}")
                    logger.debug(f"  thread_id: {body_json.get('thread_id', 'NOT PROVIDED')}")
                    logger.debug(f"  conversation_id: {_conv_id}")
                    logger.debug(f"  chat_id: {body_json.get('chat_id', 'NOT PROVIDED')}")
                    logger.debug(f"  user_id: {_user_id}")


                    # Log message count and last message
                    messages = body_json.get('messages', [])
                    logger.debug(f"  message_count: {len(messages)}")
                    if messages:
                        last_msg = messages[-1]
                        logger.debug(f"  last_message_role: {last_msg.get('role', 'NOT PROVIDED')}")
                        logger.debug(f"  last_message_preview: {last_msg.get('content', '')[:100]}...")

                    # Check for any fields that might contain IDs
                    logger.debug("SEARCHING FOR ID-LIKE FIELDS:")
                    for key, value in body_json.items():
                        if any(id_hint in key.lower() for id_hint in ['id', 'uuid', 'session', 'thread', 'conversation', 'chat', 'user']):
                            logger.debug(f"  {key}: {value}")

                except json.JSONDecodeError:
                    logger.debug(f"REQUEST BODY (RAW): {body_bytes.decode()[:500]}...")
                except Exception as e:
                    logger.error(f"Error parsing request body: {e}")
            else:
                logger.debug("REQUEST BODY: [EMPTY]")

            logger.debug("=" * 80)
        
        # Call the next middleware/endpoint
        response = await call_next(request)
        
        return response 