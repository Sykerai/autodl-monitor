"""AutoDL 地区代码与显示名称。"""

import ast
import re

from autodl_api import APIError, request_json


REGION_NAMES = {
    ('nm-B1', 'nm-B2'): '内蒙B区',
    ('cq-A1',): '重庆A区',
    ('west-B', 'west-C'): '西北B区',
    ('bj-B1',): '北京B区',
    ('bj-B1', 'bj-B2', 'bj-B3'): '北京B区',
    ('beijing-A', 'beijing-B', 'beijing-D', 'beijing-E'): '北京A区',
    ('foshan-A',): '佛山区',
    ('neimeng-A', 'neimeng-C', 'neimeng-D'): '内蒙A区',
    ('bj-C1',): 'L20专区',
    ('yz-A1',): '3090专区',
    ('beijing-C',): 'V100专区',
    ('nm-A1',): 'A800专区',
    ('gd-A1',): '华为昇腾专区',
}


def region_codes(region):
    return ast.literal_eval(region) if isinstance(region, str) else list(region)


def region_name(region):
    codes = region_codes(region)
    return REGION_NAMES.get(tuple(codes), ' / '.join(codes))


def resolve_region(value):
    for codes, name in REGION_NAMES.items():
        if value.replace(' ', '') == name:
            return list(codes)
    return [value]


def split_region_arguments(values):
    """名称实时解析，显式代码保持原样，不用静态映射展开名称。"""
    names, codes = [], []
    for value in values:
        value = value.strip()
        target = codes if re.fullmatch(r'[A-Za-z0-9_-]+', value) else names
        if value not in target:
            target.append(value.replace(' ', '') if target is names else value)
    return names, codes


def selected_region_groups(config):
    names = getattr(config, 'region_names', [])
    groups = []
    covered = set()
    if names:
        body = request_json(config, 'region/list', method='GET')
        if not isinstance(body['data'], list):
            raise APIError('地区列表响应格式异常')
        for name in names:
            codes = list(dict.fromkeys(
                entry['region_sign'] for entry in body['data']
                if isinstance(entry, dict) and entry.get('region_name') == name
                and isinstance(entry.get('region_sign'), str) and entry['region_sign']
            ))
            if not codes:
                raise ValueError(f'AutoDL 当前地区列表中没有“{name}”，请检查地区名称')
            groups.append((codes, name))
            covered.update(codes)
    for code in getattr(config, 'region_sign_list', []):
        if code not in covered:
            groups.append(([code], region_name([code])))
            covered.add(code)
    return groups
