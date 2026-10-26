"""带超时的 Server酱推送，兼容普通和 sctp SendKey。"""

import json
import re

import requests


def send_notification(config, title, content, gpu_info=None):
    if not config.sendkey or config.sendkey == '...':
        return False
    if config.sendkey.startswith('sctp'):
        match = re.match(r'^sctp(\d+)t', config.sendkey)
        if not match:
            return False
        url = f'https://{match.group(1)}.push.ft07.com/send/{config.sendkey}.send'
    else:
        url = f'https://sctapi.ftqq.com/{config.sendkey}.send'
    description = content
    if gpu_info:
        description += '\n\n' + json.dumps(gpu_info, indent=2, ensure_ascii=False)
    try:
        response = requests.post(url, json={
            'title': title, 'desp': description, 'tags': 'GPU监控|autodl',
        }, timeout=config.request_timeout)
        response.raise_for_status()
        body = response.json()
        return isinstance(body, dict) and (body.get('code') == 0 or body.get('errno') == 0)
    except (requests.RequestException, ValueError):
        # SendKey 在 URL 内，不能把包含 URL 的异常直接写入日志。
        return False
