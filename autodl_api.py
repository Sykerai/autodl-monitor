"""统一处理 AutoDL 请求；创建实例的请求绝不自动重发。"""

import time

import requests


class APIError(RuntimeError):
    pass


class AuthenticationError(APIError):
    pass


class PurchaseUncertainError(APIError):
    pass


def request_json(config, path, payload=None, *, purchase=False, method='POST'):
    attempts = 1 if purchase else getattr(config, 'max_retries', 2) + 1
    for attempt in range(attempts):
        try:
            request = requests.get if method == 'GET' else requests.post
            arguments = {'params': payload} if method == 'GET' else {'json': payload}
            response = request(
                f'https://www.autodl.com/api/v1/{path}',
                headers={'Authorization': config.Authorization,
                         'User-Agent': 'autodl-monitor/1.0'},
                **arguments, timeout=getattr(config, 'request_timeout', 15),
            )
            if response.status_code in (401, 403):
                raise AuthenticationError('登录已失效，请使用 --login 重新扫码登录')
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or 'code' not in body:
                raise ValueError('响应缺少状态字段')
        except AuthenticationError:
            raise
        except KeyboardInterrupt:
            if purchase:
                raise PurchaseUncertainError(
                    '创建请求被中断，结果尚未确认。请先到 AutoDL 控制台检查有无实例，再决定是否重启。'
                ) from None
            raise
        except (requests.RequestException, ValueError) as exc:
            if purchase:
                raise PurchaseUncertainError(
                    '创建请求的结果尚未确认。已停止自动购买，请先到 AutoDL 控制台检查是否已有实例，避免重复创建。'
                ) from exc
            if attempt + 1 == attempts:
                raise APIError('查询失败：网络异常或接口响应无效，将在下一轮重试') from exc
            time.sleep(min(2 ** attempt, 4))
            continue

        code = str(body['code'])
        if code != 'Success':
            # 不打印原始响应，防止令牌等账户信息进入日志。
            message = str(body.get('msg') or body.get('message') or '')
            auth_text = f'{code} {message}'.lower()
            if code in ('401', '403') or any(word in auth_text for word in (
                'unauth', 'notlogin', 'not_login', 'token', '登录', '登陆', '未认证',
            )):
                raise AuthenticationError('登录已失效，请使用 --login 重新扫码登录')
            if purchase:
                return body
            raise APIError('AutoDL 拒绝了查询，请确认账号状态和地区配置')
        if not purchase and 'data' not in body:
            raise APIError('AutoDL 查询结果缺少 data 字段')
        return body
