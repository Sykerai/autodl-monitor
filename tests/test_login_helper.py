import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

import login_helper


class LoginTests(unittest.TestCase):
    def test_scan_is_detected_while_qr_window_remains_open(self):
        window_closed = threading.Event()
        login_polled = threading.Event()
        image = BytesIO()
        Image.new('RGB', (2, 2)).save(image, format='PNG')

        def show(*args, **kwargs):
            if kwargs.get('block', True):
                window_closed.wait(2)

        def post(url, **kwargs):
            if url.endswith('/polling/query'):
                login_polled.set()
                return Mock(json=lambda: {'code': 'Success', 'data': {'ticket': 'test-ticket'}})
            return Mock(json=lambda: {'code': 'Success', 'data': {'token': 'test-token'}})

        with patch.object(login_helper.requests, 'get', side_effect=[
            Mock(json=lambda: {'data': {'qrcode_url': 'https://example.test/qr', 'uuid': 'test-uuid'}}),
            Mock(content=image.getvalue()),
        ]), patch.object(login_helper.requests, 'post', side_effect=post), \
                patch.object(login_helper, 'update_config_authorization'), \
                patch.object(login_helper.plt, 'show', side_effect=show), \
                patch.object(login_helper.plt, 'imshow'), \
                patch.object(login_helper.plt, 'axis'), \
                patch.object(login_helper.plt, 'pause'), \
                patch.object(login_helper.plt, 'close'), \
                ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(login_helper.login_and_update_config)
            try:
                self.assertTrue(login_polled.wait(0.5),
                                '二维码窗口未关闭时，程序没有查询手机扫码结果')
            finally:
                window_closed.set()
                self.assertEqual(result.result(timeout=2), 'test-token')

    def test_token_is_saved_beside_login_module(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'project'
            project.mkdir()
            config_path = project / 'config.json'
            config_path.write_text(json.dumps({'mode': 'monitor_only', 'authorization': ''}))
            with patch.object(login_helper, '__file__', str(project / 'login_helper.py')):
                login_helper.update_config_authorization('test-token')
            saved = json.loads(config_path.read_text())
            self.assertEqual(saved['authorization'], 'test-token')
            self.assertEqual(saved['mode'], 'monitor_only')

    def test_pending_scan_is_polled_again(self):
        with patch.object(login_helper.requests, 'post', side_effect=[
            Mock(json=lambda: {'data': None}),
            Mock(json=lambda: {'data': {'ticket': 'test-ticket'}}),
        ]) as post, patch.object(login_helper.plt, 'pause'):
            self.assertEqual(login_helper.wait_for_wechat_ticket({}, 'test-uuid'), 'test-ticket')
            self.assertEqual(post.call_count, 2)

    def test_request_timeout_does_not_stop_waiting_for_scan(self):
        with patch.object(login_helper.requests, 'post', side_effect=[
            login_helper.requests.Timeout(),
            Mock(json=lambda: {'data': {'ticket': 'test-ticket'}}),
        ]), patch.object(login_helper.plt, 'pause'):
            self.assertEqual(login_helper.wait_for_wechat_ticket({}, 'test-uuid'), 'test-ticket')

    def test_login_wait_has_a_deadline(self):
        with patch.object(login_helper.time, 'monotonic', side_effect=[0, 0, 181]), \
                patch.object(login_helper.requests, 'post', return_value=Mock(json=lambda: {'data': None})), \
                patch.object(login_helper.plt, 'pause'):
            with self.assertRaisesRegex(TimeoutError, '等待微信登录超时'):
                login_helper.wait_for_wechat_ticket({}, 'test-uuid')


if __name__ == '__main__':
    unittest.main()
