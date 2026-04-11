import os

from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")
# Assuming MermaidDrawMethod is directly available in LangGraph:
# from langgraph.graph import MermaidDrawMethod
# try:
#     from langgraph.graph import MermaidDrawMethod
# except ImportError:
#     print("Warning: Could not import MermaidDrawMethod. Install pyppeteer if you intend to use it.")



def save_langgraph_workflow(app_graph, base_filename="compiled_workflow"):
    """
    Saves a LangGraph workflow to Mermaid, PNG, and PDF files.

    Args:
        app_graph: The compiled LangGraph workflow object.
        base_filename (str, optional): The base name for the output files
            (e.g., "my_workflow"). Defaults to "compiled_workflow".
    """
    os.makedirs("images", exist_ok=True)
    mermaid_filename = f"documents/images{base_filename}.mermaid"
    png_filename = f"documents/images{base_filename}.png"
    # pdf_filename = f"documents/images{base_filename}.pdf"

    try:
        # app_graph.get_graph().draw_mermaid_png(output_file_path=png_filename, max_retries=5, retry_delay=0.5)
        app_graph.get_graph().draw_mermaid_png(output_file_path=png_filename)
        # app_graph.get_graph().draw_mermaid_png(output_file_path=png_filename,draw_method=MermaidDrawMethod.PYPPETEER)

        logger.info(f"PNG file saved to {png_filename}")
    except AttributeError as e:
        logger.error("Error: One or more of the required methods ('get_graph', 'draw_mermaid_png', 'draw_mermaid_pdf') are not found on the 'app_graph' object or its graph. Please check the LangGraph library's documentation.")
        logger.error(f"Details: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while trying to save the workflow: {e}")
    
    # try:
    #     app_graph.get_graph().draw_mermaid_pdf(output_file_path=pdf_filename, max_retries=5, retry_delay=0.5)
    #     logger.info(f"PDF file saved to {pdf_filename}")
    # except AttributeError as e:
    #     logger.error(f"Error: One or more of the required methods ('get_graph', 'draw_mermaid', 'draw_mermaid_png', 'draw_mermaid_pdf') are not found on the 'app_graph' object or its graph. Please check the LangGraph library's documentation.")
    #     logger.error(f"Details: {e}")
    # except Exception as e:
    #     logger.error(f"An unexpected error occurred while trying to save the workflow: {e}")

    try:
        # Save as Mermaid
        with open(mermaid_filename, "w") as f:
            f.write(app_graph.get_graph().draw_mermaid())
        logger.info(f"Mermaid code saved to {mermaid_filename}")
    except AttributeError as e:
        logger.error("Error: One or more of the required methods ('get_graph', 'draw_mermaid', 'draw_mermaid_png', 'draw_mermaid_pdf') are not found on the 'app_graph' object or its graph. Please check the LangGraph library's documentation.")
        logger.error(f"Details: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while trying to save the workflow: {e}")

# Example usage (assuming you have your 'app_graph' object):
# save_langgraph_workflow(app_graph)
# from app.utils.utils import save_langgraph_workflow
# save_langgraph_workflow(app_graph, base_filename="kubeintellect_supervisor_workflow")