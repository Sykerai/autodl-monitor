"""Durable purchase boundary: an interrupted submission is never submitted twice."""
import copy
from datetime import datetime, timezone
from uuid import uuid4

from local_state import atomic_json, read_json

ACTIVE = {'waiting', 'paused', 'submitting', 'uncertain', 'accepted', 'tracking'}
PLAN_FIELDS = ('mode', 'region_names', 'region_sign_list', 'gpu_model', 'gpu_num',
               'image_selection', 'image', 'private_image_uuid', 'expand_data_disk_gib',
               'max_price_per_hour', 'clone_max_price', 'clone_source_uuid', 'interval', 'retry_interval')


def task_plan(config):
    result = {name: copy.deepcopy(getattr(config, name)) for name in PLAN_FIELDS if hasattr(config, name)}
    source = getattr(config, 'clone_source', None)
    if source is not None:
        # A normalized JSON signature survives restarts, without account credentials.
        import json
        result['source_signature'] = json.loads(json.dumps(source.signature()))
    return result


class TaskStore:
    def __init__(self, path):
        self.path = path
        self.data = read_json(path)
        if self.data is not None and (not isinstance(self.data, dict) or self.data.get('schema') != 1
                                      or self.data.get('status') not in ACTIVE | {'completed', 'failed', 'cancelled'}):
            raise ValueError('任务记录格式无效；请保留记录并先核查平台上的实例')
        if self.data is not None:
            self.validate_plan(self.data.get('plan'))
            if self.data['status'] in ('accepted', 'tracking'):
                result = self.data.get('result')
                if not isinstance(result, dict) or not isinstance(result.get('instance_uuid'), str) or not result['instance_uuid']:
                    raise ValueError('已受理任务缺少实例ID，请保留记录并到官网核实')
            if self.data['status'] in ('submitting', 'uncertain'):
                attempt = self.data.get('attempt')
                if (not isinstance(attempt, dict) or not isinstance(attempt.get('previous_instances'), list)
                        or not attempt.get('machine_id') or not isinstance(attempt.get('submitted_at'), str)):
                    raise ValueError('未确认订单缺少提交记录，请保留记录并到官网核实')

    @staticmethod
    def validate_plan(plan):
        import math
        if not isinstance(plan, dict) or plan.get('mode') not in ('monitor_only', 'monitor_and_buy', 'monitor_and_clone'):
            raise ValueError('保存的任务模式无效')
        if (type(plan.get('gpu_num')) is not int or not 1 <= plan['gpu_num'] <= 8
                or not isinstance(plan.get('gpu_model'), str) or not plan['gpu_model'].strip()):
            raise ValueError('保存的 GPU 任务参数无效')
        for key in ('region_names', 'region_sign_list'):
            if not isinstance(plan.get(key, []), list) or any(not isinstance(v, str) or not v for v in plan.get(key, [])):
                raise ValueError('保存的任务地区无效')
        for key in ('interval', 'retry_interval'):
            value = plan.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('保存的检查间隔无效')
        if plan['mode'] != 'monitor_only':
            key = 'clone_max_price' if plan['mode'] == 'monitor_and_clone' else 'max_price_per_hour'
            value = plan.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('保存的费用上限无效')
        if plan['mode'] == 'monitor_and_buy':
            expansion = plan.get('expand_data_disk_gib', 0)
            if type(expansion) is not int or not 0 <= expansion <= 100000:
                raise ValueError('保存的数据盘扩容无效')
            selection = plan.get('image_selection')
            if selection and (not isinstance(selection, dict) or selection.get('kind') not in ('base', 'private')
                              or any(not isinstance(selection.get(k), str) for k in ('uuid', 'name', 'cpu_arch', 'chip_corp', 'cuda_version'))):
                raise ValueError('保存的镜像选择无效')

    @property
    def active(self):
        return bool(self.data and self.data['status'] in ACTIVE)

    def write(self, **changes):
        value = copy.deepcopy(self.data)
        value.update(changes, updated_at=datetime.now(timezone.utc).isoformat())
        atomic_json(self.path, value)
        self.data = value

    def start(self, plan):
        if self.active:
            raise ValueError('还有未完成任务，请先恢复或结束该任务')
        self.validate_plan(plan)
        self.data = {'schema': 1, 'id': str(uuid4()), 'status': 'waiting',
                     'plan': plan, 'attempt': None, 'result': None, 'checks': 0,
                     'created_at': datetime.now(timezone.utc).isoformat()}
        self.write()

    def submit(self, path, payload, previous_instances):
        if not self.data or self.data['status'] != 'waiting':
            raise ValueError('任务当前不能下单；请先核实上一次创建结果')
        info = payload['instance_info']
        self.write(status='submitting', attempt={
            'path': path, 'machine_id': info['machine_id'],
            'gpu_num': info['req_gpu_amount'], 'expand_data_disk': info['expand_data_disk'],
            'previous_instances': list(previous_instances),
            'submitted_at': datetime.now(timezone.utc).isoformat(),
        })

    def reconcile(self, rows):
        """Report new matching instances for user review, never assume absence means failure."""
        attempt = self.data.get('attempt') or {}
        previous = set(attempt.get('previous_instances', []))
        return [row for row in rows if row.get('uuid') not in previous
                and row.get('machine_id') == attempt.get('machine_id')
                and row.get('req_gpu_amount') == attempt.get('gpu_num')]
