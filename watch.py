from autodl_api import APIError, request_json
from regions import region_name, selected_region_groups

gpu2region = {
    'RTX 5090': [["west-B", "west-C"]],
    'RTX 4090': [['nm-B1', 'nm-B2'], ["cq-A1"], ["west-B", "west-C"], ["bj-B1"]],
    'RTX 4090D': [["cq-A1"], ["west-B", "west-C"]],
    'RTX 3090': [['nm-B1', 'nm-B2'], ["cq-A1"], ["west-B", "west-C"], ["beijing-A", "beijing-B", "beijing-D", "beijing-E"], ["yz-A1"]],
    'RTX 3080*2': [['nm-B1', 'nm-B2'], ["west-B", "west-C"]],
    'RTX 3080 Ti': [["west-B", "west-C"]],
    'RTX 3080': [["beijing-A", "beijing-B", "beijing-D", "beijing-E"]],
    'RTX 3060': [["west-B", "west-C"]],
    'RTX 2080 Ti *2': [['nm-B1', 'nm-B2'], ["cq-A1"]],
    'RTX 2080 Ti': [["beijing-A", "beijing-B", "beijing-D", "beijing-E"]],
    'GTX 1080 Ti': [["west-B", "west-C"]],
    'vGPU-32GB': [['nm-B1', 'nm-B2'], ["west-B", "west-C"]],
    'vGPU-48GB': [["west-B", "west-C"]],
    'V100-32GB': [["foshan-A"], ["beijing-C"]],
    'V100-SXM2-32GB': [["beijing-A", "beijing-B", "beijing-D", "beijing-E"]],
    'A100-PCIE-40GB': [["foshan-A"]],
    'A100-SXM4-80GB': [["neimeng-A", "neimeng-C", "neimeng-D"]],
    'L20': [["bj-C1"]],
    'L40': [["west-B", "west-C"]],
    'A40': [["neimeng-A", "neimeng-C", "neimeng-D"]],
    'H800': [["west-B", "west-C"]],
    'A800-80GB': [["nm-A1"]],
    'H20-NVLink': [["beijing-A", "beijing-B", "beijing-D", "beijing-E"], ["bj-C1"]],
    'TITAN Xp': [['nm-B1', 'nm-B2']],
    'Tesla T4': [["foshan-A"]],
    'RTX A4000': [["west-B", "west-C"]],
    'CPU': [["cq-A1"], ["west-B", "west-C"]],
    'CPU-close-HT': [["west-B", "west-C"]],
    '910B2x鲲鹏920': [["gd-A1"]]
}


def watch_gpu(config, gpu):
    groups = selected_region_groups(config)
    if not groups:
        groups = [(codes, region_name(codes)) for codes in gpu2region.get(gpu, [])]
    if not groups:
        raise ValueError('该型号没有预设地区，请在配置中指定 region_sign_list')
    gpu_info = {}
    for region, name in groups:
        response = request_json(config, 'machine/region/gpu_type', {'region_sign_list': region})
        if not isinstance(response['data'], list):
            raise APIError('GPU 库存响应格式异常')
        info = {'gpu': gpu, 'region_name': name, 'total_gpu_num': 0,
                'idle_gpu_num': 0, 'listed': False}
        for entry in response['data']:
            if isinstance(entry, dict) and gpu in entry:
                stats = entry[gpu]
                if not isinstance(stats, dict):
                    raise APIError('GPU 库存详情格式异常')
                for key in ('total_gpu_num', 'idle_gpu_num'):
                    value = stats.get(key)
                    if type(value) is not int or value < 0:
                        raise APIError('GPU 库存数量无效')
                info.update(stats, listed=True)
                break
        gpu_info[str(region)] = info
    return gpu_info


def available_regions(gpu_info, gpu_num=1):
    """地区库存只是初筛，多卡需求还需查询单台机器是否满足。"""
    return {region: info for region, info in gpu_info.items()
            if info.get('idle_gpu_num', 0) >= gpu_num}


