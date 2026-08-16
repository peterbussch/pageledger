"""PageLedger alpha runtime contracts.

PageLedger is intentionally small: route pages, audit evidence, and rerun
flagged pages around existing OCR/VLM extractors.
"""

from ._version import __version__
from .config import PageLedgerConfig, load_config
from .runner import AdapterExecutionError, BudgetExceededError, run

__all__ = [
    "AdapterExecutionError",
    "BudgetExceededError",
    "PageLedgerConfig",
    "__version__",
    "load_config",
    "run",
]
