import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from config_loader import Config


class ConfigAndCLITests(unittest.TestCase):
    def test_popup_preview_never_logs_in_or_checks_inventory(self):
        with patch('main.DesktopAlert') as popup, \
                patch('main.monitor') as monitor, \
                patch('login_helper.login_and_update_config') as login:
            self.assertEqual(main.main(['--test-popup']), 0)
        popup.return_value.show.assert_called_once()
        monitor.assert_not_called()
        login.assert_not_called()

    def test_legacy_config_keeps_safe_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({'gpu': {'model': 'RTX 5090', 'num': 1}}))
            config = Config(path)
        self.assertEqual(config.mode, 'monitor_only')
        self.assertEqual(config.region_sign_list, [])
        self.assertFalse(config.stop_after_found)
        self.assertEqual(config.interval, 30)

    def test_invalid_config_has_a_clear_error(self):
        for override in (
            {'gpu': {'model': 'RTX 5090', 'num': 0}},
            {'region_sign_list': 'bj-B1'},
            {'monitor': {'interval': 0}},
            {'monitor': {'request_timeout': float('inf')}},
            {'monitor': {'max_retries': True}},
            {'monitor': {'stop_after_found': 'false'}},
            {'notification': {'desktop_popup': 'true'}},
            {'notification': {'popup_cooldown_seconds': -1}},
            {'clone': {'completion_timeout_seconds': 0}},
            {'clone': {'status_interval_seconds': -1}},
        ):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'config.json'
                data = {'gpu': {'model': 'RTX 5090', 'num': 1}, **override}
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    Config(path)

    def test_region_cli_override_does_not_modify_saved_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            original = json.dumps({'authorization': 'test-token',
                                   'gpu': {'model': 'RTX 5090', 'num': 1},
                                   'region_sign_list': ['west-B']})
            path.write_text(original)
            config = Config(path)
            with patch('main.Config', return_value=config), \
                    patch('main.monitor', return_value=0) as monitor:
                self.assertEqual(main.main(['--region', '北京B区', '--once']), 0)
            self.assertEqual(config.region_names, ['北京B区'])
            self.assertEqual(config.region_sign_list, [])
            self.assertTrue(monitor.call_args.kwargs['once'])
            self.assertEqual(path.read_text(), original)

    def test_invalid_json_does_not_crash_with_a_traceback(self):
        output = io.StringIO()
        with patch('main.Config', side_effect=ValueError('配置文件 JSON 格式错误：第 2 行')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main.main([]), 2)
        self.assertIn('第 2 行', output.getvalue())

    def test_token_update_preserves_latest_settings_and_uses_custom_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({'gpu': {'model': 'RTX 5090', 'num': 1}}))
            config = Config(path)
            path.write_text(json.dumps({'gpu': {'model': 'RTX 4090', 'num': 1}}))
            config.update_authorization('test-token')
            saved = json.loads(path.read_text())
        self.assertEqual(saved['authorization'], 'test-token')
        self.assertEqual(saved['gpu']['model'], 'RTX 4090')


if __name__ == '__main__':
    unittest.main()
