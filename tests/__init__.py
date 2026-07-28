"""Test package setup.

Preview commands fall back to the repository's Git configuration when nothing
else is set. On a machine where the tool is installed that fallback is the real
1C renderer, so an unsuspecting test run launches 1C against the tiny synthetic
documents the tests build — which fails with a stream format error and leaves
1C windows behind. Pin both to empty so tests only ever use the converters they
pass in explicitly.
"""

import os

os.environ.setdefault("MXL_PREVIEW_COMMAND", "")
os.environ.setdefault("MXL_PREVIEW_BATCH_COMMAND", "")
