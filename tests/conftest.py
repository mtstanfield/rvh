"""Shared pytest fixtures for rvh tests."""
import sys
from pathlib import Path

# Add scripts/ to path so `import cpg_queries` etc. works in tests
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
