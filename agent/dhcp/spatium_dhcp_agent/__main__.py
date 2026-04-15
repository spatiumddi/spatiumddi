"""CLI entrypoint — ``spatium-dhcp-agent`` / ``python -m spatium_dhcp_agent``."""

from __future__ import annotations

import os
import sys

from .cache import ensure_layout
from .config import AgentConfig
from .log import configure_logging
from .supervisor import run


def main() -> int:
    configure_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    cfg = AgentConfig.from_env()
    ensure_layout(cfg.state_dir)
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
