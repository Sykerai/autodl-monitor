"""读取可选源实例、匹配相同配置，并通过官网克隆接口创建一次实例。"""

from dataclasses import dataclass, field
from decimal import Decimal
import math
import time

from autodl_api import APIError, AuthenticationError, PurchaseUncertainError, request_json

GIB = 1024 ** 3


def list_instances(config):
    rows, seen, page = [], set(), 1
    while True:
        body = request_json(config, 'instance', {'page_index': page, 'page_size': 50})
        data = body['data']
        if not isinstance(data, dict) or not isinstance(data.get('list'), list):
            raise APIError('源实例列表格式异常')
        items = data['list']
        new_rows = [row for row in items if isinstance(row, dict)
                    and isinstance(row.get('uuid'), str) and row['uuid'] not in seen]
        rows.extend(new_rows)
        seen.update(row['uuid'] for row in new_rows)
        if len(items) < 50 or not new_rows:
            return rows
        page += 1


def show_instances(rows):
    print('序号 | 实例ID | 所在机器 | GPU配置 | 状态', flush=True)
    for number, row in enumerate(rows, 1):
        print(f"{number} | {row['uuid']} | {row.get('region_name', '')} / "
              f"{row.get('machine_alias', '')} | {row.get('snapshot_gpu_alias_name', '')} × "
              f"{row.get('req_gpu_amount', '?')} | {row.get('status', '未知')}", flush=True)


def choose_source(config, instance_uuid='', *, interactive=True):
    rows = list_instances(config)
    if not rows:
        raise ValueError('当前账号没有可供克隆的容器实例')
    if not instance_uuid:
        show_instances(rows)
        if not interactive:
            raise ValueError('请用 --source 实例ID 指定源实例；交互运行时可直接输入序号')
        try:
            answer = input('请选择源实例序号（q 取消）：').strip()
        except EOFError as exc:
            raise ValueError('未选择源实例，未启动克隆') from exc
        if answer.lower() == 'q':
            raise ValueError('已取消选择，未启动克隆')
        if not answer.isdigit() or not 1 <= int(answer) <= len(rows):
            raise ValueError('源实例序号无效，请重新运行选择')
        instance_uuid = rows[int(answer) - 1]['uuid']
    row = next((row for row in rows if row['uuid'] == instance_uuid), None)
    if row is None:
        raise ValueError('指定源实例不在当前账号的实例列表中，请重新选择')
    return load_source(config, row)


def positive_number(value, name, *, allow_zero=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
        raise APIError(f'源实例的{name}无效，无法保证相同配置')
    return value


@dataclass(frozen=True)
class CloneSource:
    uuid: str
    name: str
    machine_id: str
    region_name: str
    region_sign: str
    gpu_model: str
    gpu_num: int
    gpu_memory: int
    cpu_cores: float
    memory_bytes: int
    data_disk_bytes: int
    system_disk_bytes: int
    cpu_arch: str
    chip_corp: str
    cuda_version: str
    image_fields: dict = field(repr=False)

    def signature(self):
        return (self.uuid, self.gpu_model, self.gpu_num, self.gpu_memory,
                self.cpu_cores, self.memory_bytes, self.data_disk_bytes,
                self.system_disk_bytes, self.cpu_arch, self.chip_corp,
                self.cuda_version, self.image_fields)


def load_source(config, row):
    if row.get('status') != 'shutdown':
        raise ValueError('源实例必须处于“已关机”状态；程序不会自动关机，请先在官网处理')
    if row.get('charge_type') != 'payg':
        raise ValueError('当前自动克隆仅支持按量计费源实例')
    snapshot = request_json(config, 'instance/snapshot',
                            {'instance_uuid': row['uuid']}, method='GET')['data']
    if not isinstance(snapshot, dict):
        raise APIError('源实例配置快照格式异常')
    image, machine = snapshot.get('image'), snapshot.get('machine_info_snapshot')
    if not isinstance(image, dict) or not isinstance(machine, dict):
        raise APIError('源实例缺少镜像或硬件配置快照')
    gpu_type = machine.get('gpu_type', {})
    if not isinstance(gpu_type, dict):
        raise APIError('源实例 GPU 规格快照格式异常')
    gpu_model = row.get('snapshot_gpu_alias_name')
    gpu_num = row.get('req_gpu_amount')
    if not isinstance(gpu_model, str) or not gpu_model or type(gpu_num) is not int or not 1 <= gpu_num <= 8:
        raise APIError('源实例 GPU 型号或卡数无效')
    system_bytes = positive_number(row.get('root_fs_total_size'), '系统盘容量')
    if snapshot.get('expand_system_disk_size', 0) != 0 or system_bytes != 30 * GIB:
        raise ValueError('该源实例使用非标准系统盘，暂无法保证相同配置，已停止克隆')
    base_disk = positive_number(machine.get('max_instance_disk_size'), '基础数据盘容量')
    expanded = positive_number(snapshot.get('expand_data_disk_size'), '数据盘扩容', allow_zero=True)
    fields = {
        'base_image_uuid': image.get('base_image_uuid') or image.get('image_name', '')
                           if image.get('image_type') == 'public' else '',
        'private_image_uuid': row.get('private_image_uuid', ''),
        'reproduction_uuid': row.get('reproduction_uuid', ''),
        'reproduction_id': row.get('reproduction_id', 0),
        'cg_application_uuid': snapshot.get('cg_application_uuid', ''),
        'cg_application_info': snapshot.get('cg_application_info') or {},
    }
    arch = snapshot.get('cpu_arch') or image.get('cpu_arch')
    corp = snapshot.get('chip_corp') or image.get('chip_corp')
    if not arch or not corp or not any(fields[key] for key in (
        'base_image_uuid', 'private_image_uuid', 'reproduction_uuid', 'cg_application_uuid',
    )):
        raise APIError('源实例镜像或架构信息不完整')
    return CloneSource(
        uuid=row['uuid'], name=row.get('name') or row.get('machine_alias') or row['uuid'],
        machine_id=row['machine_id'], region_name=row['region_name'], region_sign=row['region_sign'],
        gpu_model=gpu_model, gpu_num=gpu_num,
        gpu_memory=positive_number(gpu_type.get('memory'), '显存'),
        cpu_cores=positive_number(row.get('cpu_limit'), 'CPU 核数'),
        memory_bytes=positive_number(row.get('mem_limit_in_byte'), '内存'),
        data_disk_bytes=base_disk + expanded, system_disk_bytes=system_bytes,
        cpu_arch=arch, chip_corp=corp, cuda_version=image.get('cuda_version', ''),
        image_fields=fields,
    )


def prepare_config(config, source):
    config.clone_source = source
    config.gpu_model, config.gpu_num = source.gpu_model, source.gpu_num
    config.region_names, config.region_sign_list = [source.region_name], []


def describe_source(source):
    return (f'源实例：{source.name}（{source.uuid}）\n地区：{source.region_name}\n'
            f'GPU：{source.gpu_model} × {source.gpu_num}，每卡显存 {source.gpu_memory / GIB:g} GB\n'
            f'CPU：{source.cpu_cores:g} 核；内存：{source.memory_bytes / GIB:g} GB\n'
            f'系统盘：{source.system_disk_bytes / GIB:g} GB；数据盘：{source.data_disk_bytes / GIB:g} GB\n'
            '复制范围：系统盘和数据盘；保留原实例。')


def cuda_version(value):
    try:
        parts = tuple(int(part) for part in str(value).split('.'))
        return (parts + (0, 0))[:2]
    except ValueError:
        return None


def matching_candidates(source, candidates):
    result = []
    for candidate in candidates:
        info = candidate.get('details', {})
        if type(info.get('cpu_per_gpu')) not in (int, float) or type(info.get('mem_per_gpu')) not in (int, float):
            continue
        if (info.get('gpu_name') != source.gpu_model
                or info.get('gpu_memory') != source.gpu_memory
                or info.get('cpu_arch') != source.cpu_arch
                or info.get('chip_corp') != source.chip_corp
                or info.get('cpu_per_gpu', 0) * source.gpu_num != source.cpu_cores
                or info.get('mem_per_gpu', 0) * source.gpu_num != source.memory_bytes):
            continue
        base, maximum = info.get('max_instance_disk_size'), info.get('max_data_disk_expand_size')
        if type(base) is not int or type(maximum) is not int:
            continue
        expansion = source.data_disk_bytes - base
        if expansion < 0 or expansion > maximum or expansion % GIB:
            continue
        if source.cuda_version:
            current, required = cuda_version(info.get('highest_cuda_version')), cuda_version(source.cuda_version)
            if current is None or required is None or current < required:
                continue
        result.append({**candidate, 'expand_data_disk': expansion})
    return result


def preview_price(config, source, candidate):
    data = request_json(config, 'order/price/preview', {
        'machine_id': candidate['machine_id'], 'charge_type': 'payg',
        'num': source.gpu_num, 'expand_data_disk': candidate['expand_data_disk'],
    })['data']
    if not isinstance(data, dict):
        raise APIError('克隆价格预览格式异常')
    hourly = data.get('payg_price')
    disk = data.get('data_disk_info') or {}
    daily = disk.get('data_disk_deal_daily_price')
    if type(hourly) not in (int, float) or not math.isfinite(hourly) or hourly < 0:
        raise APIError('克隆价格预览缺少有效的 GPU 费用')
    if candidate['expand_data_disk'] and (
        type(daily) not in (int, float) or not math.isfinite(daily) or daily < 0
    ):
        raise APIError('克隆价格预览缺少有效的数据盘费用')
    hourly = Decimal(str(hourly)) / 1000
    daily = Decimal(str(daily or 0)) / 1000
    # 与单卡机器原始标价不同，价格预览已包含卡数和账号折扣。
    return {'gpu_hourly': hourly, 'disk_daily': daily,
            'hourly_equivalent': hourly + daily / 24}


def within_budget(config, source, candidates):
    if config.clone_max_price is None:
        return candidates
    maximum = Decimal(str(config.clone_max_price))
    return [candidate for candidate in candidates
            if preview_price(config, source, candidate)['hourly_equivalent'] <= maximum]


def clone_once(config, source, candidates):
    if config.clone_max_price is None:
        raise ValueError('未设置克隆总费用上限，不能自动创建实例')
    current = choose_source(config, source.uuid, interactive=False)
    if current.signature() != source.signature():
        raise ValueError('源实例配置已变化，请重新启动并选择源实例')
    for candidate in matching_candidates(source, candidates):
        price = preview_price(config, source, candidate)
        if price['hourly_equivalent'] > Decimal(str(config.clone_max_price)):
            continue
        body = request_json(config, 'order/instance/clone/payg', {
            'instance_uuid': source.uuid,
            'instance_info': {
                **source.image_fields,
                'machine_id': candidate['machine_id'], 'charge_type': 'payg',
                'req_gpu_amount': source.gpu_num, 'instance_name': '',
                'expand_data_disk': candidate['expand_data_disk'],
                'copy_data_disk_after_clone': True,
                'keep_src_user_service_address_after_clone': False, 'ft_sparse': False,
            },
            'price_info': {
                'coupon_id_list': [], 'machine_id': candidate['machine_id'],
                'charge_type': 'payg', 'duration': 1, 'num': source.gpu_num,
                'expand_data_disk': candidate['expand_data_disk'],
            },
        }, purchase=True)
        if body['code'] == 'Success':
            instance_uuid = body.get('data')
            if not isinstance(instance_uuid, str) or not instance_uuid:
                raise PurchaseUncertainError('克隆请求已返回成功，但新实例ID缺失；请到官网核查，不要重复创建')
            return {'success': True, 'message': '克隆请求已受理，数据复制完成状态请到官网查看',
                    'instance_uuid': instance_uuid, 'machine_id': candidate['machine_id'],
                    'region_name': source.region_name, 'gpu_info': {}}
    return {'success': False, 'message': '暂未找到价格和配置均满足条件的克隆目标'}


def completion_status(row):
    """请求受理、系统盘克隆和数据盘复制是不同阶段，不能仅凭实例存在判成功。"""
    info = row.get('additional_info') or {}
    status = row.get('status')
    copy_status = info.get('copy_data_disk_after_clone_status')
    if copy_status == 'Failed' or status in ('create_failed', 'pull_failed', 'clone_failed'):
        return 'failed'
    if status in ('running', 'shutdown') and copy_status == 'Succeed' and info.get('copy_data_disk_after_clone') is True:
        return 'completed'
    return 'pending'


def wait_for_completion(config, instance_uuid, *, report=lambda message: None):
    """只读取已创建实例的状态；任何失败都不再次发起克隆。"""
    deadline = time.monotonic() + getattr(config, 'clone_completion_timeout', 3600)
    interval = getattr(config, 'clone_status_interval', 10)
    last_message = None
    try:
        while time.monotonic() < deadline:
            try:
                row = next((row for row in list_instances(config) if row['uuid'] == instance_uuid), None)
                status = completion_status(row) if row else 'pending'
                if status == 'completed':
                    return {'status': status, 'message': '系统盘克隆和数据盘复制均已完成'}
                if status == 'failed':
                    return {'status': status, 'message': '平台报告克隆或数据盘复制失败，请到官网查看详情；不会重新下单'}
                message = '新实例正在创建或复制数据，等待完成...' if row else '克隆请求已受理，等待新实例出现在列表中...'
            except AuthenticationError:
                return {'status': 'pending', 'message': '登录已失效，无法确认克隆进度；请到官网检查新实例，不会重新下单'}
            except APIError:
                message = '暂时无法读取克隆进度，稍后继续查询；不会重新下单'
            if message != last_message:
                report(message)
                last_message = message
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(interval, remaining))
    except KeyboardInterrupt:
        return {'status': 'pending', 'message': '已停止本机进度跟踪，平台上的克隆仍可能继续；请到官网检查，不要重复创建'}
    return {'status': 'pending', 'message': '已达到本机进度跟踪时限，尚未确认复制完成；请到官网查看，不会重新下单'}
