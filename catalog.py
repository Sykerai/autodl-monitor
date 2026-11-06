"""Read-only AutoDL catalogs and normalized, architecture-specific image choices."""
from autodl_api import APIError, request_json
from regions import selected_region_groups


def list_regions(config):
    rows = request_json(config, 'region/list', method='GET')['data']
    if not isinstance(rows, list):
        raise APIError('地区目录格式异常')
    groups = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('visible') is False or row.get('visible') == 0:
            continue
        name, code = row.get('region_name'), row.get('region_sign')
        if isinstance(name, str) and name and isinstance(code, str) and code:
            groups.setdefault(name, [])
            if code not in groups[name]:
                groups[name].append(code)
    if not groups:
        raise APIError('当前没有可选择的地区')
    return [{'name': name, 'codes': codes} for name, codes in groups.items()]


def list_gpu_types(config):
    """Use the website's regional catalog, retaining models with zero idle cards."""
    groups = selected_region_groups(config)
    if not groups:
        raise ValueError('请先选择地区，再选择 GPU 型号')
    models = {}
    for codes, region in groups:
        rows = request_json(config, 'machine/region/gpu_type', {'region_sign_list': codes})['data']
        if not isinstance(rows, list):
            raise APIError('地区 GPU 型号目录格式异常')
        for entry in rows:
            if not isinstance(entry, dict):
                raise APIError('地区 GPU 型号条目格式异常')
            for name, stats in entry.items():
                if name in ('CPU', 'CPU-close-HT'):
                    continue
                if not isinstance(name, str) or not name or not isinstance(stats, dict):
                    raise APIError('地区 GPU 型号条目格式异常')
                if any(not isinstance(stats.get(key), str) or not stats[key] for key in ('cpu_arch', 'chip_corp')):
                    raise APIError('地区 GPU 型号缺少架构或芯片信息')
                if any(type(stats.get(key)) is not int or stats[key] < 0 for key in ('total_gpu_num', 'idle_gpu_num')):
                    raise APIError('地区 GPU 库存数量无效')
                model = models.setdefault(name, {
                    'gpu_name': name, 'cpu_arch': stats['cpu_arch'], 'chip_corp': stats['chip_corp'],
                    'total_gpu_num': 0, 'idle_gpu_num': 0, 'regions': [],
                })
                if any(model[key] != stats[key] for key in ('cpu_arch', 'chip_corp')):
                    raise APIError('同名 GPU 在所选地区的架构信息不一致，请分地区选择')
                model['total_gpu_num'] += stats['total_gpu_num']
                model['idle_gpu_num'] += stats['idle_gpu_num']
                model['regions'].append({'name': region, 'codes': list(codes),
                                         'total_gpu_num': stats['total_gpu_num'],
                                         'idle_gpu_num': stats['idle_gpu_num']})
    if not models:
        raise APIError('所选地区当前没有列出 GPU 型号，请返回更换地区')
    return list(models.values())


def list_images(config, kind, cpu_arch, chip_corp):
    if kind not in ('base', 'private'):
        raise ValueError('未知镜像来源')
    payload = {'cpu_arch': [cpu_arch], 'chip_corp': [chip_corp]}
    path = 'image/all' if kind == 'base' else 'image/private/get'
    rows = request_json(config, path, payload)['data']
    if not isinstance(rows, list):
        raise APIError('镜像列表格式异常')
    result = []
    if kind == 'base':
        def visit(nodes, trail):
            for node in nodes:
                if not isinstance(node, dict):
                    raise APIError('基础镜像目录格式异常')
                label = node.get('label_name')
                if isinstance(label, str):
                    visit(node.get('children') or [], trail + [label])
                elif isinstance(label, dict) and label.get('uuid'):
                    version = str(label.get('v') or '')
                    result.append({'kind': 'base', 'uuid': label['uuid'],
                                   'name': ' / '.join(trail + [version or '无 CUDA']),
                                   'cuda_version': version if chip_corp == 'nvidia' else '',
                                   'cpu_arch': label.get('cpu_arch'), 'chip_corp': label.get('chip_corp')})
        visit(rows, [])
    else:
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('image_uuid'), str):
                raise APIError('私有镜像条目格式异常')
            if row.get('status') != 'finished' or row.get('deleted_at'):
                continue
            result.append({'kind': 'private', 'uuid': row['image_uuid'], 'name': str(row.get('name') or '未命名镜像'),
                           'cuda_version': str(row.get('cuda_v') or '') if chip_corp == 'nvidia' else '',
                           'cpu_arch': row.get('cpu_arch'), 'chip_corp': row.get('chip_corp')})
    return [row for row in result if row['cpu_arch'] == cpu_arch and row['chip_corp'] == chip_corp]


def refresh_image(config):
    saved = getattr(config, 'image_selection', None)
    if not saved:
        return
    rows = list_images(config, saved['kind'], saved['cpu_arch'], saved['chip_corp'])
    current = next((row for row in rows if row['uuid'] == saved['uuid']), None)
    if current is None or any(current[key] != saved[key] for key in ('cuda_version', 'cpu_arch', 'chip_corp')):
        raise ValueError('所选镜像已删除或环境发生变化，请重新选择镜像')
    config.image_selection = current
