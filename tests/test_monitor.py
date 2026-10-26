import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
import watch
from autodl_api import APIError, AuthenticationError


def settings(**overrides):
    values = dict(Authorization='test-token', mode='monitor_only', gpu_model='RTX 5090',
                  gpu_num=1, region_sign_list=['bj-B1'], interval=30,
                  retry_interval=10, request_timeout=15, max_retries=0,
                  sendkey='', notification_cooldown=300, stop_after_found=False,
                  desktop_popup=False, popup_cooldown=300,
                  image='test-image', private_image_uuid='')
    values.update(overrides)
    return SimpleNamespace(**values)


def snapshot(idle=0):
    return {"['bj-B1']": {'gpu': 'RTX 5090', 'total_gpu_num': 480,
                            'idle_gpu_num': idle, 'chip_corp': 'nvidia', 'cpu_arch': 'x86'}}


class StopMonitoring(Exception):
    pass


class MonitorRegressionTests(unittest.TestCase):
    def test_positive_region_stock_but_no_machine_explains_the_difference(self):
        output = io.StringIO()
        with patch('watch.watch_gpu', return_value=snapshot(1)), \
                patch('gpu.find_candidates', return_value=[]), \
                patch('gpu.buy') as buy, patch('cloning.clone_once') as clone, \
                patch('main.DesktopAlert') as popup, contextlib.redirect_stdout(output):
            self.assertEqual(main.monitor(settings(), once=True), 0)
        self.assertIn('地区汇总显示空闲 1 张', output.getvalue())
        self.assertIn('机器明细未找到可租机器', output.getvalue())
        buy.assert_not_called(); clone.assert_not_called()
        popup.return_value.show.assert_not_called()

    def test_available_machine_triggers_popup_without_sendkey(self):
        with patch('watch.watch_gpu', return_value=snapshot(1)), \
                patch('gpu.find_candidates', return_value=[{'machine_id': 'test', 'gpu_idle_num': 1, 'region': "['bj-B1']"}]), \
                patch('main.DesktopAlert') as popup, \
                patch('time.sleep', side_effect=StopMonitoring):
            with self.assertRaises(StopMonitoring):
                main.monitor(settings(desktop_popup=True, sendkey=''))
        popup.return_value.show.assert_called_once()
        title, message = popup.return_value.show.call_args.args
        self.assertIn('RTX 5090', title)
        self.assertIn('北京B区', message)
        self.assertIn('可租机器：1 台', message)

    def test_popup_card_count_uses_rentable_machine_details(self):
        candidates = [
            {'machine_id': 'one', 'gpu_idle_num': 2, 'region': "['bj-B1']"},
            {'machine_id': 'two', 'gpu_idle_num': 3, 'region': "['bj-B1']"},
        ]
        with patch('watch.watch_gpu', return_value=snapshot(1)), \
                patch('gpu.find_candidates', return_value=candidates), \
                patch('main.DesktopAlert') as popup, \
                patch('time.sleep', side_effect=StopMonitoring):
            with self.assertRaises(StopMonitoring):
                main.monitor(settings(desktop_popup=True))
        message = popup.return_value.show.call_args.args[1]
        self.assertIn('可租机器：2 台', message)
        self.assertIn('可租机器空闲卡数：5 张', message)

    def test_zero_idle_or_unsuitable_machine_does_not_trigger_popup(self):
        for idle in (0, 4):
            with self.subTest(idle=idle), \
                    patch('watch.watch_gpu', return_value=snapshot(idle)), \
                    patch('gpu.find_candidates', return_value=[]), \
                    patch('main.DesktopAlert') as popup, \
                    patch('time.sleep', side_effect=StopMonitoring):
                with self.assertRaises(StopMonitoring):
                    main.monitor(settings(desktop_popup=True))
                popup.return_value.show.assert_not_called()

    def test_once_does_not_trigger_popup(self):
        with patch('watch.watch_gpu', return_value=snapshot(1)), \
                patch('gpu.find_candidates', return_value=[{'machine_id': 'test', 'gpu_idle_num': 1, 'region': "['bj-B1']"}]), \
                patch('main.DesktopAlert') as popup:
            self.assertEqual(main.monitor(settings(desktop_popup=True), once=True), 0)
        popup.return_value.show.assert_not_called()

    def test_named_region_includes_all_current_subregions(self):
        config = settings(region_names=['北京B区'])
        with patch('requests.get', return_value=Mock(status_code=200, json=lambda: {
            'code': 'Success', 'data': [
                {'region_name': '北京B区', 'region_sign': 'bj-B1'},
                {'region_name': '北京B区', 'region_sign': 'bj-B2'},
                {'region_name': '北京B区', 'region_sign': 'bj-B3'},
            ]
        })), patch('requests.post', return_value=Mock(status_code=200, json=lambda: {
            'code': 'Success', 'data': [{'RTX 5090': {'total_gpu_num': 2544, 'idle_gpu_num': 0}}]
        })) as post:
            result = watch.watch_gpu(config, 'RTX 5090')
        self.assertEqual(post.call_args.kwargs['json']['region_sign_list'], ['bj-B1', 'bj-B2', 'bj-B3'])
        self.assertEqual(len(result), 1)
        self.assertEqual(next(iter(result.values()))['total_gpu_num'], 2544)

    def test_zero_idle_cards_keeps_monitoring(self):
        output = io.StringIO()
        with patch('watch.watch_gpu', return_value=snapshot()), \
                patch('gpu.get_machine_info', return_value=[]) as machines, \
                patch('notifications.send_notification') as notify, \
                patch('gpu.buy') as buy, \
                patch('time.sleep', side_effect=StopMonitoring), \
                contextlib.redirect_stdout(output):
            with self.assertRaises(StopMonitoring):
                main.monitor(settings())
        self.assertIn('暂无可用GPU', output.getvalue())
        self.assertNotIn('发现可用GPU', output.getvalue())
        machines.assert_called_once()
        notify.assert_not_called()
        buy.assert_not_called()

    def test_explicit_beijing_region_overrides_legacy_gpu_map(self):
        response = Mock(status_code=200, json=lambda: {'code': 'Success', 'data': [
            {'RTX 5090': {'total_gpu_num': 8, 'idle_gpu_num': 0}}
        ]})
        with patch('requests.post', return_value=response) as post:
            result = watch.watch_gpu(settings(), 'RTX 5090')
        self.assertEqual(post.call_args.kwargs['json']['region_sign_list'], ['bj-B1'])
        self.assertIn("['bj-B1']", result)

    def test_new_subregion_is_discovered_without_changing_code(self):
        config = settings(region_names=['北京B区'], region_sign_list=[])
        with patch('regions.request_json', return_value={'data': [
            {'region_name': '北京B区', 'region_sign': code}
            for code in ['bj-B1', 'bj-B2', 'bj-B3', 'bj-B4']
        ]}), patch('watch.request_json', return_value={
            'data': [{'RTX 5090': {'total_gpu_num': 2600, 'idle_gpu_num': 0}}]
        }) as post:
            watch.watch_gpu(config, 'RTX 5090')
        self.assertEqual(post.call_args.args[2]['region_sign_list'],
                         ['bj-B1', 'bj-B2', 'bj-B3', 'bj-B4'])

    def test_missing_region_name_does_not_fall_back_to_other_regions(self):
        with patch('regions.request_json', return_value={'data': []}), \
                patch('watch.request_json') as stock:
            with self.assertRaisesRegex(ValueError, '没有'):
                watch.watch_gpu(settings(region_names=['不存在的地区']), 'RTX 5090')
        stock.assert_not_called()

    def test_missing_model_is_distinct_from_zero_stock(self):
        with patch('watch.request_json', return_value={'data': []}):
            result = watch.watch_gpu(settings(), 'RTX 5090')
        self.assertFalse(result["['bj-B1']"]['listed'])
        self.assertEqual(watch.available_regions(result), {})

    def test_invalid_stock_is_not_treated_as_availability(self):
        with patch('watch.request_json', return_value={
            'data': [{'RTX 5090': {'total_gpu_num': 8, 'idle_gpu_num': '5'}}]
        }):
            with self.assertRaises(APIError):
                watch.watch_gpu(settings(), 'RTX 5090')

    def test_region_total_does_not_imply_one_machine_has_enough_cards(self):
        with patch('watch.watch_gpu', return_value=snapshot(4)), \
                patch('gpu.get_machine_info', return_value=[]) as machines, \
                patch('gpu.buy') as buy:
            self.assertEqual(main.monitor(settings(gpu_num=4), once=True), 0)
        machines.assert_called_once()
        buy.assert_not_called()

    def test_positive_stock_keeps_monitoring_and_limits_pushes(self):
        with patch('watch.watch_gpu', return_value=snapshot(1)), \
                patch('gpu.find_candidates', return_value=[{'machine_id': 'test', 'gpu_idle_num': 1, 'region': "['bj-B1']"}]), \
                patch('notifications.send_notification', return_value=True) as notify, \
                patch('time.sleep', side_effect=[None, StopMonitoring]):
            with self.assertRaises(StopMonitoring):
                main.monitor(settings(sendkey='test-key', notification_cooldown=300))
        notify.assert_called_once()

    def test_once_never_buys_even_when_purchase_mode_is_configured(self):
        with patch('watch.watch_gpu', return_value=snapshot(1)), \
                patch('gpu.find_candidates', return_value=[{'machine_id': 'test', 'gpu_idle_num': 1, 'region': "['bj-B1']"}]), \
                patch('gpu.buy') as buy:
            self.assertEqual(main.monitor(settings(mode='monitor_and_buy'), once=True), 0)
        buy.assert_not_called()

    def test_network_error_recovers_next_round(self):
        with patch('watch.watch_gpu', side_effect=[APIError('网络异常'), snapshot()]) as poll, \
                patch('gpu.get_machine_info', return_value=[]), \
                patch('time.sleep', side_effect=[None, StopMonitoring]):
            with self.assertRaises(StopMonitoring):
                main.monitor(settings())
        self.assertEqual(poll.call_count, 2)

    def test_expired_login_exits_with_an_actionable_error(self):
        output = io.StringIO()
        with patch('watch.watch_gpu', side_effect=AuthenticationError('请使用 --login 重新扫码登录')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(main.monitor(settings()), 2)
        self.assertIn('--login', output.getvalue())

    def test_created_instance_is_not_retried_when_notification_fails(self):
        with patch('watch.watch_gpu', return_value=snapshot(1)), \
                patch('gpu.find_candidates', return_value=[{'machine_id': 'test', 'gpu_idle_num': 1, 'region': "['bj-B1']"}]), \
                patch('gpu.buy', return_value={'success': True, 'message': '创建成功',
                      'region_name': '北京B区', 'machine_id': 'test'}) as buy, \
                patch('notifications.send_notification', return_value=False):
            self.assertEqual(main.monitor(settings(mode='monitor_and_buy', sendkey='test-key')), 0)
        buy.assert_called_once()


if __name__ == '__main__':
    unittest.main()
