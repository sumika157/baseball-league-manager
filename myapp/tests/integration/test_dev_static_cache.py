"""開発サーバーの静的ファイルに ``Cache-Control: no-cache`` が付くことの検査。

古い CSS がブラウザに残る問題（#162）の再発防止。``runserver`` の静的配信は
ミドルウェアを通らないので、ハンドラ（``management/commands/runserver.py``）を直接確かめる。
"""

from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.handlers.wsgi import WSGIHandler
from django.test import RequestFactory, SimpleTestCase, override_settings

from myapp.management.commands.runserver import Command, NoCacheStaticFilesHandler

CSS_PATH = "/static/myapp/css/theme.css"


class NoCacheStaticHandlerTest(SimpleTestCase):
    def test_静的ファイルの応答にno_cacheが付く(self) -> None:
        handler = NoCacheStaticFilesHandler(WSGIHandler())
        response = handler.get_response(RequestFactory().get(CSS_PATH))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-cache")
        response.close()

    def test_If_Modified_Sinceには304を返し_no_cacheも付く(self) -> None:
        handler = NoCacheStaticFilesHandler(WSGIHandler())
        request = RequestFactory().get(CSS_PATH, headers={"If-Modified-Since": "Wed, 01 Jan 2099 00:00:00 GMT"})
        response = handler.get_response(request)
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.headers["Cache-Control"], "no-cache")

    def test_静的ファイル以外はこのハンドラを通らない(self) -> None:
        # _should_handle が STATIC_URL 配下だけを拾い、それ以外は元のアプリへ渡す
        handler = NoCacheStaticFilesHandler(WSGIHandler())
        self.assertTrue(handler._should_handle(CSS_PATH))
        self.assertFalse(handler._should_handle("/"))


class RunserverHandlerSelectionTest(SimpleTestCase):
    @override_settings(DEBUG=True, PRODUCTION_STATIC=False)
    def test_開発ではno_cacheのハンドラになる(self) -> None:
        handler = Command().get_handler(use_static_handler=True, insecure_serving=False)
        self.assertIsInstance(handler, NoCacheStaticFilesHandler)

    @override_settings(DEBUG=True, PRODUCTION_STATIC=True)
    def test_本番の静的配信のときは差し替えない(self) -> None:
        handler = Command().get_handler(use_static_handler=True, insecure_serving=False)
        self.assertNotIsInstance(handler, NoCacheStaticFilesHandler)
        self.assertIsInstance(handler, StaticFilesHandler)

    @override_settings(DEBUG=False, PRODUCTION_STATIC=False)
    def test_DEBUGが偽なら差し替えない(self) -> None:
        handler = Command().get_handler(use_static_handler=True, insecure_serving=False)
        self.assertNotIsInstance(handler, NoCacheStaticFilesHandler)
