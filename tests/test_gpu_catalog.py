"""GPU selection must use regional availability, rather than the global catalog."""
import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import catalog
import interactive
from autodl_api import APIError


def model(name):
    return {'gpu_name': name, 'cpu_arch': 'x86', 'chip_corp': 'nvidia'}


class RegionalGPUCatalogTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(region_names=['北京B区'], region_sign_list=[], gpu_model='RTX 5090')
        self.global_rows = [model('RTX 5090'), model('H800'), model('RTX 4090')]
        self.region_rows = [
            {'RTX 5090': {'total_gpu_num': 2536, 'idle_gpu_num': 1, 'cpu_arch': 'x86', 'chip_corp': 'nvidia'}},
            {'RTX 4090': {'total_gpu_num': 1352, 'idle_gpu_num': 0, 'cpu_arch': 'x86', 'chip_corp': 'nvidia'}},
        ]

    def api(self, config, path, payload=None, **kwargs):
        if path == 'machine/gpu_type':
            return {'data': self.global_rows}
        if path == 'machine/region/gpu_type':
            return {'data': self.region_rows}
        raise AssertionError('Unexpected endpoint: ' + path)

    def test_selected_region_excludes_other_regions_but_keeps_zero_idle_models(self):
        with patch('regions.request_json', return_value={'data': [
            {'region_name': '北京B区', 'region_sign': 'bj-B1'},
            {'region_name': '北京B区', 'region_sign': 'bj-B2'},
            {'region_name': '北京B区', 'region_sign': 'bj-B3'},
        ]}), patch('catalog.request_json', side_effect=self.api) as api, \
                patch('interactive.choose', side_effect=lambda prompt, rows, **kwargs: rows[0]) as choose, \
                contextlib.redirect_stdout(io.StringIO()):
            interactive.select_gpu(self.config)
        offered = choose.call_args.args[1]
        self.assertEqual([row['gpu_name'] for row in offered], ['RTX 5090', 'RTX 4090'])
        self.assertEqual(self.config.gpu_model, 'RTX 5090')
        self.assertEqual(choose.call_args.kwargs['label'](offered[1]), 'RTX 4090 · 空闲 0 / 总数 1352 张 · x86 / nvidia')
        self.assertIn('北京B区', choose.call_args.args[0])
        api.assert_called_once_with(self.config, 'machine/region/gpu_type',
                                    {'region_sign_list': ['bj-B1', 'bj-B2', 'bj-B3']})

    def test_multiple_regions_union_and_aggregate_without_dropping_zero_idle(self):
        groups = [(['bj-B1'], '北京B区'), (['west-B', 'west-C'], '西北B区')]
        other = [
            {'RTX 5090': {'total_gpu_num': 100, 'idle_gpu_num': 2, 'cpu_arch': 'x86', 'chip_corp': 'nvidia'}},
            {'H800': {'total_gpu_num': 50, 'idle_gpu_num': 0, 'cpu_arch': 'x86', 'chip_corp': 'nvidia'}},
        ]
        with patch('catalog.selected_region_groups', return_value=groups), \
                patch('catalog.request_json', side_effect=[{'data': self.region_rows}, {'data': other}]):
            result = catalog.list_gpu_types(self.config)
        self.assertEqual([r['gpu_name'] for r in result], ['RTX 5090', 'RTX 4090', 'H800'])
        self.assertEqual((result[0]['idle_gpu_num'], result[0]['total_gpu_num']), (3, 2636))
        self.assertEqual([r['name'] for r in result[0]['regions']], ['北京B区', '西北B区'])
        self.assertEqual(result[2]['idle_gpu_num'], 0)

    def test_empty_region_never_falls_back_to_global_catalog(self):
        with patch('catalog.selected_region_groups', return_value=[(['bj-B1'], '北京B区')]), \
                patch('catalog.request_json', return_value={'data': []}) as api, self.assertRaises(APIError):
            catalog.list_gpu_types(self.config)
        self.assertEqual(api.call_args.args[1], 'machine/region/gpu_type')

    def test_invalid_regional_inventory_is_not_silently_used(self):
        for invalid in ['1', -1, True, None]:
            with self.subTest(invalid=invalid):
                stats = dict(self.region_rows[0]['RTX 5090'], idle_gpu_num=invalid)
                with patch('catalog.selected_region_groups', return_value=[(['bj-B1'], '北京B区')]), \
                        patch('catalog.request_json', return_value={'data': [{'RTX 5090': stats}]}), self.assertRaises(APIError):
                    catalog.list_gpu_types(self.config)

    def test_cpu_excluded_and_new_regional_model_needs_no_global_metadata(self):
        rows = [{'CPU': {'total_gpu_num': 48, 'idle_gpu_num': 0}},
                {'Future GPU': {'total_gpu_num': 8, 'idle_gpu_num': 0, 'cpu_arch': 'x86', 'chip_corp': 'nvidia'}}]
        with patch('catalog.selected_region_groups', return_value=[(['bj-B1'], '北京B区')]), \
                patch('catalog.request_json', return_value={'data': rows}):
            result = catalog.list_gpu_types(self.config)
        self.assertEqual([r['gpu_name'] for r in result], ['Future GPU'])

    def test_previously_selected_unavailable_model_is_not_defaulted(self):
        self.config.gpu_model = 'H800'
        with patch('catalog.selected_region_groups', return_value=[(['bj-B1'], '北京B区')]), \
                patch('catalog.request_json', return_value={'data': self.region_rows}), \
                patch('interactive.choose', side_effect=lambda prompt, rows, **kwargs: rows[0]) as choose:
            interactive.select_gpu(self.config)
        self.assertEqual(choose.call_args.kwargs['defaults'], [])


if __name__ == '__main__':
    unittest.main()
