"""Read-only progress for an accepted creation; never issues a purchase."""
import time
from cloning import list_instances
from autodl_api import APIError, AuthenticationError


def wait(config, instance_uuid, *, report=print):
    deadline = time.monotonic() + config.clone_completion_timeout
    previous = None
    try:
        while time.monotonic() < deadline:
            try:
                row = next((row for row in list_instances(config) if row['uuid'] == instance_uuid), None)
                if row and row.get('status') in ('running', 'shutdown'):
                    status = 'completed'
                elif row and row.get('status') in ('create_failed', 'pull_failed', 'clone_failed'):
                    status = 'failed'
                else:
                    status = 'pending'
                if status != 'pending':
                    return {'status': status, 'message': '实例已就绪' if status == 'completed' else '平台报告实例创建失败，请到官网查看'}
                message = '正在准备实例 · 等待平台报告就绪 · Ctrl+C 停止本机跟踪'
            except AuthenticationError:
                return {'status': 'pending', 'message': '登录已失效；更新凭证后可恢复跟踪，不会再次下单'}
            except APIError:
                message = '暂时无法读取进度，将重试查询，不会再次下单'
            if message != previous:
                report(message)
                previous = message
            time.sleep(min(config.clone_status_interval, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        return {'status': 'pending', 'message': '已停止本机跟踪，平台仍可能继续创建；可从主菜单恢复跟踪'}
    return {'status': 'pending', 'message': '暂未确认就绪，可从主菜单继续跟踪；不会再次下单'}
