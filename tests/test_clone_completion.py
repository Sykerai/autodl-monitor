import contextlib
import io
import unittest
from unittest.mock import Mock, patch

import cloning
import main
from autodl_api import APIError
from test_cloning import source
from test_monitor import settings


def cloned_row(status='running', copy_status='Succeed', copy_data=True):
    return {'uuid': 'new-instance', 'status': status,
            'additional_info': {'copy_data_disk_after_clone': copy_data,
                                'copy_data_disk_after_clone_status': copy_status}}


class CompletionTests(unittest.TestCase):
    def test_success_requires_both_ready_instance_and_successful_data_copy(self):
        for status in ('running', 'shutdown'):
            self.assertEqual(cloning.completion_status(cloned_row(status)), 'completed')
        for row in (cloned_row('created_cloning'), cloned_row(copy_status='Running'),
                    cloned_row(copy_status=''), cloned_row(copy_data=False)):
            self.assertEqual(cloning.completion_status(row), 'pending')

    def test_platform_failures_are_not_mistaken_for_completion(self):
        self.assertEqual(cloning.completion_status(cloned_row(copy_status='Failed')), 'failed')
        self.assertEqual(cloning.completion_status(cloned_row('create_failed')), 'failed')

    def test_status_poll_waits_through_creation_and_copying(self):
        with patch('cloning.list_instances', side_effect=[
            [], [cloned_row('created_cloning', 'Running')], [cloned_row()]
        ]) as read, patch('cloning.time.sleep') as sleep:
            result = cloning.wait_for_completion(settings(), 'new-instance')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(read.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_read_error_does_not_trigger_another_clone(self):
        with patch('cloning.list_instances', side_effect=[APIError('offline'), [cloned_row()]]), \
                patch('cloning.clone_once') as clone, patch('cloning.time.sleep'):
            result = cloning.wait_for_completion(settings(), 'new-instance')
        self.assertEqual(result['status'], 'completed')
        clone.assert_not_called()

    def test_deadline_is_pending_not_false_success_or_failure(self):
        with patch('cloning.time.monotonic', side_effect=[0, 0, 1, 1]), \
                patch('cloning.list_instances', return_value=[cloned_row(copy_status='Running')]):
            result = cloning.wait_for_completion(settings(clone_completion_timeout=1), 'new-instance')
        self.assertEqual(result['status'], 'pending')

    def test_interrupt_only_stops_tracking(self):
        with patch('cloning.list_instances', side_effect=KeyboardInterrupt()):
            result = cloning.wait_for_completion(settings(), 'new-instance')
        self.assertEqual(result['status'], 'pending')
        self.assertIn('仍可能继续', result['message'])

    def test_completion_alert_replaces_availability_and_is_not_rate_limited(self):
        src = source()
        popup = Mock()
        result = {'region_name': '北京B区', 'instance_uuid': 'new-instance'}
        with patch('cloning.wait_for_completion', return_value={
            'status': 'completed', 'message': '系统盘克隆和数据盘复制均已完成'
        }), patch('notifications.send_notification', return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            code = main.finish_clone(settings(), popup, src, result)
        self.assertEqual(code, 0)
        self.assertEqual(popup.show.call_count, 2)
        first, last = popup.show.call_args_list
        self.assertEqual(first.args[0], '克隆请求已受理')
        self.assertIn('尚未确认复制完成', first.args[1])
        self.assertEqual(last.args[0], '克隆成功 · 数据复制完成')
        self.assertIn('new-instance', last.args[1])
        self.assertTrue(first.kwargs['force'])
        self.assertTrue(last.kwargs['force'])

    def test_unexpected_tracking_error_exits_without_resubmitting(self):
        src = source()
        with patch('cloning.wait_for_completion', side_effect=APIError('offline')), \
                patch('cloning.clone_once') as clone, patch('notifications.send_notification'), \
                contextlib.redirect_stdout(io.StringIO()):
            popup = Mock()
            code = main.finish_clone(settings(), popup, src,
                                     {'region_name': '北京B区', 'instance_uuid': 'new-instance'})
        self.assertEqual(code, 2)
        self.assertEqual(popup.show.call_args.args[0], '克隆进度待确认')
        clone.assert_not_called()


if __name__ == '__main__':
    unittest.main()
