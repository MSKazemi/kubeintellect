# Re-export shim — real implementation is in kube_q.core.config
from kube_q.core.config import (  # noqa: F401
    CONFIG_DIR,
    LOG_FILE,
    Config,
    load_config,
    setup_logging,
    validate_config,
)
