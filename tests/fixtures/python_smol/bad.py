"""Tiny fixture used only by tests/test_semgrep.py.

Two obvious patterns that semgrep's `p/python` rule pack
catches deterministically:
  - exec(input(...))  -> dangerous-exec / exec-detected
  - hardcoded API key -> generic secret rule
"""

import os


def run_user_command():
    cmd = input("enter command: ")
    exec(cmd)  # noqa: S102


# Hardcoded secret — semgrep p/python flags this.
API_KEY = "sk-1234567890abcdef1234567890abcdef"


def get_env_secret():
    return os.environ.get("REAL_SECRET", API_KEY)
