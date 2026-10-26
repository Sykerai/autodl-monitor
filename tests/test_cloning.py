import contextlib
import io
import unittest
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

import cloning
import main
from autodl_api import PurchaseUncertainError
from config_loader import Config
from test_monitor import settings

GIB = 1024 ** 3


def row(uuid='source-one'):
    return {'uuid': uuid, 'status': 'shutdown', 'charge_type': 'payg',
            'name': '实验环境', 'machine_id': 'source-machine', 'machine_alias': '617机',
            'region_name': '北京B区', 'region_sign': 'bj-B2',
            'snapshot_gpu_alias_name': 'RTX 5090', 'req_gpu_amount': 1,
            'cpu_limit': 25, 'mem_limit_in_byte': 90 * GIB,
            'root_fs_total_size': 30 * GIB, 'private_image_uuid': '',
            'reproduction_uuid': '', 'reproduction_id': 0}


def source_snapshot():
    return {'image': {'image_type': 'public', 'image_name': 'base-test',
                      'cuda_version': '12.8', 'chip_corp': 'nvidia', 'cpu_arch': 'x86'},
            'cpu_arch': 'x86', 'chip_corp': 'nvidia',
            'machine_info_snapshot': {'gpu_type': {'memory': 32 * GIB},
                                      'max_instance_disk_size': 50 * GIB},
            'expand_data_disk_size': 100 * GIB, 'expand_system_disk_size': 0}


def source():
    with patch('cloning.request_json', return_value={'data': source_snapshot()}):
        return cloning.load_source(settings(), row())


def candidate():
    return {'machine_id': 'target-machine', 'region': "['bj-B1', 'bj-B2', 'bj-B3']",
            'region_name': '北京B区', 'gpu_idle_num': 1, 'machine_price': 3030,
            'details': {'gpu_name': 'RTX 5090', 'gpu_memory': 32 * GIB,
                        'cpu_arch': 'x86', 'chip_corp': 'nvidia',
                        'cpu_per_gpu': 25, 'mem_per_gpu': 90 * GIB,
                        'max_instance_disk_size': 50 * GIB,
                        'max_data_disk_expand_size': 500 * GIB,
                        'highest_cuda_version': '13.2'}}


class CloningTests(unittest.TestCase):
    def test_live_source_selection_uses_current_list_not_a_fixed_instance(self):
        with patch('cloning.list_instances', return_value=[row('first'), row('second')]), \
                patch('builtins.input', return_value='2'), \
                patch('cloning.load_source') as load, contextlib.redirect_stdout(io.StringIO()):
            cloning.choose_source(settings())
        self.assertEqual(load.call_args.args[1]['uuid'], 'second')

    def test_noninteractive_selection_requires_explicit_source(self):
        with patch('cloning.list_instances', return_value=[row()]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, '--source'):
                cloning.choose_source(settings(), interactive=False)

    def test_running_source_is_not_automatically_stopped(self):
        running = row(); running['status'] = 'running'
        with patch('cloning.request_json') as request:
            with self.assertRaisesRegex(ValueError, '已关机'):
                cloning.load_source(settings(), running)
        request.assert_not_called()

    def test_source_snapshot_inherits_full_config_and_expanded_data_disk(self):
        src = source()
        self.assertEqual((src.gpu_model, src.gpu_num, src.cpu_cores, src.memory_bytes),
                         ('RTX 5090', 1, 25, 90 * GIB))
        self.assertEqual(src.data_disk_bytes, 150 * GIB)
        config = settings()
        cloning.prepare_config(config, src)
        self.assertEqual(config.region_names, ['北京B区'])
        self.assertEqual(config.region_sign_list, [])

    def test_same_configuration_matches_and_keeps_disk_expansion(self):
        result = cloning.matching_candidates(source(), [candidate()])
        self.assertEqual(result[0]['expand_data_disk'], 100 * GIB)

    def test_different_cpu_memory_disk_or_cuda_is_excluded(self):
        for key, value in [('cpu_per_gpu', 24), ('mem_per_gpu', 80 * GIB),
                           ('gpu_memory', 24 * GIB), ('gpu_name', 'RTX 4090'),
                           ('max_data_disk_expand_size', 99 * GIB),
                           ('highest_cuda_version', '12.4')]:
            with self.subTest(key=key):
                target = candidate(); target['details'][key] = value
                self.assertEqual(cloning.matching_candidates(source(), [target]), [])

    def test_price_preview_includes_account_discount_and_daily_disk_cost(self):
        target = cloning.matching_candidates(source(), [candidate()])[0]
        with patch('cloning.request_json', return_value={'data': {
            'payg_price': 2880, 'data_disk_info': {'data_disk_deal_daily_price': 670}
        }}) as request:
            quote = cloning.preview_price(settings(), source(), target)
        self.assertEqual(quote['gpu_hourly'], Decimal('2.88'))
        self.assertEqual(quote['disk_daily'], Decimal('0.67'))
        self.assertGreater(quote['hourly_equivalent'], Decimal('2.88'))
        self.assertEqual(request.call_args.args[2]['expand_data_disk'], 100 * GIB)

    def test_missing_budget_never_sends_a_clone_request(self):
        with patch('cloning.request_json') as request:
            with self.assertRaisesRegex(ValueError, '上限'):
                cloning.clone_once(settings(clone_max_price=None), source(), [candidate()])
        request.assert_not_called()

    def test_expensive_target_never_sends_a_clone_request(self):
        src = source()
        with patch('cloning.choose_source', return_value=src), \
                patch('cloning.preview_price', return_value={'hourly_equivalent': Decimal('4')}), \
                patch('cloning.request_json') as request:
            result = cloning.clone_once(settings(clone_max_price=3), src, [candidate()])
        self.assertFalse(result['success'])
        request.assert_not_called()

    def test_clone_copies_both_disks_and_uses_dedicated_endpoint(self):
        src = source()
        with patch('cloning.choose_source', return_value=src), \
                patch('cloning.preview_price', return_value={'hourly_equivalent': Decimal('2.91')}), \
                patch('cloning.request_json', return_value={'code': 'Success', 'data': 'new-instance'}) as request:
            result = cloning.clone_once(settings(clone_max_price=3), src, [candidate()])
        self.assertTrue(result['success'])
        self.assertEqual(result['instance_uuid'], 'new-instance')
        self.assertEqual(request.call_args.args[1], 'order/instance/clone/payg')
        payload = request.call_args.args[2]
        self.assertEqual(payload['instance_uuid'], src.uuid)
        self.assertTrue(payload['instance_info']['copy_data_disk_after_clone'])
        self.assertEqual(payload['instance_info']['base_image_uuid'], 'base-test')
        self.assertEqual(payload['instance_info']['req_gpu_amount'], 1)
        self.assertEqual(payload['instance_info']['expand_data_disk'], 100 * GIB)
        self.assertEqual(payload['price_info']['expand_data_disk'], 100 * GIB)
        self.assertTrue(request.call_args.kwargs['purchase'])
        request.assert_called_once()

    def test_source_configuration_drift_stops_before_mutation(self):
        src = source()
        with patch('cloning.choose_source', return_value=replace(src, gpu_num=2)), \
                patch('cloning.request_json') as request:
            with self.assertRaisesRegex(ValueError, '配置已变化'):
                cloning.clone_once(settings(clone_max_price=3), src, [candidate()])
        request.assert_not_called()

    def test_missing_new_instance_id_stops_instead_of_retrying(self):
        src = source()
        with patch('cloning.choose_source', return_value=src), \
                patch('cloning.preview_price', return_value={'hourly_equivalent': Decimal('2')}), \
                patch('cloning.request_json', return_value={'code': 'Success', 'data': None}) as request:
            with self.assertRaises(PurchaseUncertainError):
                cloning.clone_once(settings(clone_max_price=3), src, [candidate(), candidate()])
        request.assert_called_once()

    def test_clone_preview_does_not_start_monitor_or_create_instances(self):
        with patch('cloning.choose_source', return_value=source()), \
                patch('main.monitor') as monitor, patch('cloning.clone_once') as clone, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(['--clone-preview', '--source', 'test-source']), 0)
        monitor.assert_not_called(); clone.assert_not_called()

    def test_automatic_clone_requires_a_budget_even_after_source_selection(self):
        config = Config(); config.clone_max_price = None
        with patch('main.Config', return_value=config), \
                patch('cloning.choose_source', return_value=source()), \
                patch('main.monitor') as monitor, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(['--clone', '--source', 'test-source']), 2)
        monitor.assert_not_called()

    def test_cancelled_confirmation_does_not_start_cloning(self):
        with patch('cloning.choose_source', return_value=source()), \
                patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value='cancel'), \
                patch('main.monitor') as monitor, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(['--clone', '--source', 'test-source', '--max-price', '3']), 0)
        monitor.assert_not_called()

    def test_confirmed_clone_plan_passes_selected_source_and_budget_to_monitor(self):
        src = source()
        with patch('cloning.choose_source', return_value=src), \
                patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value='CLONE'), \
                patch('main.monitor', return_value=0) as monitor, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main(['--clone', '--source', src.uuid, '--max-price', '3']), 0)
        config = monitor.call_args.args[0]
        self.assertEqual(config.mode, 'monitor_and_clone')
        self.assertEqual(config.clone_source.uuid, src.uuid)
        self.assertEqual(config.clone_max_price, 3)
        self.assertEqual(config.gpu_model, src.gpu_model)

    def test_clone_monitor_exits_after_one_accepted_request(self):
        src = source()
        config = settings(mode='monitor_and_clone', clone_source=src, clone_max_price=3)
        snapshot = {"['bj-B1', 'bj-B2', 'bj-B3']": {
            'total_gpu_num': 2544, 'idle_gpu_num': 1, 'region_name': '北京B区'
        }}
        result = {'success': True, 'message': '克隆请求已受理', 'instance_uuid': 'new-instance',
                  'machine_id': 'target-machine', 'region_name': '北京B区'}
        with patch('watch.watch_gpu', return_value=snapshot), \
                patch('gpu.find_candidates', return_value=[candidate()]), \
                patch('cloning.within_budget', side_effect=lambda c, s, targets: targets), \
                patch('cloning.clone_once', return_value=result) as clone, \
                patch('cloning.wait_for_completion', return_value={
                    'status': 'completed', 'message': '系统盘和数据盘均已复制完成'
                }), \
                patch('gpu.buy') as buy, patch('time.sleep') as sleep, \
                patch('notifications.send_notification', return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.monitor(config), 0)
        clone.assert_called_once(); buy.assert_not_called(); sleep.assert_not_called()

    def test_single_clone_query_never_mutates_even_with_compatible_targets(self):
        src = source()
        config = settings(mode='monitor_and_clone', clone_source=src, clone_max_price=None)
        with patch('watch.watch_gpu', return_value={"['bj-B1']": {
            'total_gpu_num': 2544, 'idle_gpu_num': 1
        }}), patch('gpu.find_candidates', return_value=[candidate()]), \
                patch('cloning.clone_once') as clone, patch('gpu.buy') as buy, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.monitor(config, once=True), 0)
        clone.assert_not_called(); buy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
