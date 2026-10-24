"""URL のパスコンバータ。

`wskey`: ワークスペースの key（`/w/<key>/...`）。形（長さと文字）の出典はドメインの `KEY_LENGTH`・`KEY_ALPHABET` で、
ここで複製しない。形の合わない key は URL の解決の段階で外れるので、DB を読まずに 404 になる。
"""

import re

from ..domain.workspace import KEY_ALPHABET, KEY_LENGTH


class WorkspaceKeyConverter:
    """`<wskey:key>`。ビューへは文字列のまま渡す（解決は `workspace_view` が行う）。"""

    regex = f"[{re.escape(KEY_ALPHABET)}]{{{KEY_LENGTH}}}"

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: str) -> str:
        return value
