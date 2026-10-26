import json
import math
from pathlib import Path


class Config:
    def __init__(self, config_file='config.json'):
        self.config_path = Path(__file__).parent / config_file
        try:
            self.config_data = json.loads(self.config_path.read_text(encoding='utf-8'))
        except FileNotFoundError as exc:
            raise ValueError(f'配置文件 {config_file} 不存在') from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f'配置文件 JSON 格式错误：第 {exc.lineno} 行') from exc
        self.reload_values()

    def reload_values(self):
        data = self.config_data
        if not isinstance(data, dict):
            raise ValueError('配置文件顶层必须是对象')
        self.Authorization = data.get('authorization', '')
        if not isinstance(self.Authorization, str):
            raise ValueError('authorization 必须是字符串')
        self.mode = data.get('mode', 'monitor_only')
        if self.mode not in ('monitor_only', 'monitor_and_buy', 'monitor_and_clone'):
            raise ValueError('mode 必须是 monitor_only、monitor_and_buy 或 monitor_and_clone')
        gpu = data.get('gpu', {})
        image = data.get('image', {})
        monitor = data.get('monitor', {})
        notification = data.get('notification', {})
        if not all(isinstance(item, dict) for item in (gpu, image, monitor, notification)):
            raise ValueError('gpu、image、monitor 和 notification 必须是对象')
        self.gpu_model = gpu.get('model', '')
        self.gpu_num = gpu.get('num', 1)
        if not isinstance(self.gpu_model, str) or not self.gpu_model.strip():
            raise ValueError('gpu.model 必须填写 GPU 型号')
        if type(self.gpu_num) is not int or not 1 <= self.gpu_num <= 8:
            raise ValueError('gpu.num 必须是 1 到 8 的整数')
        self.region_sign_list = data.get('region_sign_list', [])
        if not isinstance(self.region_sign_list, list) or any(
            not isinstance(code, str) or not code.strip() for code in self.region_sign_list
        ):
            raise ValueError('region_sign_list 必须是非空地区代码组成的列表')
        self.region_sign_list = list(dict.fromkeys(self.region_sign_list))
        self.region_names = data.get('region_names', [])
        if not isinstance(self.region_names, list) or any(
            not isinstance(name, str) or not name.strip() for name in self.region_names
        ):
            raise ValueError('region_names 必须是非空地区名称组成的列表')
        self.region_names = list(dict.fromkeys(name.replace(' ', '') for name in self.region_names))
        self.private_image_uuid = image.get('private_image_uuid', '')
        self.image = image.get('autodl_image', '')
        self.sendkey = notification.get('sendkey', '')
        if not all(isinstance(value, str) for value in (
            self.private_image_uuid, self.image, self.sendkey,
        )):
            raise ValueError('镜像地址、私有镜像 UUID 和 SendKey 必须是字符串')
        for key, default in (
            ('interval', 30), ('retry_interval', 10), ('request_timeout', 15),
        ):
            value = monitor.get(key, default)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'monitor.{key} 必须是大于 0 的有限数字')
            setattr(self, key, value)
        self.max_retries = monitor.get('max_retries', 2)
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 5:
            raise ValueError('monitor.max_retries 必须是 0 到 5 的整数')
        self.stop_after_found = monitor.get('stop_after_found', False)
        if type(self.stop_after_found) is not bool:
            raise ValueError('monitor.stop_after_found 必须是 true 或 false')
        self.notification_cooldown = notification.get('cooldown_seconds', 300)
        if type(self.notification_cooldown) not in (int, float) or not math.isfinite(self.notification_cooldown) or self.notification_cooldown < 0:
            raise ValueError('notification.cooldown_seconds 必须是非负有限数字')
        self.desktop_popup = notification.get('desktop_popup', True)
        if type(self.desktop_popup) is not bool:
            raise ValueError('notification.desktop_popup 必须是 true 或 false')
        self.popup_cooldown = notification.get('popup_cooldown_seconds', 300)
        if type(self.popup_cooldown) not in (int, float) or not math.isfinite(self.popup_cooldown) or self.popup_cooldown < 0:
            raise ValueError('notification.popup_cooldown_seconds 必须是非负有限数字')
        clone = data.get('clone', {})
        if not isinstance(clone, dict):
            raise ValueError('clone 必须是对象')
        self.clone_source_uuid = clone.get('source_instance_uuid', '')
        if not isinstance(self.clone_source_uuid, str):
            raise ValueError('clone.source_instance_uuid 必须是字符串')
        self.clone_max_price = clone.get('max_price_per_hour')
        if self.clone_max_price is not None and (
            type(self.clone_max_price) not in (int, float)
            or not math.isfinite(self.clone_max_price) or self.clone_max_price <= 0
        ):
            raise ValueError('clone.max_price_per_hour 必须是大于 0 的有限数字或 null')
        self.clone_source = None
        self.clone_completion_timeout = clone.get('completion_timeout_seconds', 3600)
        self.clone_status_interval = clone.get('status_interval_seconds', 10)
        for key, value in (
            ('completion_timeout_seconds', self.clone_completion_timeout),
            ('status_interval_seconds', self.clone_status_interval),
        ):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'clone.{key} 必须是大于 0 的有限数字')

    def has_authorization(self):
        return bool(self.Authorization.strip())

    def update_authorization(self, authorization):
        # 登录助手负责落盘；此处仍支持原有的配置更新接口。
        from login_helper import update_config_authorization
        update_config_authorization(authorization, config_path=self.config_path)
        self.Authorization = authorization
        self.config_data['authorization'] = authorization
