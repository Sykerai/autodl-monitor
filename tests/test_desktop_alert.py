import unittest
from unittest.mock import Mock, patch

from desktop_alert import ALERT_SCRIPT, DesktopAlert


class DesktopAlertTests(unittest.TestCase):
    @patch('desktop_alert.sys.platform', 'darwin')
    @patch('desktop_alert.shutil.which', return_value='/usr/bin/osascript')
    @patch('desktop_alert.subprocess.Popen')
    def test_message_is_passed_as_data_and_process_does_not_block(self, launch, _):
        alert = DesktopAlert()
        message = '北京B区\nRTX "5090"\n空闲 1 张'
        self.assertTrue(alert.show('GPU空闲', message))
        self.assertEqual(launch.call_args.args[0], ['osascript', '-e', ALERT_SCRIPT, 'GPU空闲', message])
        self.assertTrue(launch.call_args.kwargs['start_new_session'])

    @patch('desktop_alert.sys.platform', 'darwin')
    @patch('desktop_alert.shutil.which', return_value='/usr/bin/osascript')
    @patch('desktop_alert.subprocess.Popen')
    def test_open_dialog_is_not_duplicated_even_after_cooldown(self, launch, _):
        launch.return_value.poll.return_value = None
        alert = DesktopAlert(cooldown=0)
        self.assertTrue(alert.show('title', 'text'))
        self.assertFalse(alert.show('title', 'text'))
        launch.assert_called_once()

    @patch('desktop_alert.sys.platform', 'darwin')
    @patch('desktop_alert.shutil.which', return_value='/usr/bin/osascript')
    @patch('desktop_alert.subprocess.Popen')
    def test_closed_dialog_still_respects_cooldown(self, launch, _):
        launch.return_value.poll.return_value = 0
        alert = DesktopAlert(cooldown=300)
        with patch('desktop_alert.time.monotonic', side_effect=[0, 299, 300]):
            self.assertTrue(alert.show('title', 'text'))
            self.assertFalse(alert.show('title', 'text'))
            self.assertTrue(alert.show('title', 'text'))
        self.assertEqual(launch.call_count, 2)

    def test_disabled_alert_never_starts_a_process(self):
        with patch('desktop_alert.subprocess.Popen') as launch:
            self.assertFalse(DesktopAlert(enabled=False).show('title', 'text'))
        launch.assert_not_called()

    @patch('desktop_alert.sys.platform', 'darwin')
    @patch('desktop_alert.shutil.which', return_value='/usr/bin/osascript')
    @patch('desktop_alert.subprocess.Popen')
    def test_result_alert_replaces_open_window_and_bypasses_cooldown(self, launch, _):
        launch.return_value.poll.return_value = None
        alert = DesktopAlert(cooldown=300)
        self.assertTrue(alert.show('发现空闲', 'text'))
        self.assertTrue(alert.show('克隆成功', '数据复制完成', force=True))
        launch.return_value.terminate.assert_called_once()
        self.assertEqual(launch.call_count, 2)

    @patch('desktop_alert.sys.platform', 'darwin')
    @patch('desktop_alert.shutil.which', return_value='/usr/bin/osascript')
    @patch('desktop_alert.subprocess.Popen')
    def test_result_alert_is_shown_even_after_recent_window_was_closed(self, launch, _):
        launch.return_value.poll.return_value = 0
        alert = DesktopAlert(cooldown=300)
        self.assertTrue(alert.show('发现空闲', 'text'))
        self.assertTrue(alert.show('克隆成功', '数据复制完成', force=True))
        self.assertEqual(launch.call_count, 2)

    @patch('desktop_alert.sys.platform', 'darwin')
    @patch('desktop_alert.shutil.which', return_value='/usr/bin/osascript')
    def test_unavailable_desktop_does_not_raise(self, _):
        with patch('desktop_alert.subprocess.Popen', side_effect=OSError()):
            alert = DesktopAlert()
            self.assertFalse(alert.show('title', 'text'))
        self.assertIn('监控仍会继续', alert.error)


if __name__ == '__main__':
    unittest.main()
