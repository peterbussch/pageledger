"""PageLedger alpha runtime contracts.

PageLedger is intentionally small: route pages, align outputs, audit evidence,
and rerun failures around existing OCR/VLM extractors.
"""

from .config import PageLedgerConfig, load_config
from .runner import AdapterExecutionError, BudgetExceededError, run

__version__ = "0.1.1"

__all__ = [
    "AdapterExecutionError",
    "BudgetExceededError",
    "PageLedgerConfig",
    "__version__",
    "load_config",
    "run",
]
