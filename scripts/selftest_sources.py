#!/usr/bin/env python3
"""配置格式、资源搬迁、JAR 回退及停用策略的离线回归：python scripts/selftest_sources.py。"""
import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import update


class Raw(io.BytesIO):
    def read(self, size=-1, decode_content=False):
        return super().read(size)

    def stream(self, size, decode_content=False):
        # 模拟压缩传输被拆成多个小块，不能假设一次读取就能拿齐签名。
        yield self.read(1)
        yield self.read(2)
        while chunk := self.read(size):
            yield chunk


class Session:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        status, body, final_url = self.routes[url]
        response = requests.Response()
        response.status_code = status
        response.url = final_url or url
        response._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        response.raw = Raw(response.content)
        response._content_consumed = not kwargs.get('stream', False)
        return response

    def close(self):
        pass


def config(key="a", **fields):
    return {"sites": [{"key": key, "name": key, "type": 3, "api": "csp_Test"}], **fields}


class SourceTests(unittest.TestCase):
    def test_plain_json_does_not_decode_markers_in_strings(self):
        original = config(ads=["https://example.test/abcd1234**keep"])
        self.assertEqual(update.decode_config(b'\xef\xbb\xbf' + json.dumps(original).encode()), original)

    def test_image_base64_and_embedded_key_cbc(self):
        image = b'\xff\xd8\xfffake-image\xff\xd9ABCDEFGH**' + base64.b64encode(json.dumps(config()).encode())
        self.assertEqual(update.decode_config(image), config())
        # 固定协议样例，密钥/IV 内嵌于响应；测试本身不执行加密器。
        cbc = (b'24233132333435363738393031323323249ac62fc5b68774624fd55b2a7c7c467e'
               b'866a67cd47ce9be074e7bf13b41bfba7a56017778e36abfb26cd8979f46c1e039'
               b'3e57007632a2fcf8172ed11aebac0d0a196b1e603d15946d0025109f27bbb1a'
               b'31373934393731353834313337')
        expected = config("cbc")
        expected['sites'][0]['name'] = 'CBC'
        self.assertEqual(update.decode_config(cbc), expected)
        self.assertEqual(update.decode_config(b'ABCDEFGH**' + base64.b64encode(cbc)), expected)

    def test_html_and_malformed_payloads_are_rejected(self):
        for body in (b'<html>ABCDEFGH**e30=</html>', b'2423bad', b'ABCDEFGH**bad!', b'\xff\xd8\xffno-config'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                update.decode_config(body)

    def test_resources_follow_final_url_and_preserve_query(self):
        obj = config(spider="../jar/custom.jar;md5;123", wallpaper="/wall.jpg")
        obj['sites'][0].update(api="./lib/drpy.js", ext={"url": "../rules/a.js?path=../keep"})
        session = Session({'https://source.test/go': (200, obj, 'https://cdn.test/project/json/config.json')})
        actual, err = update.fetch_single(session, 'https://source.test/go')
        self.assertIsNone(err)
        self.assertEqual(actual['spider'], 'https://cdn.test/project/jar/custom.jar;md5;123')
        self.assertEqual(actual['wallpaper'], 'https://cdn.test/wall.jpg')
        self.assertEqual(actual['sites'][0]['api'], 'https://cdn.test/project/json/lib/drpy.js')
        self.assertEqual(actual['sites'][0]['ext']['url'], 'https://cdn.test/project/rules/a.js?path=../keep')
        self.assertEqual(obj['spider'], '../jar/custom.jar;md5;123')

    def test_broken_spider_falls_back_and_uses_source_headers(self):
        session = Session({
            'https://source.test/bad': (200, config(spider='./bad.jar'), None),
            'https://source.test/bad.jar': (403, b'gone', None),
            'https://source.test/html': (200, config(spider='./html.jar'), None),
            'https://source.test/html.jar': (200, b'<html>not a jar</html>', None),
            'https://source.test/good': (200, config(spider='./good.png'), None),
            'https://source.test/good.png': (200, b'PK\x03\x04archive', None),
        })
        used, obj, err = update.fetch_source(session, {
            'urls': ['https://source.test/bad', 'https://source.test/html', 'https://source.test/good'],
            'headers': {'User-Agent': 'TestPlayer'},
        })
        self.assertEqual(used, 'https://source.test/good')
        self.assertIsNone(err)
        self.assertEqual(obj['spider'], 'https://source.test/good.png')
        self.assertTrue(all(kwargs['headers']['User-Agent'] == 'TestPlayer' for _, kwargs in session.calls))

    def test_aggregate_preserves_one_jar_and_its_context(self):
        primary = config(spider='https://a.test/a.jar', rules=[{'name': 'A'}], ads=['ad.test'])
        secondary = config('b', spider='https://b.test/b.jar', rules=[{'name': 'B'}])
        actual = update.merge_configs([primary, secondary])
        self.assertEqual(actual, primary)
        self.assertEqual([s['key'] for s in actual['sites']], ['a'])
        actual['rules'][0]['name'] = 'changed'
        self.assertEqual(primary['rules'][0]['name'], 'A')

    def test_disabled_empty_subscription_and_online_before_cache(self):
        with tempfile.TemporaryDirectory(prefix='tvbox_sources_selftest_') as temp:
            root = Path(temp)
            out = root/'output'
            out.mkdir()
            previous = config('cached')
            for sid in ('cached', 'empty'):
                update.write_json(out/f'{sid}.json', previous)
            cfg = {
                'repo': 'test/repo', 'aggregate_prefer': ['cached', 'empty', 'good'],
                'sources': [
                    {'id': 'good', 'name': 'Good', 'urls': ['https://source.test/good']},
                    {'id': 'cached', 'name': 'Cached', 'urls': ['https://source.test/gone']},
                    {'id': 'empty', 'name': 'Empty', 'urls': ['https://source.test/sub']},
                    {'id': 'disabled', 'name': 'Disabled', 'enabled': False, 'urls': ['https://never.test/']},
                ],
            }
            update.write_json(root/'sources.json', cfg)
            session = Session({
                'https://source.test/good': (200, config('good'), None),
                'https://source.test/gone': (404, b'gone', None),
                'https://source.test/sub': (200, {'urls': [{'name': 'Dead', 'url': './missing'}]}, None),
                'https://source.test/missing': (404, b'gone', None),
            })
            with patch.multiple(update, CONFIG_FILE=root/'sources.json', OUTPUT_DIR=out,
                                HISTORY_FILE=out/'history.json', README_FILE=root/'README.md'), \
                    patch.dict(update.os.environ, {'GITHUB_REPOSITORY': 'test/repo', 'CNB': 'false'}), \
                    patch.object(update.requests, 'Session', return_value=session):
                self.assertEqual(update.main(), 0)
            state = json.loads((out/'status.json').read_text())
            self.assertEqual(state['summary']['total'], 3)
            self.assertEqual(state['summary']['ok'], 1)
            self.assertEqual(state['summary']['cached'], 2)
            self.assertEqual(state['summary']['disabled'], 1)
            self.assertEqual(state['aggregate']['source_id'], 'good')
            empty = next(s for s in state['sources'] if s['id'] == 'empty')
            self.assertFalse(empty['ok'])
            self.assertTrue(empty['cached'])
            self.assertEqual(json.loads((out/'empty.json').read_text()), previous)
            self.assertEqual(len(json.loads((out/'subscribe.json').read_text())['urls']), 3)
            self.assertFalse(any(url == 'https://never.test/' for url, _ in session.calls))


if __name__ == '__main__':
    unittest.main()
