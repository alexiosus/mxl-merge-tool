"""HTML embedding helpers shared by the merge UI and the setup page."""

from __future__ import annotations

import json


def safe_json_for_script(value: object) -> str:
    """JSON that cannot terminate the surrounding script element."""

    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
