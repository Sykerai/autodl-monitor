from autodl_api import APIError, request_json
from regions import REGION_NAMES, region_codes, region_name
from watch import watch_gpu
from decimal import Decimal
import pricing


region2name = {str(list(codes)): name for codes, name in REGION_NAMES.items()}


def get_machine_info(config, gpu, region, gpu_num, *, diagnostics=None):
    machines = []
    counts = diagnostics if diagnostics is not None else {}
    for key in ('examined', 'disabled', 'capacity_full', 'insufficient_idle', 'eligible'):
        counts.setdefault(key, 0)
    seen = set()
    page = 1
    page_size = 90  # 官网请求封装也支持 90；减少逐页查询时错过短暂空闲的窗口。
    while True:
        response = request_json(config, 'user/machine/list', {
            'charge_type': 'payg', 'default_order': True,
            'gpu_idle_num': gpu_num, 'gpu_type_name': [gpu],
            'page_index': page, 'page_size': page_size,
            'region_sign_list': region_codes(region),
        })
        data = response['data']
        if not isinstance(data, dict) or not isinstance(data.get('list'), list):
            raise APIError('机器列表响应格式异常')
        items = data['list']
        new_ids = set()
        for item in items:
            if not isinstance(item, dict) or 'machine_id' not in item:
                raise APIError('机器信息响应格式异常')
            machine_id = item['machine_id']
            if machine_id in seen or machine_id in new_ids:
                continue
            new_ids.add(machine_id)
            counts['examined'] += 1
            # 与官网“租用”按钮的可用条件保持一致：部分不可租用机器
            # 仍返回正的 gpu_idle_num，单看空闲卡数会产生误报。
            status = item.get('on_line')
            maximum, occupied = item.get('max_instance_num'), item.get('binding_instance_num')
            if type(status) is not int or type(maximum) is not int or type(occupied) is not int:
                raise APIError('机器可租状态或实例容量缺失')
            if status in (3, 4, 5, 6):
                counts['disabled'] += 1
                continue
            if maximum <= occupied:
                counts['capacity_full'] += 1
                continue
            idle, price = item.get('gpu_idle_num'), item.get('payg_price')
            if type(idle) is not int or type(price) not in (int, float) or price < 0:
                raise APIError('机器库存或价格无效')
            if idle >= gpu_num:
                counts['eligible'] += 1
                machines.append({'machine_id': machine_id, 'machine_price': price,
                                 'region': region, 'gpu_idle_num': idle,
                                 'details': {key: item.get(key) for key in (
                                     'gpu_name', 'gpu_memory', 'cpu_arch', 'chip_corp',
                                     'cpu_per_gpu', 'mem_per_gpu', 'max_instance_disk_size',
                                     'max_data_disk_expand_size', 'highest_cuda_version',
                                     'driver_version',
                                 )}})
            else:
                counts['insufficient_idle'] += 1
        seen.update(new_ids)
        effective_size = data.get('page_size', page_size)
        if type(effective_size) is not int or effective_size <= 0:
            raise APIError('机器列表分页大小无效')
        if len(items) < effective_size or not new_ids:
            break
        page += 1
    return machines


def find_candidates(config, gpu_info):
    candidates = []
    config.machine_scan = {}
    # 汇总与明细更新可能不同步：汇总为 0 也不能跳过真实机器检查。
    for region, info in gpu_info.items():
        machines = get_machine_info(config, config.gpu_model, region, config.gpu_num,
                                    diagnostics=config.machine_scan)
        for machine in machines:
            machine['region_name'] = info.get('region_name', region_name(region))
        candidates.extend(machines)
    return candidates


def compatible_candidates(config, candidates):
    selection = getattr(config, 'image_selection', None)
    expansion = getattr(config, 'expand_data_disk_gib', 0) * pricing.GIB
    result = []
    from cloning import cuda_version
    for machine in candidates:
        details = machine.get('details', {})
        if details.get('gpu_name') != config.gpu_model:
            continue
        maximum = details.get('max_data_disk_expand_size')
        if expansion and (type(maximum) is not int or maximum < expansion):
            continue
        if selection:
            if any(details.get(key) != selection[key] for key in ('cpu_arch', 'chip_corp')):
                continue
            required = selection.get('cuda_version')
            if required:
                current, minimum = cuda_version(details.get('highest_cuda_version')), cuda_version(required)
                if current is None or minimum is None or current < minimum:
                    continue
        result.append(dict(machine, expand_data_disk=expansion))
    return result


def buy(config, gpu, gpu_num, image, private_image_uuid, *, gpu_info=None, candidates=None):
    snapshot = watch_gpu(config, gpu) if gpu_info is None else gpu_info
    if candidates is None:
        candidates = find_candidates(config, snapshot)
    result = {'success': False, 'message': '当前没有满足条件的可租机器',
              'gpu_info': snapshot}
    from catalog import refresh_image
    refresh_image(config)
    maximum = getattr(config, 'max_price_per_hour', None)
    candidates = pricing.ranked(config, compatible_candidates(config, candidates), gpu_num,
                                maximum=maximum)
    selection = getattr(config, 'image_selection', None)
    if selection:
        image_fields = {('base_image_uuid' if selection['kind'] == 'base' else 'private_image_uuid'): selection['uuid']}
    elif private_image_uuid:
        image_fields = {'private_image_uuid': private_image_uuid}
    elif image:
        image_fields = {'image': image}  # Legacy command/config compatibility only.
    else:
        raise ValueError('请先在新建菜单中选择镜像')
    for machine in candidates:
        machine_id = machine['machine_id']
        expansion = machine['expand_data_disk']
        price = pricing.preview(config, machine, gpu_num, expansion)
        if price['hourly_equivalent'] > Decimal(str(maximum)):
            continue
        response = request_json(config, 'order/instance/create/payg', {
            'instance_info': {
                **image_fields,
                'charge_type': 'payg', 'expand_data_disk': expansion,
                'machine_id': machine_id, 'req_gpu_amount': gpu_num,
            },
            'price_info': {'charge_type': 'payg', 'expand_data_disk': expansion,
                           'duration': 1, 'coupon_id_list': [],
                           'machine_id': machine_id, 'num': gpu_num},
        }, purchase=True)
        if response['code'] == 'Success':
            from autodl_api import PurchaseUncertainError
            instance_uuid = response.get('data')
            if not isinstance(instance_uuid, str) or not instance_uuid.strip():
                raise PurchaseUncertainError('新建请求已返回成功，但实例ID缺失；请核实平台，不能重复创建')
            result.update(success=True, machine_id=machine_id,
                          instance_uuid=instance_uuid,
                          region=machine['region'],
                          region_name=machine.get('region_name', region_name(machine['region'])),
                          price=float(price['hourly_equivalent']),
                          message=f'已创建包含 {gpu_num} 张 {gpu} 的实例')
            return result
    return result
