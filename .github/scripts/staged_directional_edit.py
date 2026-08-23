# message: docs: synchronize promoted directional efficiency evidence
from __future__ import annotations

import subprocess

# The previous commit contains the fully asserted final-sync script. Execute only its
# code/comment/Markdown sections here; workflow cleanup is performed separately through
# the connected GitHub writer because Actions' GITHUB_TOKEN cannot update workflows.
source = subprocess.check_output(
    ["git", "show", "HEAD~1:.github/scripts/staged_directional_edit.py"],
    text=True,
)
marker = "# 8) Restore normal full CI."
if marker not in source:
    raise SystemExit("previous staged sync is missing workflow-cleanup marker")
source = source.split(marker, 1)[0]
exec(compile(source, "staged_directional_edit_payload.py", "exec"))
