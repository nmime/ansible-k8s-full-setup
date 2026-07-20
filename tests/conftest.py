"""Shared local test bootstrap.

The repository keeps Ansible in an ignored project virtual environment. Tests
that exercise the playbooks spawn ``ansible-playbook`` by name, so make direct
``.venv/bin/pytest`` runs behave like an activated virtual environment while
leaving CI and system installations untouched.
"""

from __future__ import annotations

import os
from pathlib import Path


VENV_BIN = Path(__file__).resolve().parents[1] / ".venv" / "bin"

if (VENV_BIN / "ansible-playbook").is_file():
    os.environ["PATH"] = f"{VENV_BIN}{os.pathsep}{os.environ.get('PATH', '')}"
