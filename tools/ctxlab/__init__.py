"""ctxlab: shared library for the context-engineering research tooling.

Evaluation-side code only (miners, metrics, method zoo, harnesses). Nothing
here may be imported by the agent pipeline (`driver_agent_pregen/`): modules
in this package read reference diffs and gold labels, which are banned from
the agent input boundary.

Importing this package bootstraps sys.path so that ctxlab modules can reach
`driver_agent_pregen` helpers (repo root) the same way tools/ scripts do.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
