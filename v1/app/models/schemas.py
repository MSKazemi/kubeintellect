# app/models/schemas.py


################################
#    Supervisor Agent State    #
################################


# Keep KubernetesActionParams as a general structure for agent nodes
# if they are designed to accept this broader set of possible params.
# Alternatively, agent nodes could expect the specific *Params model.
# For now, let's assume intent_detection_node will try to fit extracted
# params into KubernetesActionParams for downstream compatibility.
################################
#   Kubernetes Action Params   #
################################


# # Define KubernetesActionParams FIRST
# class KubernetesActionParams(BaseModel):
#     """
#     Common parameters for various Kubernetes actions initiated by LangGraph agents.
#     """
#     name: Optional[str] = Field(None, description="The name of the Kubernetes resource (e.g., pod name, deployment name).")
#     namespace: Optional[str] = Field("default", description="The Kubernetes namespace. Defaults to 'default'.")
    
#     # For scaling deployments
#     replicas: Optional[int] = Field(None, description="The desired number of replicas for scaling operations.")
    
#     # For fetching pod logs
#     container_name: Optional[str] = Field(None, description="The specific container name within the pod to fetch logs from.")
#     tail_lines: Optional[int] = Field(50, description="Number of lines from the end of the logs to show. Defaults to 50.")
#     previous_container_logs: Optional[bool] = Field(False, description="If True, fetch logs from the previous instance of the container(s). Defaults to False.")
    
#     # You can add other common parameters here as your agents evolve
#     # E.g., label_selector: Optional[str] = None
#     # E.g., field_selector: Optional[str] = None

# # Now define GraphState (or PocGraphState, ensure consistency with your workflow files)
# class GraphState(BaseModel): # Ensure this name matches what's used in your main_workflow.py
#     """
#     Represents the state of the KubeIntellect LangGraph workflow.
#     """
#     initial_llm_response_content: str = Field(
#         description="The raw user query or initial content passed to the workflow."
#     )
    
#     detected_intent: Optional[str] = Field(
#         default=None,
#         description="The primary Kubernetes action intent classified by the NLU step (e.g., 'list_pods', 'get_deployment')."
#     )
    
#     # This will store the parameters extracted by the LLM, structured according to
#     # one of the *Params models (e.g., ScaleDeploymentParams), but then mapped
#     # to the fields of KubernetesActionParams for the agent nodes.

#     intent_params: KubernetesActionParams = Field( # Use the defined class
#         default_factory=KubernetesActionParams, # Now KubernetesActionParams is defined
#         description="Structured parameters extracted for the detected intent, fitting the common K8s action structure."
#     )
    
#     k8s_action_result: Optional[Any] = Field(
#         default=None,
#         description="The direct result from a Kubernetes service call made by an agent (e.g., list of pod names, deployment dict, log string, boolean success status)."
#     )
    
#     k8s_action_status: Optional[str] = Field(
#         default=None,
#         description="Status of the last Kubernetes action performed ('success' or 'error')."
#     )
    
#     final_formatted_content: Optional[str] = Field(
#         default=None,
#         description="The final, human-readable content string prepared by the workflow for the API response."
#     )
    
#     error_message: Optional[str] = Field(
#         default=None,
#         description="A general error message if a significant issue occurs during workflow processing that isn't part of k8s_action_result."
#     )

#     class Config:
#         arbitrary_types_allowed = True # Useful if k8s_action_result can hold diverse complex types


# ===========================================
# --- Request Models ---



# --- Response Models ---

    