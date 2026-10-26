import hashlib
from io import BytesIO
from PIL import Image
import json
import time
import os
import tempfile
from pathlib import Path

import requests
import matplotlib.pyplot as plt
from fake_useragent import UserAgent


def update_config_authorization(token, config_path=None):
    """更新配置文件中的 authorization"""
    config_path = Path(config_path) if config_path is not None else Path(__file__).parent / 'config.json'
    if not isinstance(token, str) or not token.strip():
        raise ValueError('登录结果缺少有效令牌，请重新扫码')
    
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        
        config_data['authorization'] = token
        
        # 原子替换，防止写入被中断时破坏配置；保留刚刚修改过的其他选项。
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=config_path.parent, delete=False) as f:
                temporary = f.name
                json.dump(config_data, f, indent=2, ensure_ascii=False)
                f.write('\n')
            os.replace(temporary, config_path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    else:
        raise FileNotFoundError("配置文件不存在，无法保存登录信息")


def wait_for_wechat_ticket(headers, uuid, timeout=180):
    """保持二维码窗口响应，直到手机确认登录或等待超时。"""
    query_url = 'https://www.autodl.com/api/v1/wx/polling/query'
    started = time.monotonic()
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("等待微信登录超时，请重新运行并扫描新的二维码")
        try:
            response = requests.post(
                url=query_url, headers=headers, json={'uuid': uuid},
                timeout=min(10, remaining),
            )
            response.raise_for_status()
            data = response.json().get('data') or {}
            if data.get('ticket'):
                return data['ticket']
        except requests.Timeout:
            pass
        plt.pause(min(1, remaining))


def login_and_update_config(phone=None, password=None, config_path=None):
    """登录并自动更新配置文件"""
    headers = {
        'User-Agent': UserAgent().random
    }
    
    if phone is not None and password is not None:
        # 1、获取ticket
        login_url = 'https://www.autodl.com/api/v1/new_login'
        sha1 = hashlib.sha1()
        sha1.update(str(password).encode('utf-8'))
        data = {
            'phone': str(phone),
            'password': sha1.hexdigest(),
            'phone_area': '+86'
        }
        response = requests.post(url=login_url, headers=headers, json=data, timeout=15).json()
        ticket = response['data']['ticket']
        # 2、获取token
        passport_url = 'https://www.autodl.com/api/v1/passport'
        response = requests.post(url=passport_url, headers=headers, json={'ticket': ticket}, timeout=15).json()
        token = response['data']['token']
        
        # 更新配置文件
        update_config_authorization(token, config_path=config_path)
        print(f"登录成功，authorization 已更新到配置文件")
        return token
        
    else:
        # 1、获取二维码和uuid
        login_url = 'https://www.autodl.com/api/v1/wx/login'
        response = requests.get(url=login_url, headers=headers, timeout=15).json()
        qrcode_url = response['data']['qrcode_url']
        uuid = response['data']['uuid']
        # 2、获取ticket
        response = requests.get(url=qrcode_url, headers=headers, timeout=15).content
        img = Image.open(BytesIO(response))
        plt.imshow(img)
        plt.axis('off')
        plt.show(block=False)
        plt.pause(0.1)
        print("请使用微信扫描二维码，并在手机上确认登录；程序正在等待登录结果...", flush=True)
        try:
            ticket = wait_for_wechat_ticket(headers, uuid)
        finally:
            plt.close()
        # 3、获取token
        passport_url = 'https://www.autodl.com/api/v1/passport'
        response = requests.post(url=passport_url, headers=headers, json={'ticket': ticket}, timeout=15).json()
        token = response['data']['token']
        
        # 更新配置文件
        update_config_authorization(token, config_path=config_path)
        print(f"登录成功，authorization 已更新到配置文件")
        return token


# 保持原有的 login 函数兼容性
def login(phone=None, password=None):
    """原有的登录函数，保持向后兼容"""
    return login_and_update_config(phone, password)
