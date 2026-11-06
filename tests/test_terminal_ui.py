import contextlib
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import interactive
import main
import terminal_ui as ui


class TerminalPresentationTests(unittest.TestCase):
    def test_panels_fit_narrow_terminals_without_losing_long_names_or_ids(self):
        name = '用于服装纹理实验的很长镜像名称' * 4
        instance = 'instance-' + 'abcdef1234567890' * 5
        for width in (24, 40, 60, 88):
            with self.subTest(width=width):
                text = ui.render_panel('新建实例请求已受理 · 正在准备环境',
                                       [('环境', [('镜像名称', name), ('实例 ID', instance)])],
                                       notes=['这是需要完整保留的中文提示。'], width=width)
                self.assertTrue(all(ui.cells(line) == width for line in text.splitlines()))
                body = ''.join(line[2:-2].strip() for line in text.splitlines()[1:-1])
                self.assertIn(name, body)
                self.assertIn(instance, body)

    def test_redirected_output_has_no_color_or_cursor_control_codes(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ui.panel('本次任务', [(None, [('GPU', 'RTX 5090 × 1 张')])])
            ui.message('第一行\n第二行')
        self.assertNotIn('\x1b', output.getvalue())
        self.assertEqual(output.getvalue().count('第二行'), 1)

    def test_no_color_works_even_for_a_color_capable_terminal(self):
        output = io.StringIO()
        with patch.object(output, 'isatty', return_value=True), contextlib.redirect_stdout(output), \
                patch.dict(os.environ, {'NO_COLOR': '1'}):
            self.assertEqual(ui.tone('测试', 'accent'), '测试')

    def test_task_summary_separates_environment_price_and_full_private_image_info(self):
        config = SimpleNamespace(mode='monitor_and_buy', region_names=['北京B区'], region_sign_list=[],
                                 gpu_model='RTX 5090', gpu_num=1,
                                 image_selection={'kind':'private','name':'111','cuda_version':'12.8'},
                                 expand_data_disk_gib=50, max_price_per_hour=3.5, interval=30)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('terminal_ui.terminal_width',return_value=60):
            interactive.summary(config)
        result=output.getvalue()
        for label in ('资源','环境','费用','我的私有镜像','111','12.8','+50 GB','3.5 元 / 小时'):
            self.assertIn(label,result)
        self.assertTrue(all(ui.cells(line) <= 60 for line in result.splitlines()))

    def test_menu_does_not_put_navigation_in_long_heading(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output), patch('builtins.input',return_value='1'), \
                patch('terminal_ui.terminal_width',return_value=40):
            result=interactive.choose('选择私有镜像',['111 · CUDA 12.8'])
        self.assertEqual(result,'111 · CUDA 12.8')
        self.assertNotIn('选择私有镜像（b',output.getvalue())
        self.assertTrue(all(ui.cells(line)<=40 for line in output.getvalue().splitlines()))

    def test_confirmation_keeps_cancel_and_back_without_long_input_prompt(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch('builtins.input',return_value='b') as read:
            with self.assertRaises(interactive.Back):
                interactive.answer('确认开始自动租用（y/n）','n')
        read.assert_called_once_with('  › ')
        self.assertIn('直接回车使用：n',output.getvalue())

    def test_stock_scan_groups_a_single_snapshot_with_mismatch_note(self):
        config=SimpleNamespace(gpu_model='RTX 5090',gpu_num=1)
        snapshot={"['bj-B1']":{'region_name':'北京B区','listed':True,'total_gpu_num':2536,'idle_gpu_num':0}}
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch('terminal_ui.terminal_width',return_value=64):
            ui.scan(config,snapshot,[{'gpu_idle_num':1}],checks=12,elapsed=75,scan_seconds=1.2,
                    rentable_count=1,same_config_count=1)
        result=output.getvalue()
        self.assertEqual(result.count('资源检查'),1)
        for phrase in ('第 12 次','00:01:15','2536','可租机器','汇总快照空闲 0 张'):
            self.assertIn(phrase,result)
        self.assertTrue(all(ui.cells(line)<=64 for line in result.splitlines()))


if __name__=='__main__':unittest.main()
