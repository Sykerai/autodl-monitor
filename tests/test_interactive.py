import contextlib
import io
import json
import math
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch, Mock

import catalog
import gpu
import interactive
import main
import pricing
from autodl_api import request_json, PurchaseUncertainError, AuthenticationError
from config_loader import Config
from local_state import process_lock, atomic_json
from task_store import TaskStore, task_plan
from test_cloning import source, candidate, row, source_snapshot

IMAGE = {'kind': 'base', 'uuid': 'base-id', 'name': 'PyTorch / 2.5 / 3.12 / 12.4',
         'cuda_version': '12.4', 'cpu_arch': 'x86', 'chip_corp': 'nvidia'}
GPU = {'gpu_name': 'RTX 5090', 'cpu_arch': 'x86', 'chip_corp': 'nvidia'}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Config(Path(self.directory.name) / 'config.json')
        self.config.Authorization = 'secret-token'
        # CLI entry tests must not share the real user's currently running process lock.
        lock_patch = patch('local_state.process_lock', side_effect=lambda directory: process_lock(self.config.state_dir))
        lock_patch.start()
        self.addCleanup(lock_patch.stop)
        self.store = TaskStore(self.config.state_dir / 'task.json')
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def prepare(self):
        self.config.mode = 'monitor_and_buy'
        self.config.image_selection = IMAGE.copy()
        self.store.start(task_plan(self.config))
        self.config.task_journal = self.store

    def test_missing_settings_bootstrap_without_copying_template(self):
        self.assertTrue(self.config.config_path.exists())
        self.assertEqual(self.config.mode, 'monitor_only')
        self.assertEqual(self.config.max_price_per_hour, 3)
        self.assertEqual(os.stat(self.config.config_path).st_mode & 0o777, 0o600)

    def test_default_entry_is_menu_even_with_saved_purchase_mode(self):
        self.config.mode = 'monitor_and_buy'
        with patch('main.Config', return_value=self.config), patch('interactive.run', return_value=0) as menu, patch('main.monitor') as monitor:
            self.assertEqual(main.main([]), 0)
        menu.assert_called_once(); monitor.assert_not_called()

    def test_invalid_selection_reprompts_and_search_keeps_identity(self):
        with patch('builtins.input', side_effect=['/5090', '2', '1']):
            result = interactive.choose('GPU', ['RTX 5090', 'RTX 4090'])
        self.assertEqual(result, 'RTX 5090')

    def test_multi_region_dedup_preserves_user_priority(self):
        with patch('builtins.input', return_value='2,1,2'):
            self.assertEqual(interactive.choose('地区', ['北京', '内蒙'], multiple=True), ['内蒙', '北京'])

    def test_back_from_amount_returns_to_gpu_step(self):
        self.config.mode = 'monitor_only'
        with patch('interactive.ensure_login'), patch('interactive.select_regions') as regions, \
                patch('interactive.select_gpu') as choose_gpu, patch('interactive.select_amount', side_effect=[interactive.Back(), None]), \
                patch('interactive.yes', return_value=True):
            interactive.build_plan(self.config, 'monitor_only')
        self.assertEqual(choose_gpu.call_count, 2)
        regions.assert_called_once()

    def test_new_wizard_has_expansion_and_budget_without_purchase(self):
        with patch('interactive.ensure_login'), patch('interactive.select_regions'), patch('interactive.select_gpu'), \
                patch('interactive.select_amount'), patch('interactive.select_image'), \
                patch('interactive.select_disk') as disk, patch('interactive.select_budget') as budget, \
                patch('interactive.yes', return_value=True), patch('gpu.buy') as buy:
            result = interactive.build_plan(self.config, 'monitor_and_buy')
        disk.assert_called_once(); budget.assert_called_once(); buy.assert_not_called()
        self.assertEqual(result.mode, 'monitor_and_buy')

    def test_clone_wizard_never_asks_for_regions_model_image_or_expansion(self):
        def select(config):
            import cloning
            cloning.prepare_config(config, source())
        with patch('interactive.ensure_login'), patch('interactive.select_source', side_effect=select), \
                patch('interactive.select_regions') as regions, patch('interactive.select_gpu') as gpu_select, \
                patch('interactive.select_image') as image, patch('interactive.select_disk') as disk, \
                patch('interactive.select_budget'), patch('interactive.yes', return_value=True):
            result = interactive.build_plan(self.config, 'monitor_and_clone')
        for mock in (regions, gpu_select, image, disk):mock.assert_not_called()
        self.assertEqual(result.region_names, [source().region_name])

    def test_settings_save_preserves_latest_token_and_never_saves_purchase_mode(self):
        self.config.mode = 'monitor_and_buy'
        self.config.gpu_num = 2
        latest = json.loads(self.config.config_path.read_text())
        latest['authorization'] = 'new-token'
        atomic_json(self.config.config_path, latest)
        self.config.save_preferences()
        saved = json.loads(self.config.config_path.read_text())
        self.assertEqual(saved['authorization'], 'new-token')
        self.assertEqual(saved['gpu']['num'], 2)
        self.assertEqual(saved['mode'], 'monitor_only')

    def test_price_includes_all_cards_quote_and_expansion_daily_fee(self):
        with patch('pricing.request_json', return_value={'data': {'payg_price': 2900,
                'data_disk_info': {'data_disk_deal_daily_price': 4800}}}) as api:
            quote = pricing.preview(self.config, {'machine_id': 'm'}, 2, 50*pricing.GIB)
        self.assertEqual(quote['hourly_equivalent'], Decimal('3.1'))
        self.assertEqual(api.call_args.args[2]['num'], 2)
        self.assertEqual(api.call_args.args[2]['expand_data_disk'], 50*pricing.GIB)

    def test_missing_expansion_fee_or_nonfinite_price_blocks_order(self):
        for data in [{'payg_price': 1}, {'payg_price': float('nan')}, {'payg_price': 1, 'data_disk_info': {'data_disk_deal_daily_price': -1}}]:
            with patch('pricing.request_json', return_value={'data': data}), self.assertRaises(Exception):
                pricing.preview(self.config, {'machine_id': 'm'}, 1, pricing.GIB)

    def test_incompatible_image_or_insufficient_expansion_excludes_machine(self):
        self.config.image_selection = IMAGE.copy(); self.config.expand_data_disk_gib = 50
        base = {'machine_id': 'm', 'details': {'gpu_name': 'RTX 5090', 'cpu_arch': 'x86', 'chip_corp': 'nvidia',
                'highest_cuda_version': '12.8', 'max_data_disk_expand_size': 100*pricing.GIB}}
        for changes in [{'cpu_arch': 'arm'}, {'highest_cuda_version': '11.8'}, {'max_data_disk_expand_size': 0}]:
            bad = dict(base, details={**base['details'], **changes})
            self.assertEqual(gpu.compatible_candidates(self.config, [bad]), [])
        self.assertEqual(len(gpu.compatible_candidates(self.config, [base])), 1)

    def test_new_purchase_ranks_and_rechecks_price_and_keeps_expansion(self):
        self.prepare(); self.config.expand_data_disk_gib = 50
        machines = [dict(candidate(), machine_id='expensive'), dict(candidate(), machine_id='cheap')]
        for machine in machines:machine['details']['highest_cuda_version']='12.8'
        def price(config, machine, num, expansion):
            return {'hourly_equivalent': Decimal('2.9' if machine['machine_id']=='expensive' else '2.0')}
        with patch('catalog.refresh_image'), patch('pricing.preview', side_effect=price), \
                patch('gpu.request_json', return_value={'code': 'Success','data':'new-uuid'}) as order:
            result = gpu.buy(self.config, 'RTX 5090', 1, '', '', candidates=machines, gpu_info={})
        self.assertTrue(result['success'])
        payload=order.call_args.args[2]
        self.assertEqual(payload['instance_info']['machine_id'], 'cheap')
        self.assertEqual(payload['instance_info']['base_image_uuid'], 'base-id')
        self.assertEqual(payload['instance_info']['expand_data_disk'], 50*pricing.GIB)
        self.assertNotIn('image', payload['instance_info'])

    def test_price_increase_on_final_check_prevents_purchase(self):
        self.config.mode='monitor_and_buy'; self.config.image_selection=IMAGE.copy()
        machine=dict(candidate());machine['details']['highest_cuda_version']='12.8'
        with patch('catalog.refresh_image'), patch('pricing.preview', side_effect=[
                {'hourly_equivalent':Decimal('2')},{'hourly_equivalent':Decimal('4')}]), patch('gpu.request_json') as order:
            result=gpu.buy(self.config,'RTX 5090',1,'','',candidates=[machine],gpu_info={})
        self.assertFalse(result['success']);order.assert_not_called()

    def test_atomic_submit_is_visible_before_network_and_success_survives_restart(self):
        self.prepare()
        payload={'instance_info':{'machine_id':'m','req_gpu_amount':1,'expand_data_disk':0}}
        def submit(*args, **kwargs):
            self.assertEqual(TaskStore(self.store.path).data['status'],'submitting')
            return {'code':'Success','data':'created'}
        with patch('cloning.list_instances',return_value=[{'uuid':'old'}]), patch('autodl_api._request_json',side_effect=submit):
            request_json(self.config,'order/instance/create/payg',payload,purchase=True)
        recovered=TaskStore(self.store.path)
        self.assertEqual(recovered.data['result']['instance_uuid'],'created')
        self.assertEqual(recovered.data['status'],'accepted')
        self.assertNotIn('secret-token',Path(self.store.path).read_text())
        with self.assertRaises(ValueError):recovered.start(task_plan(self.config))

    def test_timeout_or_interrupt_leaves_recoverable_uncertain_task_without_resubmit(self):
        for error in [PurchaseUncertainError('timeout'), KeyboardInterrupt()]:
            with self.subTest(error=type(error).__name__):
                self.store=TaskStore(self.config.state_dir/'task.json')
                if self.store.active:self.store.write(status='cancelled')
                self.prepare()
                payload={'instance_info':{'machine_id':'m','req_gpu_amount':1,'expand_data_disk':0}}
                with patch('cloning.list_instances',return_value=[]), patch('autodl_api._request_json',side_effect=error) as api:
                    with self.assertRaises(BaseException):request_json(self.config,'order/test',payload,purchase=True)
                    with self.assertRaises(ValueError):request_json(self.config,'order/test',payload,purchase=True)
                self.assertEqual(api.call_count,1)
                self.assertEqual(TaskStore(self.store.path).data['status'],'uncertain')

    def test_success_without_uuid_is_uncertain_and_not_retried(self):
        self.prepare()
        with patch('cloning.list_instances',return_value=[]), patch('autodl_api._request_json',return_value={'code':'Success','data':None}) as api:
            with self.assertRaises(PurchaseUncertainError):
                request_json(self.config,'order/test',{'instance_info':{'machine_id':'m','req_gpu_amount':1,'expand_data_disk':0}},purchase=True)
        api.assert_called_once();self.assertEqual(self.store.data['status'],'uncertain')

    def test_rejection_can_retry_but_success_can_never_submit_twice(self):
        self.prepare()
        payload={'instance_info':{'machine_id':'m','req_gpu_amount':1,'expand_data_disk':0}}
        with patch('cloning.list_instances',return_value=[]), patch('autodl_api._request_json',side_effect=[
                {'code':'NoStock'}, {'code':'Success','data':'new'}]) as api:
            request_json(self.config,'order/test',payload,purchase=True)
            self.assertEqual(self.store.data['status'],'waiting')
            request_json(self.config,'order/test',payload,purchase=True)
            with self.assertRaises(ValueError):request_json(self.config,'order/test',payload,purchase=True)
        self.assertEqual(api.call_count,2)

    def test_recovery_only_proposes_new_matching_instances_and_never_reorders(self):
        self.prepare();self.store.submit('order/test',{'instance_info':{'machine_id':'m','req_gpu_amount':1,'expand_data_disk':0}},['old'])
        matches=self.store.reconcile([{'uuid':'old','machine_id':'m','req_gpu_amount':1},
          {'uuid':'match','machine_id':'m','req_gpu_amount':1},{'uuid':'other','machine_id':'x','req_gpu_amount':1}])
        self.assertEqual([v['uuid'] for v in matches],['match'])
        with patch('interactive.ensure_login'),patch('cloning.list_instances',return_value=[]), \
                patch('interactive.yes',return_value=False),patch('gpu.buy') as order:
            interactive.resume_task(self.config,self.store)
        order.assert_not_called();self.assertEqual(self.store.data['status'],'submitting')

    def test_accepted_task_resume_only_tracks_and_never_revalidates_source(self):
        self.prepare();self.store.write(status='accepted',result={'instance_uuid':'created','machine_id':'m'})
        with patch('interactive.ensure_login'),patch('interactive.yes',return_value=True), \
                patch('interactive.track_existing') as track,patch('gpu.buy') as order:
            interactive.resume_task(self.config,self.store)
        track.assert_called_once();order.assert_not_called()

    def test_waiting_task_needs_confirmation_and_preserves_plan_on_decline(self):
        self.prepare();self.store.write(status='paused')
        with patch('interactive.ensure_login'),patch('catalog.refresh_image'),patch('interactive.yes',side_effect=[False,False]),patch('main.monitor') as monitor:
            interactive.resume_task(self.config,self.store)
        monitor.assert_not_called();self.assertEqual(self.store.data['status'],'paused')

    def test_ctrl_c_keeps_resumable_task_and_does_not_submit(self):
        self.prepare()
        with patch('main.monitor',side_effect=KeyboardInterrupt()),patch('interactive.yes',return_value=True):
            interactive.run_task(self.config,self.store)
        self.assertEqual(TaskStore(self.store.path).data['status'],'paused')

    def test_second_process_cannot_acquire_same_configuration_lock(self):
        with process_lock(self.config.state_dir):
            with self.assertRaises(ValueError):
                with process_lock(self.config.state_dir):pass

    def test_base_tree_and_private_images_normalize_without_exposing_private_fields(self):
        leaf={'uuid':'base-id','v':'12.4','cpu_arch':'x86','chip_corp':'nvidia'}
        tree=[{'label_name':'PyTorch','children':[{'label_name':'2.5','children':[{'label_name':'3.12','children':[{'label_name':leaf}]}]}]}]
        with patch('catalog.request_json',return_value={'data':tree}):
            self.assertEqual(catalog.list_images(self.config,'base','x86','nvidia')[0],IMAGE)
        with patch('catalog.request_json',return_value={'data':[{'image_uuid':'p','name':'my','status':'finished','cuda_v':'12.4','cpu_arch':'x86','chip_corp':'nvidia','user_phone':'secret-phone'}]}):
            rows=catalog.list_images(self.config,'private','x86','nvidia')
        self.assertNotIn('secret-phone',json.dumps(rows))

    def test_image_removed_between_confirmation_and_order_is_blocked(self):
        self.config.image_selection=IMAGE.copy()
        with patch('catalog.list_images',return_value=[]),self.assertRaises(ValueError):catalog.refresh_image(self.config)

    def test_completion_and_failed_notifications_cannot_reenter_purchase(self):
        self.prepare();self.store.write(status='accepted',result={'instance_uuid':'new','machine_id':'m'})
        with patch('instance_status.wait',return_value={'status':'completed','message':'ready'}), \
                patch('notifications.send_notification',side_effect=RuntimeError('notification offline')),patch('main.DesktopAlert'):
            self.assertEqual(main.finish_new(self.config,Mock(),{'instance_uuid':'new','region_name':'北京'}),0)
        self.assertEqual(TaskStore(self.store.path).data['status'],'completed')

    def test_new_mode_entire_menu_to_completed_task_only_submits_once(self):
        accepted = []
        def backend(config, path, payload=None, **kwargs):
            if path == 'instance':
                return {'code': 'Success', 'data': {'list': [{'uuid':'created','status':'running'}] if accepted else []}}
            if path == 'order/instance/create/payg':
                accepted.append(payload)
                self.assertEqual(TaskStore(self.config.state_dir/'task.json').data['status'],'submitting')
                return {'code':'Success','data':'created'}
            raise AssertionError(path)
        machine = candidate();machine['details']['highest_cuda_version']='12.8'
        inputs = ['2','1,2','1','1','1','1','1','1','1','y','50','3','y','6']
        with patch('main.Config',return_value=self.config),patch('sys.stdin.isatty',return_value=True), \
                patch('builtins.input',side_effect=inputs),patch('catalog.list_regions',return_value=[{'name':'北京B区'},{'name':'内蒙B区'}]), \
                patch('catalog.list_gpu_types',return_value=[GPU]),patch('catalog.list_images',return_value=[IMAGE]), \
                patch('watch.watch_gpu',return_value={"['bj-B1']":{'total_gpu_num':1,'idle_gpu_num':1}}), \
                patch('gpu.find_candidates',return_value=[machine]),patch('pricing.preview',return_value={'hourly_equivalent':Decimal('2')}), \
                patch('autodl_api._request_json',side_effect=backend),patch('main.DesktopAlert'),patch('notifications.send_notification'):
            self.assertEqual(main.main([]),0)
        self.assertEqual(len(accepted),1)
        self.assertEqual(accepted[0]['instance_info']['expand_data_disk'],50*pricing.GIB)
        saved=TaskStore(self.config.state_dir/'task.json').data
        self.assertEqual(saved['status'],'completed')
        self.assertEqual(saved['plan']['region_names'],['北京B区','内蒙B区'])

    def test_clone_mode_entire_menu_to_copy_completion_only_submits_once(self):
        accepted=[]
        def backend(config,path,payload=None,**kwargs):
            if path=='instance':
                items=[row()]
                if accepted:items.append({'uuid':'cloned','status':'running','additional_info':{
                    'copy_data_disk_after_clone':True,'copy_data_disk_after_clone_status':'Succeed'}})
                return {'code':'Success','data':{'list':items}}
            if path=='instance/snapshot':return {'code':'Success','data':source_snapshot()}
            if path=='order/instance/clone/payg':
                accepted.append(payload)
                return {'code':'Success','data':'cloned'}
            raise AssertionError(path)
        with patch('main.Config',return_value=self.config),patch('sys.stdin.isatty',return_value=True), \
                patch('builtins.input',side_effect=['3','1','3','y','6']),patch('interactive.ensure_login'), \
                patch('watch.watch_gpu',return_value={"['bj-B1']":{'total_gpu_num':1,'idle_gpu_num':1}}), \
                patch('gpu.find_candidates',return_value=[candidate()]),patch('cloning.preview_price',return_value={'hourly_equivalent':Decimal('2')}), \
                patch('autodl_api._request_json',side_effect=backend),patch('main.DesktopAlert'),patch('notifications.send_notification'):
            self.assertEqual(main.main([]),0)
        self.assertEqual(len(accepted),1)
        self.assertTrue(accepted[0]['instance_info']['copy_data_disk_after_clone'])
        self.assertEqual(TaskStore(self.config.state_dir/'task.json').data['status'],'completed')

    def test_monitor_menu_ctrl_c_returns_home_and_preserves_waiting_task(self):
        with patch('main.Config',return_value=self.config),patch('sys.stdin.isatty',return_value=True), \
                patch('interactive.ensure_login'),patch('builtins.input',side_effect=['1','1','1','1','y','y','6']), \
                patch('catalog.list_regions',return_value=[{'name':'北京B区'}]),patch('catalog.list_gpu_types',return_value=[GPU]), \
                patch('main.monitor',side_effect=KeyboardInterrupt()),patch('autodl_api._request_json') as request:
            self.assertEqual(main.main([]),0)
        request.assert_not_called()
        self.assertEqual(TaskStore(self.config.state_dir/'task.json').data['status'],'paused')

    def test_unrecognized_platform_rejection_stops_instead_of_blind_retry(self):
        self.prepare()
        with patch('cloning.list_instances',return_value=[]),patch('autodl_api._request_json',return_value={'code':'InternalError'}):
            with self.assertRaises(PurchaseUncertainError):
                request_json(self.config,'order/test',{'instance_info':{'machine_id':'m','req_gpu_amount':1,'expand_data_disk':0}},purchase=True)
        self.assertEqual(self.store.data['status'],'uncertain')

    def test_failure_to_record_submission_never_sends_network_order(self):
        self.prepare()
        with patch('cloning.list_instances',return_value=[]),patch('task_store.atomic_json',side_effect=OSError('disk full')), \
                patch('autodl_api._request_json') as api:
            with self.assertRaises(OSError):
                request_json(self.config,'order/test',{'instance_info':{'machine_id':'m','req_gpu_amount':1,'expand_data_disk':0}},purchase=True)
        api.assert_not_called()

    def test_post_order_disk_failure_keeps_durable_submission_barrier(self):
        self.prepare()
        def write(path,value):
            if value['status']!='submitting':raise OSError('disk full after request')
            atomic_json(path,value)
        with patch('cloning.list_instances',return_value=[]),patch('task_store.atomic_json',side_effect=write), \
                patch('autodl_api._request_json',return_value={'code':'Success','data':'created'}) as api:
            with self.assertRaises(OSError):
                request_json(self.config,'order/test',{'instance_info':{'machine_id':'m','req_gpu_amount':1,'expand_data_disk':0}},purchase=True)
        api.assert_called_once()
        self.assertEqual(TaskStore(self.store.path).data['status'],'submitting')

    def test_cancel_at_summary_returns_to_main_without_persisting_or_ordering(self):
        with patch('main.Config',return_value=self.config),patch('sys.stdin.isatty',return_value=True), \
                patch('interactive.ensure_login'),patch('builtins.input',side_effect=['1','1','1','1','n','6']), \
                patch('catalog.list_regions',return_value=[{'name':'北京B区'}]),patch('catalog.list_gpu_types',return_value=[GPU]), \
                patch('main.monitor') as monitor:
            self.assertEqual(main.main([]),0)
        monitor.assert_not_called()
        self.assertFalse(Path(self.store.path).exists())

    def test_expired_authorization_uses_official_response_code(self):
        with patch('requests.post',return_value=Mock(status_code=200,json=lambda:{'code':'AuthorizeFailed','msg':'expired'})):
            with self.assertRaises(AuthenticationError):request_json(self.config,'image/all',{})

    def test_changed_clone_source_cannot_resume_old_purchase_authorization(self):
        import cloning
        self.config.mode='monitor_and_clone';self.config.clone_max_price=3
        cloning.prepare_config(self.config,source());self.config.clone_source_uuid=source().uuid
        self.store.start(task_plan(self.config));self.store.write(status='paused')
        changed=__import__('dataclasses').replace(source(),data_disk_bytes=200*pricing.GIB)
        with patch('interactive.ensure_login'),patch('cloning.choose_source',return_value=changed),patch('main.monitor') as monitor:
            with self.assertRaises(ValueError):interactive.resume_task(self.config,self.store)
        monitor.assert_not_called()

    def test_base_image_back_returns_to_previous_environment_step(self):
        with patch('builtins.input',side_effect=['1','1','b','1','1','1']):
            selected=interactive.choose_base_image([IMAGE],{})
        self.assertEqual(selected,IMAGE)

    def test_unfinished_private_image_is_not_offered(self):
        rows=[{'image_uuid':'unfinished','status':'building','name':'test','cuda_v':'12.4','cpu_arch':'x86','chip_corp':'nvidia'}]
        with patch('catalog.request_json',return_value={'data':rows}):
            self.assertEqual(catalog.list_images(self.config,'private','x86','nvidia'),[])


if __name__=='__main__':unittest.main()
