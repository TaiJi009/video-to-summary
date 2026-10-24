"""与 frontend/src/utils/filename.ts 对齐：非法字符 → 下划线。"""

import re

_ILLEGAL = re.compile(r'[/\\:*?"<>|]')


def sanitize_filename(title: str) -> str:
    s = _ILLEGAL.sub("_", title).strip()
    return s or "untitled"
