import unittest
from unittest.mock import Mock, patch

import requests

import gpu
import notifications
from autodl_api import AuthenticationError, PurchaseUncertainError, request_json
from test_monitor import settings


class APITests(unittest.TestCase):
    def test_zero_summary_does_not_hide_a_rentable_machine(self):
        info = {"['bj-B1']": {'gpu': 'RTX 5090', 'total_gpu_num': 2544,
                                'idle_gpu_num': 0, 'region_name': '北京B区'}}
        with patch('gpu.get_machine_info', return_value=[{
            'machine_id': 'available-now', 'gpu_idle_num': 1, 'region': "['bj-B1']"
        }]) as scan:
            candidates = gpu.find_candidates(settings(), info)
        self.assertEqual(len(candidates), 1)
        scan.assert_called_once()

    def test_machine_scan_records_why_candidates_were_excluded(self):
        base = {'payg_price': 1000, 'on_line': 2, 'max_instance_num': 10,
                'binding_instance_num': 1, 'gpu_idle_num': 1}
        items = [dict(base, machine_id='offline', on_line=3),
                 dict(base, machine_id='full', binding_instance_num=10),
                 dict(base, machine_id='busy', gpu_idle_num=0),
                 dict(base, machine_id='ready')]
        counts = {}
        with patch('gpu.request_json', return_value={'data': {'list': items}}):
            machines = gpu.get_machine_info(settings(), 'RTX 5090', "['bj-B1']", 1,
                                            diagnostics=counts)
        self.assertEqual(counts, {'examined': 4, 'disabled': 1, 'capacity_full': 1,
                                  'insufficient_idle': 1, 'eligible': 1})
        self.assertEqual([m['machine_id'] for m in machines], ['ready'])

    def test_pagination_respects_page_size_returned_by_server(self):
        base = {'payg_price': 1000, 'on_line': 2, 'max_instance_num': 10,
                'binding_instance_num': 1, 'gpu_idle_num': 1}
        with patch('gpu.request_json', side_effect=[
            {'data': {'page_size': 1, 'list': [dict(base, machine_id='one')]}},
            {'data': {'page_size': 1, 'list': []}},
        ]) as request:
            machines = gpu.get_machine_info(settings(), 'RTX 5090', "['bj-B1']", 1)
        self.assertEqual(len(machines), 1)
        self.assertEqual(request.call_count, 2)

    def test_disabled_or_full_machines_are_not_available_even_with_idle_cards(self):
        items = [
            {'machine_id': f'disabled-{status}', 'gpu_idle_num': 8, 'payg_price': 1000,
             'on_line': status, 'max_instance_num': 170, 'binding_instance_num': 10}
            for status in (3, 4, 5, 6)
        ] + [
            {'machine_id': 'full', 'gpu_idle_num': 8, 'payg_price': 1000,
             'on_line': 2, 'max_instance_num': 10, 'binding_instance_num': 10},
            {'machine_id': 'rentable', 'gpu_idle_num': 1, 'payg_price': 1000,
             'on_line': 2, 'max_instance_num': 170, 'binding_instance_num': 10},
        ]
        with patch('gpu.request_json', return_value={'data': {'list': items}}):
            machines = gpu.get_machine_info(settings(), 'RTX 5090', "['bj-B1']", 1)
        self.assertEqual([m['machine_id'] for m in machines], ['rentable'])

    def test_read_request_retries_timeout(self):
        response = Mock(status_code=200, json=lambda: {'code': 'Success', 'data': []})
        with patch('requests.post', side_effect=[requests.Timeout(), response]) as post, \
                patch('time.sleep'):
            self.assertEqual(request_json(settings(max_retries=1), 'test', {}),
                             {'code': 'Success', 'data': []})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs['timeout'], 15)

    def test_order_timeout_does_not_resubmit(self):
        with patch('requests.post', side_effect=requests.Timeout()) as post:
            with self.assertRaises(PurchaseUncertainError):
                request_json(settings(max_retries=5), 'order/test', {}, purchase=True)
        post.assert_called_once()

    def test_order_invalid_response_does_not_resubmit(self):
        with patch('requests.post', return_value=Mock(status_code=200, json=lambda: {})) as post:
            with self.assertRaises(PurchaseUncertainError):
                request_json(settings(), 'order/test', {}, purchase=True)
        post.assert_called_once()

    def test_interrupting_an_order_reports_uncertain_result(self):
        with patch('requests.post', side_effect=KeyboardInterrupt()) as post:
            with self.assertRaises(PurchaseUncertainError):
                request_json(settings(), 'order/test', {}, purchase=True)
        post.assert_called_once()

    def test_authentication_errors_are_not_retried(self):
        with patch('requests.post', return_value=Mock(status_code=401)) as post:
            with self.assertRaises(AuthenticationError):
                request_json(settings(max_retries=3), 'test', {})
        post.assert_called_once()

    def test_unknown_region_order_does_not_crash_after_creation(self):
        candidates = [{'machine_id': 'test-machine', 'machine_price': 1000,
                       'region': "['future-region']"}]
        with patch('gpu.request_json', return_value={'code': 'Success'}) as post:
            result = gpu.buy(settings(), 'RTX 5090', 1, '', '',
                             gpu_info={}, candidates=candidates)
        self.assertTrue(result['success'])
        self.assertEqual(result['region_name'], 'future-region')
        self.assertTrue(post.call_args.kwargs['purchase'])

    def test_machine_pagination_and_per_machine_card_filter(self):
        first = [{'machine_id': str(i), 'gpu_idle_num': 1, 'payg_price': 1000,
                  'on_line': 2, 'max_instance_num': 170, 'binding_instance_num': 10}
                 for i in range(90)]
        last = [{'machine_id': 'last', 'gpu_idle_num': 4, 'payg_price': 1000,
                 'on_line': 2, 'max_instance_num': 170, 'binding_instance_num': 10}]
        with patch('gpu.request_json', side_effect=[{'data': {'list': first}},
                                                   {'data': {'list': last}}]) as post:
            machines = gpu.get_machine_info(settings(), 'RTX 5090', "['bj-B1']", 4)
        self.assertEqual([m['machine_id'] for m in machines], ['last'])
        self.assertEqual(post.call_args.args[2]['page_index'], 2)

    def test_notification_timeout_is_nonfatal_and_has_a_limit(self):
        with patch('requests.post', side_effect=requests.Timeout('secret-url')) as post:
            self.assertFalse(notifications.send_notification(settings(sendkey='test-key'), 'title', 'text'))
        self.assertEqual(post.call_args.kwargs['timeout'], 15)


if __name__ == '__main__':
    unittest.main()
