# app/services/llm_service.py
from typing import List, Dict, Any, Optional

from openai import AsyncAzureOpenAI  # Or your preferred OpenAI client

from app.core.config import settings  # Assuming Azure settings are here
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")
# from openai import AsyncAzureOpenAI, OpenAIError
# from openai.types.chat import ChatCompletion # For type hinting the response
# from app.core.config import settings
# from typing import List, Dict, Union


# Initialize client (similar to azure_openai_service.py)
try:
    llm_client = AsyncAzureOpenAI(
        api_key=settings.AZURE_OPENAI_API_KEY,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )
except Exception as e:
    logger.error(f"Failed to initialize LLM client: {e}", exc_info=True)
    llm_client = None

async def get_general_llm_response(messages: List[Dict[str, str]], model_deployment: Optional[str] = None) -> Any:# -> ChatCompletion:
    """
    Gets a response from the configured LLM for general queries.
    """
    if not llm_client:
        logger.error("LLM client is not initialized.")
        # Consider raising an exception or returning a specific error object
        # raise OpenAIError("Azure OpenAI client not initialized.") # Or a custom exception
        return {"error": "LLM client not available"}

    deployment = model_deployment if model_deployment else settings.AZURE_OPENAI_DEPLOYMENT_NAME

    try:
        logger.info(f"Sending request to LLM. Deployment: {deployment}")
        completion = await llm_client.chat.completions.create(
            model=deployment,
            messages=messages,
            temperature=settings.AZURE_OPENAI_TEMPERATURE, # Use configured settings
            max_tokens=settings.AZURE_OPENAI_MAX_TOKENS,   # Use configured settings
            #             # n=1, # Number of completions to generate, fixed to 1 for PoC
#             # stream=False # Non-streaming for PoC
        )
        logger.info(f"Received response from LLM: ID {completion.id if completion else 'N/A'}")
        return completion # Return the full completion object (OpenAI SDK v1.x type)
    #     except OpenAIError as e:
#         logger.error(f"Error calling Azure OpenAI: {e.message if hasattr(e, 'message') else str(e)} (Type: {e.type if hasattr(e, 'type') else 'Unknown'}, Code: {e.code if hasattr(e, 'code') else 'Unknown'})", exc_info=True)
#         raise # Re-raise the error to be handled by the endpoint
    except Exception as e:
        logger.error(f"Error calling LLM service: {e}", exc_info=True)
        # Consider raising an exception or returning a specific error object
        return {"error": str(e)}
    # raise OpenAIError(f"An unexpected error occurred while contacting Azure OpenAI: {str(e)}")
