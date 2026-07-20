# app/api/v1/endpoints/kubernetes.py
from fastapi import APIRouter, HTTPException, status
from app.services.kubernetes_service import list_namespaces, KubernetesConfigurationError, KubernetesAPIError  # Added KubernetesAPIError
from app.utils.logger_config import setup_logging
logger = setup_logging(app_name="kubeintellect")

router = APIRouter()


@router.get(
    "/namespaces",
    tags=["Kubernetes"],
    summary="Check Kubernetes API Server Connectivity and List Namespaces",
    description="Attempts to connect to the Kubernetes API server and retrieves a list of namespaces.  This endpoint is useful for verifying API server access and basic cluster health.",
    responses={
        200: {"description": "Successfully connected to the API server and retrieved namespaces."},
        500: {"description": "Internal server error, indicating a problem connecting to the K8s API server or configuration issues."},
        503: {"description": "Service Unavailable, likely due to the K8s API server being unreachable."},
    },
)
async def get_namespaces():
    """
    Checks connectivity to the Kubernetes API server and retrieves a list of all namespaces.
    """
    try:
        logger.info("Checking Kubernetes API server connectivity and listing namespaces...")
        namespaces = list_namespaces()  # Attempt to list namespaces
        if namespaces is None:
            logger.error("list_namespaces returned None, indicating a potential connection or access issue.")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve namespaces from Kubernetes.  Check API server connectivity.",
            )
        return {
            "status": "success",
            "api_server_status": "connected",  # Explicitly indicate successful connection
            "namespaces_count": len(namespaces),
            "namespaces": namespaces,
        }
    except KubernetesConfigurationError as e:
        logger.error(f"Kubernetes configuration error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Kubernetes configuration error: {e}",
        )
    except KubernetesAPIError as e:  # Catch specific API errors
        logger.error(f"Error connecting to Kubernetes API server: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,  # Use 503 for unavailability
            detail=f"Failed to connect to Kubernetes API server: {e}",
        )
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {e}",
        )
    




