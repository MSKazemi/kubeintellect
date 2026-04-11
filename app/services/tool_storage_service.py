# app/services/tool_storage_service.py
"""
Service for storing and loading tool files on PVC.

This service handles file operations for runtime-generated tools,
storing them in individual Python files on the persistent volume.
"""

import hashlib
from pathlib import Path
from typing import Optional, List

from app.core.config import settings
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


class ToolStorageService:
    """Service for storing/loading tool files on PVC."""
    
    def __init__(self, base_path: Optional[str] = None):
        """
        Initialize the tool storage service.
        
        Args:
            base_path: Base path for tool storage. Defaults to config value.
        """
        if base_path is None:
            base_path = getattr(settings, 'RUNTIME_TOOLS_PVC_PATH', '/mnt/runtime-tools')
        
        self.base_path = Path(base_path)
        self.tools_dir = self.base_path / "tools"
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        
        logger.debug(f"Tool storage initialized at {self.base_path}")
    
    def save_tool_file(self, tool_id: str, code: str) -> str:
        """
        Save tool code to file.
        
        Args:
            tool_id: Unique tool identifier
            code: Python code for the tool
            
        Returns:
            Relative file path from base_path
        """
        filename = f"gen_{tool_id}.py"
        file_path = self.tools_dir / filename
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(code)
            
            relative_path = str(file_path.relative_to(self.base_path))
            logger.info(f"Saved tool file: {relative_path} (tool_id: {tool_id})")
            return relative_path
        except Exception as e:
            logger.error(f"Failed to save tool file {filename}: {e}", exc_info=True)
            raise
    
    def load_tool_file(self, tool_id: str) -> Optional[str]:
        """
        Load tool code from file.
        
        Args:
            tool_id: Unique tool identifier
            
        Returns:
            Tool code as string, or None if file not found
        """
        filename = f"gen_{tool_id}.py"
        file_path = self.tools_dir / filename
        
        if not file_path.exists():
            logger.warning(f"Tool file not found: {filename}")
            return None
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                code = f.read()
            logger.debug(f"Loaded tool file: {filename}")
            return code
        except Exception as e:
            logger.error(f"Failed to load tool file {filename}: {e}", exc_info=True)
            return None
    
    def delete_tool_file(self, tool_id: str):
        """
        Delete tool file.
        
        Args:
            tool_id: Unique tool identifier
        """
        filename = f"gen_{tool_id}.py"
        file_path = self.tools_dir / filename
        
        if file_path.exists():
            try:
                file_path.unlink()
                logger.info(f"Deleted tool file: {filename}")
            except Exception as e:
                logger.error(f"Failed to delete tool file {filename}: {e}", exc_info=True)
                raise
        else:
            logger.warning(f"Tool file not found for deletion: {filename}")
    
    def list_tool_files(self) -> List[str]:
        """
        List all tool file IDs.
        
        Returns:
            List of tool IDs (extracted from filenames)
        """
        tool_ids = []
        try:
            for file_path in self.tools_dir.glob("gen_*.py"):
                # Extract tool_id from filename: gen_<tool_id>.py
                tool_id = file_path.stem.replace("gen_", "")
                tool_ids.append(tool_id)
        except Exception as e:
            logger.error(f"Failed to list tool files: {e}", exc_info=True)
        
        return tool_ids
    
    def compute_file_checksum(self, tool_id: str) -> Optional[str]:
        """
        Compute the SHA-256 checksum of a saved tool file.

        Args:
            tool_id: Unique tool identifier

        Returns:
            Hex-encoded SHA-256 digest, or None if the file does not exist.
        """
        filename = f"gen_{tool_id}.py"
        file_path = self.tools_dir / filename

        if not file_path.exists():
            logger.warning(f"Cannot checksum missing tool file: {filename}")
            return None

        try:
            digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
            logger.debug(f"Checksum for {filename}: {digest[:12]}…")
            return digest
        except Exception as e:
            logger.error(f"Failed to compute checksum for {filename}: {e}", exc_info=True)
            return None

    def get_tools_directory(self) -> Path:
        """Get the tools directory path."""
        return self.tools_dir

