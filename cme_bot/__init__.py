"""CME Simulator auto-trader — playwright-driven client for the free CME
Trading Simulator.  See README.md for setup and caveats."""
from .client import CMESimulatorClient, DEFAULT_CDP_URL

__all__ = ["CMESimulatorClient", "DEFAULT_CDP_URL"]
__version__ = "0.2.0"
