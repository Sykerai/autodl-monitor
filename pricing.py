"""Whole-instance hourly equivalent including expanded data disk; fail closed."""
from decimal import Decimal
import math

from autodl_api import APIError, request_json

GIB = 1024 ** 3


def preview(config, candidate, gpu_num, expansion):
    data = request_json(config, 'order/price/preview', {
        'machine_id': candidate['machine_id'], 'charge_type': 'payg',
        'num': gpu_num, 'expand_data_disk': expansion,
    })['data']
    return from_data(data, expansion)


def from_data(data, expansion):
    if not isinstance(data, dict):
        raise APIError('价格预览格式异常')
    hourly = data.get('payg_price')
    disk = data.get('data_disk_info') or {}
    if not isinstance(disk, dict):
        raise APIError('数据盘价格格式异常')
    daily = disk.get('data_disk_deal_daily_price')
    if type(hourly) not in (int, float) or not math.isfinite(hourly) or hourly < 0:
        raise APIError('价格预览缺少有效的整机 GPU 费用')
    if expansion and (type(daily) not in (int, float) or not math.isfinite(daily) or daily < 0):
        raise APIError('价格预览缺少有效的数据盘费用')
    if not expansion:
        daily = 0
    hourly, daily = Decimal(str(hourly)) / 1000, Decimal(str(daily)) / 1000
    return {'gpu_hourly': hourly, 'disk_daily': daily, 'hourly_equivalent': hourly + daily / 24}


def ranked(config, candidates, gpu_num, expansion=None, maximum=None):
    if maximum is None or type(maximum) not in (int, float) or not math.isfinite(maximum) or maximum <= 0:
        raise ValueError('自动创建实例必须设置大于 0 的费用上限')
    matches = []
    for index, candidate in enumerate(candidates):
        size = candidate.get('expand_data_disk', 0) if expansion is None else expansion
        price = preview(config, candidate, gpu_num, size)
        if price['hourly_equivalent'] <= Decimal(str(maximum)):
            matches.append((price['hourly_equivalent'], index, dict(candidate, expand_data_disk=size)))
    return [candidate for _, _, candidate in sorted(matches, key=lambda row: (row[0], row[1]))]
