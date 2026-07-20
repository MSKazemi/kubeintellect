# app/utils/ssh_tunnel_manager.py
import subprocess
import time
import socket
import logging
import threading
from typing import Optional, List

from app.core.config import settings # Assuming you'll add SSH settings here

logger = logging.getLogger(__name__)

class SSHTunnelManager:
    def __init__(self,
                 local_port: int,
                 remote_host: str,
                 remote_port: int,
                 ssh_server: str,
                 ssh_user: Optional[str] = None,
                 ssh_key_path: Optional[str] = None,
                 ssh_bastion_server: Optional[str] = None, # For multi-hop
                 keep_alive_interval: int = 30):
        self.local_port = local_port
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.ssh_server = ssh_server # This is the server you SSH into (e.g., bastion)
        self.ssh_user = ssh_user or settings.SSH_TUNNEL_USER # Get from settings
        self.ssh_key_path = ssh_key_path or settings.SSH_TUNNEL_KEY_PATH # Get from settings
        self.ssh_bastion_server = ssh_bastion_server # For jump host scenarios

        self.process: Optional[subprocess.Popen] = None
        self.monitoring_thread: Optional[threading.Thread] = None
        self.stop_monitoring_event = threading.Event()
        self.keep_alive_interval = keep_alive_interval
        self.is_active_flag = False

    def _build_ssh_command(self) -> List[str]:
        # Example: ssh -L local_port:remote_k8s_api_host:remote_k8s_api_port -i key user@bastion -N
        # Target for -L is where the K8s API server is from the perspective of the ssh_server (bastion)
        port_forward_spec = f"{self.local_port}:{self.remote_host}:{self.remote_port}"
        
        command = ["ssh", "-L", port_forward_spec]
        if self.ssh_key_path:
            command.extend(["-i", self.ssh_key_path])

        ssh_target = f"{self.ssh_user}@{self.ssh_server}" if self.ssh_user else self.ssh_server
        
        # Handle jump host / bastion if configured
        if self.ssh_bastion_server:
            # Example: ssh -J user@bastion_host user@final_ssh_server -L ...
            # This part can get complex depending on exact SSH needs.
            # For simplicity, this example assumes direct SSH to ssh_server which then forwards.
            # If ssh_server IS the bastion, and remote_host is reachable from it:
            pass # Current command structure is okay for this

        command.extend([ssh_target, "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"])
        return command

    def is_tunnel_port_active(self, host="localhost", timeout=2) -> bool:
        """Checks if the local forwarded port is listening."""
        try:
            with socket.create_connection((host, self.local_port), timeout=timeout):
                logger.debug(f"Successfully connected to tunnel on {host}:{self.local_port}")
                return True
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            logger.debug(f"Tunnel port {host}:{self.local_port} not active: {e}")
            return False

    def start(self) -> bool:
        if self.is_active_flag and self.process and self.process.poll() is None:
            if self.is_tunnel_port_active():
                logger.info("SSH tunnel is already active and responsive.")
                return True
            else:
                logger.warning("SSH tunnel process exists but port is not responsive. Attempting to restart.")
                self.stop() # Stop existing broken process

        ssh_command = self._build_ssh_command()
        logger.info(f"Attempting to start SSH tunnel with command: {' '.join(ssh_command)}")
        try:
            # Using Popen to run in the background
            self.process = subprocess.Popen(ssh_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            logger.info(f"SSH tunnel process initiated with PID: {self.process.pid}. Waiting for connection...")
            
            # Wait a bit for the tunnel to establish
            time.sleep(settings.SSH_TUNNEL_SETUP_WAIT or 5) # Make wait time configurable

            if self.process.poll() is None and self.is_tunnel_port_active():
                self.is_active_flag = True
                logger.info(f"SSH tunnel established successfully to localhost:{self.local_port}.")
                # Start monitoring in a separate thread if desired for long-running app
                # self.start_monitoring_thread() # Optional, see below
                return True
            else:
                self.is_active_flag = False
                stderr_output = self.process.stderr.read().decode() if self.process.stderr else "No stderr"
                logger.error(f"Failed to establish SSH tunnel. Process exited or port not active. SSH stderr: {stderr_output}")
                self.stop() # Ensure process is cleaned up
                return False
        except FileNotFoundError:
            logger.error("SSH command not found. Ensure SSH client is installed and in PATH.")
            self.is_active_flag = False
            return False
        except Exception as e:
            logger.error(f"Error starting SSH tunnel: {e}", exc_info=True)
            self.is_active_flag = False
            if self.process:
                self.stop()
            return False

    def stop(self):
        logger.info("Stopping SSH tunnel...")
        self.is_active_flag = False
        self.stop_monitoring_event.set() # Signal monitoring thread to stop
        if self.monitoring_thread and self.monitoring_thread.is_alive():
            self.monitoring_thread.join(timeout=5)
            if self.monitoring_thread.is_alive(): # Check if join timed out
                 logger.warning("SSH tunnel monitoring thread did not terminate cleanly.")

        if self.process:
            if self.process.poll() is None: # If process is still running
                try:
                    self.process.terminate()
                    self.process.wait(timeout=5) # Wait for graceful termination
                    logger.info(f"SSH tunnel process {self.process.pid} terminated.")
                except subprocess.TimeoutExpired:
                    logger.warning(f"SSH tunnel process {self.process.pid} did not terminate gracefully, killing.")
                    self.process.kill()
                    self.process.wait()
                    logger.info(f"SSH tunnel process {self.process.pid} killed.")
                except Exception as e:
                    logger.error(f"Error during SSH tunnel process termination: {e}", exc_info=True)
            else:
                logger.info("SSH tunnel process was already stopped.")
            self.process = None
        self.stop_monitoring_event.clear() # Reset for next start

    def _monitor_loop(self):
        """Internal loop for monitoring the tunnel."""
        logger.info("SSH tunnel monitoring thread started.")
        while not self.stop_monitoring_event.is_set():
            if not self.is_active_flag or self.process is None or self.process.poll() is not None or not self.is_tunnel_port_active():
                logger.warning("SSH tunnel detected as inactive or process died. Attempting restart.")
                self.is_active_flag = False # Ensure it's marked inactive before restart attempt
                if self.process and self.process.poll() is not None: # If process died, clear it
                    self.process = None
                
                # Potentially add retry logic with backoff here
                if not self.start(): # Attempt to restart
                    logger.error("Failed to restart SSH tunnel after detection. Will retry after interval.")
                else:
                    logger.info("SSH tunnel restarted successfully by monitor.")
            
            # Wait for the interval or until stop event is set
            self.stop_monitoring_event.wait(timeout=self.keep_alive_interval)
        logger.info("SSH tunnel monitoring thread stopped.")

    def start_monitoring_thread(self):
        """Starts a background thread to monitor and restart the tunnel if it dies."""
        if self.monitoring_thread and self.monitoring_thread.is_alive():
            logger.info("Monitoring thread already running.")
            return

        self.stop_monitoring_event.clear()
        self.monitoring_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitoring_thread.start()

    def __enter__(self):
        """Context manager entry: starts the tunnel."""
        if not self.start():
            raise RuntimeError("Failed to establish SSH tunnel for context manager.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit: stops the tunnel."""
        self.stop()

# --- How to use it in your application startup (e.g., app/main.py) ---
# This depends on whether the tunnel is always needed or per-request/operation.

# Global tunnel manager instance (if one tunnel is used app-wide)
# This is a simplified example; managing global state like this needs care.
# app_ssh_tunnel_manager: Optional[SSHTunnelManager] = None

# def startup_ssh_tunnel():
#     global app_ssh_tunnel_manager
#     if settings.SSH_TUNNEL_ENABLED: # Add this to your config
#         app_ssh_tunnel_manager = SSHTunnelManager(
#             local_port=settings.SSH_TUNNEL_LOCAL_PORT,
#             remote_host=settings.SSH_TUNNEL_K8S_API_HOST, # e.g., "192.168.56.11"
#             remote_port=settings.SSH_TUNNEL_K8S_API_PORT, # e.g., 6443
#             ssh_server=settings.SSH_TUNNEL_SERVER_HOST,   # e.g., "141.5.107.135"
#             ssh_user=settings.SSH_TUNNEL_USER,
#             ssh_key_path=settings.SSH_TUNNEL_KEY_PATH
#         )
#         if app_ssh_tunnel_manager.start():
#             logger.info("SSH Tunnel started successfully during application startup.")
#             # If you want it to be self-healing for the app's lifetime:
#             # app_ssh_tunnel_manager.start_monitoring_thread()
#         else:
#             logger.error("Failed to start SSH tunnel during application startup. Kubernetes features via tunnel might be unavailable.")
#             # Decide if this is a fatal error for your app

# def shutdown_ssh_tunnel():
#     if app_ssh_tunnel_manager:
#         logger.info("Shutting down SSH tunnel during application shutdown.")
#         app_ssh_tunnel_manager.stop()

# In app/main.py, you might add these to FastAPI startup/shutdown events:
# app.add_event_handler("startup", startup_ssh_tunnel)
# app.add_event_handler("shutdown", shutdown_ssh_tunnel)

# If Kubernetes client needs to use this tunnel, its KUBECONFIG or client configuration
# would need to point to 'https://localhost:LOCAL_PORT_OF_TUNNEL'
# The Kubernetes client library (kubernetes.config.load_kube_config()) would then
# be configured with a context that points to this local port.
# This SSH tunnel setup is primarily for making a remote K8s API endpoint
# appear as if it's on localhost:LOCAL_PORT.


# TODO check the following codes to see if it work better
# New Version =============================================================

# # app/utils/ssh_tunnel_manager.py
# import subprocess
# import time
# import socket
# import logging
# import threading
# from typing import Optional, List

# # Assuming you will add SSH tunnel related settings to your Pydantic settings model
# # from app.core.config import settings # Example: settings.SSH_TUNNEL_LOCAL_PORT etc.

# logger = logging.getLogger(__name__)

# # --- Placeholder for settings - Replace with actual import from app.core.config ---
# class MockSettings:
#     # These would come from your app.core.config.Settings
#     SSH_TUNNEL_ENABLED: bool = False # Default to False
#     SSH_TUNNEL_LOCAL_PORT: int = 6443
#     SSH_TUNNEL_K8S_API_HOST: str = "192.168.56.11" # Target K8s API host (from bastion's perspective)
#     SSH_TUNNEL_K8S_API_PORT: int = 6443          # Target K8s API port
#     SSH_TUNNEL_SERVER_HOST: str = "141.5.107.135" # Bastion/SSH server
#     SSH_TUNNEL_USER: Optional[str] = "cloud"
#     SSH_TUNNEL_KEY_PATH: Optional[str] = "~/.ssh/DigitalTwin.pem" # Expand ~ in code
#     SSH_TUNNEL_SETUP_WAIT: int = 5 # Seconds to wait for tunnel setup
#     SSH_TUNNEL_KEEP_ALIVE_INTERVAL: int = 30 # Seconds

# settings = MockSettings() # Replace with: from app.core.config import settings
# # --- End Placeholder ---


# class SSHTunnelManager:
#     def __init__(self,
#                  local_port: int = settings.SSH_TUNNEL_LOCAL_PORT,
#                  remote_k8s_api_host: str = settings.SSH_TUNNEL_K8S_API_HOST,
#                  remote_k8s_api_port: int = settings.SSH_TUNNEL_K8S_API_PORT,
#                  ssh_server_host: str = settings.SSH_TUNNEL_SERVER_HOST,
#                  ssh_user: Optional[str] = settings.SSH_TUNNEL_USER,
#                  ssh_key_path: Optional[str] = settings.SSH_TUNNEL_KEY_PATH,
#                  keep_alive_interval: int = settings.SSH_TUNNEL_KEEP_ALIVE_INTERVAL):
        
#         self.local_port = local_port
#         self.remote_k8s_api_host = remote_k8s_api_host
#         self.remote_k8s_api_port = remote_k8s_api_port
#         self.ssh_server_host = ssh_server_host
#         self.ssh_user = ssh_user
        
#         if ssh_key_path:
#             self.ssh_key_path = os.path.expanduser(ssh_key_path) # Expand ~
#         else:
#             self.ssh_key_path = None
            
#         self.keep_alive_interval = keep_alive_interval

#         self.process: Optional[subprocess.Popen] = None
#         self.monitoring_thread: Optional[threading.Thread] = None
#         self.stop_monitoring_event = threading.Event()
#         self._is_active_flag = False # Internal flag for tunnel status

#     def _build_ssh_command(self) -> List[str]:
#         port_forward_spec = f"{self.local_port}:{self.remote_k8s_api_host}:{self.remote_k8s_api_port}"
        
#         command = ["ssh", "-L", port_forward_spec]
#         if self.ssh_key_path:
#             command.extend(["-i", self.ssh_key_path])

#         ssh_target = f"{self.ssh_user}@{self.ssh_server_host}" if self.ssh_user else self.ssh_server_host
        
#         command.extend([
#             ssh_target, 
#             "-N", # Do not execute remote commands
#             "-o", "ExitOnForwardFailure=yes", # Exit if forwarding fails
#             "-o", f"ServerAliveInterval={self.keep_alive_interval // 2}", # Keep connection alive
#             "-o", "ServerAliveCountMax=3",      # Number of keep-alive messages
#             "-o", "ConnectTimeout=10",          # Timeout for establishing SSH connection
#             "-o", "StrictHostKeyChecking=no",   # Consider security implications for production
#             "-o", "UserKnownHostsFile=/dev/null" # Consider security implications
#         ])
#         logger.debug(f"Built SSH command: {' '.join(command)}")
#         return command

#     def is_port_active(self, host="localhost", timeout=2) -> bool:
#         """Checks if the local forwarded port is listening."""
#         try:
#             with socket.create_connection((host, self.local_port), timeout=timeout):
#                 logger.debug(f"Connectivity test: Successfully connected to tunnel on {host}:{self.local_port}")
#                 return True
#         except (ConnectionRefusedError, socket.timeout, OSError) as e:
#             logger.debug(f"Connectivity test: Tunnel port {host}:{self.local_port} not active: {type(e).__name__} - {e}")
#             return False

#     def start(self) -> bool:
#         if self._is_active_flag and self.process and self.process.poll() is None:
#             if self.is_port_active():
#                 logger.info(f"SSH tunnel to localhost:{self.local_port} is already active and responsive.")
#                 return True
#             else:
#                 logger.warning("SSH tunnel process exists but port is not responsive. Attempting to restart.")
#                 self.stop(join_monitor=False) # Stop existing broken process without waiting for monitor if it's stuck

#         ssh_command = self._build_ssh_command()
#         logger.info(f"Attempting to start SSH tunnel: {' '.join(ssh_command)}")
#         try:
#             self.process = subprocess.Popen(ssh_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
#             logger.info(f"SSH tunnel process initiated with PID: {self.process.pid}. Waiting for connection...")
            
#             time.sleep(settings.SSH_TUNNEL_SETUP_WAIT)

#             if self.process.poll() is None and self.is_port_active():
#                 self._is_active_flag = True
#                 logger.info(f"SSH tunnel established successfully: localhost:{self.local_port} -> {self.ssh_server_host} -> {self.remote_k8s_api_host}:{self.remote_k8s_api_port}.")
#                 return True
#             else:
#                 self._is_active_flag = False
#                 stdout_output = self.process.stdout.read().decode(errors='ignore') if self.process.stdout else "No stdout"
#                 stderr_output = self.process.stderr.read().decode(errors='ignore') if self.process.stderr else "No stderr"
#                 logger.error(f"Failed to establish SSH tunnel. Process exited (code: {self.process.poll()}) or port not active. "
#                              f"SSH stdout: '{stdout_output.strip()}', SSH stderr: '{stderr_output.strip()}'")
#                 self.stop(join_monitor=False)
#                 return False
#         except FileNotFoundError:
#             logger.error("SSH command not found. Ensure SSH client is installed and in PATH.")
#             self._is_active_flag = False
#             return False
#         except Exception as e:
#             logger.error(f"Error starting SSH tunnel: {e}", exc_info=True)
#             self._is_active_flag = False
#             if self.process:
#                 self.stop(join_monitor=False)
#             return False

#     def stop(self, join_monitor: bool = True):
#         logger.info("Stopping SSH tunnel...")
#         self._is_active_flag = False # Mark as inactive immediately
#         self.stop_monitoring_event.set() 
        
#         if join_monitor and self.monitoring_thread and self.monitoring_thread.is_alive():
#             logger.debug("Waiting for monitoring thread to stop...")
#             self.monitoring_thread.join(timeout=self.keep_alive_interval + 2) # Give it time to react
#             if self.monitoring_thread.is_alive():
#                  logger.warning("SSH tunnel monitoring thread did not terminate cleanly.")
#         self.monitoring_thread = None # Clear the thread object

#         if self.process:
#             if self.process.poll() is None:
#                 logger.info(f"Terminating SSH tunnel process {self.process.pid}...")
#                 try:
#                     self.process.terminate()
#                     self.process.wait(timeout=5)
#                     logger.info(f"SSH tunnel process {self.process.pid} terminated.")
#                 except subprocess.TimeoutExpired:
#                     logger.warning(f"SSH tunnel process {self.process.pid} did not terminate gracefully, killing.")
#                     self.process.kill()
#                     self.process.wait()
#                     logger.info(f"SSH tunnel process {self.process.pid} killed.")
#                 except Exception as e_term:
#                     logger.error(f"Error during SSH tunnel process termination: {e_term}", exc_info=True)
#             else:
#                 logger.info(f"SSH tunnel process {self.process.pid} was already stopped (exit code: {self.process.poll()}).")
#             self.process = None
#         self.stop_monitoring_event.clear()

#     def _monitor_loop(self):
#         logger.info(f"SSH tunnel monitoring thread started for localhost:{self.local_port}. Check interval: {self.keep_alive_interval}s.")
#         while not self.stop_monitoring_event.is_set():
#             if not self._is_active_flag: # If start() failed or stop() was called
#                 logger.debug("Monitor: Tunnel marked inactive, attempting to restart.")
#                 # No need to check process.poll() here as start() will handle it
#             elif self.process is None or self.process.poll() is not None or not self.is_port_active():
#                 logger.warning(f"Monitor: SSH tunnel to localhost:{self.local_port} detected as inactive or process died. Attempting restart.")
#                 self._is_active_flag = False # Mark as inactive before restart
#                 if self.process and self.process.poll() is not None:
#                     logger.info(f"Monitor: SSH process {self.process.pid} found dead (exit code: {self.process.poll()}).")
#                     self.process = None # Clear dead process
                
#                 if not self.start():
#                     logger.error(f"Monitor: Failed to restart SSH tunnel to localhost:{self.local_port}. Will retry after {self.keep_alive_interval}s.")
#                 else:
#                     logger.info(f"Monitor: SSH tunnel to localhost:{self.local_port} restarted successfully.")
#             else:
#                 logger.debug(f"Monitor: SSH tunnel to localhost:{self.local_port} is active.")
            
#             self.stop_monitoring_event.wait(timeout=self.keep_alive_interval)
#         logger.info(f"SSH tunnel monitoring thread for localhost:{self.local_port} stopped.")

#     def start_monitoring(self):
#         if not self._is_active_flag:
#             logger.warning("Cannot start monitoring, tunnel is not active. Call start() first.")
#             return
#         if self.monitoring_thread and self.monitoring_thread.is_alive():
#             logger.info("Monitoring thread is already running.")
#             return

#         self.stop_monitoring_event.clear()
#         self.monitoring_thread = threading.Thread(target=self._monitor_loop, daemon=True)
#         self.monitoring_thread.start()

#     def __enter__(self):
#         if not self.start():
#             raise RuntimeError("Failed to establish SSH tunnel for context manager.")
#         # Optionally start monitoring if the context is expected to be long-lived
#         # self.start_monitoring() 
#         return self

#     def __exit__(self, exc_type, exc_val, exc_tb):
#         self.stop()

# # --- Global Tunnel Instance (Example for app-wide use) ---
# # To be managed in app/main.py or a dedicated connection manager module
# _global_ssh_tunnel: Optional[SSHTunnelManager] = None

# def get_global_ssh_tunnel() -> Optional[SSHTunnelManager]:
#     return _global_ssh_tunnel

# def initialize_ssh_tunnel_globally():
#     """Initializes and starts the SSH tunnel if configured and not already active."""
#     global _global_ssh_tunnel
#     if settings.SSH_TUNNEL_ENABLED:
#         if _global_ssh_tunnel is None:
#             logger.info("Initializing global SSH tunnel manager.")
#             _global_ssh_tunnel = SSHTunnelManager() # Uses settings from config
        
#         if not _global_ssh_tunnel._is_active_flag: # Check internal flag
#             logger.info("Global SSH tunnel not active, attempting to start...")
#             if _global_ssh_tunnel.start():
#                 _global_ssh_tunnel.start_monitoring() # Keep it alive
#             else:
#                 logger.error("Failed to start global SSH tunnel. K8s operations via tunnel may fail.")
#         else:
#             logger.info("Global SSH tunnel already active.")
#     else:
#         logger.info("Global SSH tunnel is disabled in settings.")

# def shutdown_global_ssh_tunnel():
#     global _global_ssh_tunnel
#     if _global_ssh_tunnel:
#         logger.info("Shutting down global SSH tunnel.")
#         _global_ssh_tunnel.stop()
#         _global_ssh_tunnel = None

