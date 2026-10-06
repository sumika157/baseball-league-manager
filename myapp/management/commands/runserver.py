"""開発サーバー（runserver）。静的ファイルの応答に ``Cache-Control: no-cache`` を付ける。

ブラウザは Cache-Control の無い CSS・JS を、更新日時から見積もった期間だけ再確認せずに使い回す
（ヒューリスティックキャッシュ）。CSS を直しても再読み込みだけでは新しい CSS が当たらず、
古い見た目が残る。``no-cache`` は「使う前に毎回サーバーに確かめる」指示で、
runserver の静的配信は ``If-Modified-Since`` に 304 を返すので、確かめても重くならない。

ミドルウェアでは付けられない。``StaticFilesHandler`` は静的ファイルのリクエストを
ミドルウェアの連鎖に通さず直接配信するため、ここでハンドラの ``serve`` を包む。
``myapp`` は ``INSTALLED_APPS`` で ``django.contrib.staticfiles`` より前にあるので、
こちらの ``runserver`` が優先される。
"""

from typing import Any

from django.conf import settings
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.contrib.staticfiles.management.commands.runserver import Command as StaticRunserver
from django.core.handlers.wsgi import WSGIHandler
from django.http import FileResponse, HttpRequest, HttpResponse


class NoCacheStaticFilesHandler(StaticFilesHandler):
    """静的ファイルの応答に ``Cache-Control: no-cache`` を付ける開発用ハンドラ。"""

    def serve(self, request: HttpRequest) -> HttpResponse | FileResponse:
        response = super().serve(request)
        response.headers["Cache-Control"] = "no-cache"
        return response


def use_no_cache_static() -> bool:
    """開発（DEBUG が真で、本番の静的配信ではない）のときだけ真。"""
    return bool(settings.DEBUG) and not settings.PRODUCTION_STATIC


class Command(StaticRunserver):
    def get_handler(self, *args: Any, **options: Any) -> Any:
        handler = super().get_handler(*args, **options)
        if isinstance(handler, StaticFilesHandler) and use_no_cache_static():
            application: WSGIHandler = handler.application
            return NoCacheStaticFilesHandler(application)
        return handler
