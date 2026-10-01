"""`.dockerignore` の検査。

`frontend/node_modules/` が除外されておらず、Windows 側（U:）からの
`docker compose build` がビルドコンテキストの転送中に
`invalid file request frontend/node_modules/.bin/...` で止まったことがあるため、機械的に防ぐ。
"""

import pathlib

from django.conf import settings
from django.test import SimpleTestCase

ROOT = pathlib.Path(settings.BASE_DIR)

# gitignore 済みの生成物のうち、ビルドコンテキストに入れてはいけないもの
FRONTEND_ARTIFACT_PREFIXES = ("frontend/", "myapp/static/myapp/dist")


def _patterns(path: pathlib.Path) -> set[str]:
    """コメントと空行を除いたパターンの集合。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")}


class DockerignoreTest(SimpleTestCase):
    def test_frontend_artifacts_are_excluded_from_build_context(self):
        """gitignore したフロントエンドの生成物は、ビルドコンテキストからも外れていること。

        ソースはバインドマウントでコンテナに渡るので、イメージに含める必要は無い。
        node_modules を含めると、シンボリックリンクのせいで Windows 側からビルドできなくなる。
        """
        ignored = {p for p in _patterns(ROOT / ".gitignore") if p.startswith(FRONTEND_ARTIFACT_PREFIXES)}
        self.assertTrue(ignored, "gitignore からフロントエンドの生成物が見つかりません。")

        missing = ignored - _patterns(ROOT / ".dockerignore")
        self.assertFalse(missing, f".dockerignore に無い生成物: {sorted(missing)}")
