import argparse
import math
import sys
import time
from datetime import datetime

import requests

import gpu
import cloning
import notifications
import watch
from autodl_api import APIError, AuthenticationError, PurchaseUncertainError
from config_loader import Config
from desktop_alert import DesktopAlert
from regions import region_name, split_region_arguments


def log(message):
    print(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}', flush=True)


def finish_clone(config, popup, source, result):
    """已受理后进入只读跟踪；这里的任何异常都不能回到下单循环。"""
    context = (f'地区：{result["region_name"]}\nGPU：{config.gpu_model} × {config.gpu_num}\n'
               f'源实例：{source.uuid}\n新实例：{result["instance_uuid"]}\n')

    def announce(title, message):
        log(f'{title}：{message}')
        log(f'新实例ID：{result["instance_uuid"]}')
        try:
            shown = popup.show(title, context + '\n' + message, force=True)
            if not shown and popup.error:
                log(popup.error)
        except Exception:
            log('本机结果弹窗未能显示，请以上述终端结果为准')
        try:
            sent = notifications.send_notification(config, title, context + '\n' + message)
            if config.sendkey and not sent:
                log('微信通知发送失败；不会因此重复克隆')
        except Exception:
            log('微信通知发送失败；不会因此重复克隆')

    announce('克隆请求已受理', '平台正在创建新实例并复制数据。此时尚未确认复制完成；程序将继续跟踪进度，不再下单。')
    try:
        outcome = cloning.wait_for_completion(config, result['instance_uuid'], report=log)
    except KeyboardInterrupt:
        outcome = {'status': 'pending', 'message': '本机跟踪已停止，平台上的克隆仍可能继续；请到官网检查新实例，不要重复创建'}
    except Exception:
        # 不输出带认证信息的原始异常；更不能让它进入库存重试循环。
        outcome = {'status': 'pending', 'message': '暂时无法确认复制结果，请到官网查看新实例；不会重新下单'}
    title = {'completed': '克隆成功 · 数据复制完成',
             'failed': '克隆或数据复制失败', 'pending': '克隆进度待确认'}[outcome['status']]
    announce(title, outcome['message'])
    return {'completed': 0, 'failed': 1, 'pending': 2}[outcome['status']]


def monitor(config, *, once=False):
    popup = DesktopAlert(enabled=config.desktop_popup, cooldown=config.popup_cooldown)
    last_notification = None
    failures = 0
    while True:
        try:
            snapshot = watch.watch_gpu(config, config.gpu_model)
            for region, info in snapshot.items():
                name = info.get('region_name', region_name(region))
                if info.get('listed', True):
                    log(f'{name} | {config.gpu_model} | '
                        f"总计 {info['total_gpu_num']} 张，空闲 {info['idle_gpu_num']} 张")
                else:
                    log(f'{name} | 该区域当前未列出 {config.gpu_model}')

            scan_started = time.monotonic()
            candidates = gpu.find_candidates(config, snapshot)
            rentable_count = len(candidates)
            same_config_count = rentable_count
            source = getattr(config, 'clone_source', None)
            if source is not None:
                candidates = cloning.matching_candidates(source, candidates)
                same_config_count = len(candidates)
                candidates = cloning.within_budget(config, source, candidates)
            scan_seconds = time.monotonic() - scan_started
            failures = 0
            delay = config.interval
            if candidates:
                idle = sum(machine['gpu_idle_num'] for machine in candidates)
                summary_idle = sum(info['idle_gpu_num'] for info in snapshot.values())
                if idle != summary_idle:
                    log(f'汇总快照空闲 {summary_idle} 张，本轮可租机器明细空闲 {idle} 张'
                        f'（检查耗时 {scan_seconds:.1f} 秒）；本次按机器明细判断')
                log(f'发现可用GPU：{len(candidates)} 台机器满足每台 {config.gpu_num} 张的需求'
                    f'（这些可租机器共空闲 {idle} 张）')
                if not once:
                    names = ', '.join(dict.fromkeys(
                        machine.get('region_name', region_name(machine['region']))
                        for machine in candidates
                    ))
                    shown = popup.show(
                        f'发现空闲 GPU — {config.gpu_model}',
                        f'地区：{names}\nGPU：{config.gpu_model}\n'
                        f'可租机器：{len(candidates)} 台\n单台需求：{config.gpu_num} 张\n'
                        f'可租机器空闲卡数：{idle} 张\n\n'
                        + ('将尝试创建一个同配置克隆，含系统盘和数据盘，请查看终端结果。'
                         if source is not None else
                         '请尽快前往 AutoDL 查看，库存可能随时变化。\n监控会继续运行。'),
                    )
                    if shown:
                        log('已触发本机弹窗和提示音，监控继续运行')
                    elif popup.error:
                        log(popup.error)
                if config.mode in ('monitor_and_buy', 'monitor_and_clone') and not once:
                    if config.mode == 'monitor_and_clone':
                        result = cloning.clone_once(config, source, candidates)
                    else:
                        result = gpu.buy(config, config.gpu_model, config.gpu_num,
                                         config.image, config.private_image_uuid,
                                         gpu_info=snapshot, candidates=candidates)
                    if result['success']:
                        if config.mode == 'monitor_and_clone':
                            return finish_clone(config, popup, source, result)
                        log(f"{result['message']}，地区：{result['region_name']}，"
                            f"机器ID：{result['machine_id']}")
                        if result.get('instance_uuid'):
                            log(f"新实例ID：{result['instance_uuid']}；克隆及数据复制进度请在官网查看")
                        sent = notifications.send_notification(
                            config, f'GPU实例创建请求成功 - {config.gpu_model}',
                            f"{result['message']}\n地区：{result['region_name']}\n"
                            f"机器ID：{result['machine_id']}\n实际费用请在 AutoDL 控制台查看。",
                            snapshot,
                        )
                        if config.sendkey:
                            log('微信通知已发送' if sent else '微信通知发送失败，实例已创建，请查看控制台')
                        return 0  # 成功后退出，通知失败也绝不再次购买。
                    log(result['message'])
                    delay = config.retry_interval
                else:
                    now = time.monotonic()
                    if not once and config.sendkey and (
                        last_notification is None or now - last_notification >= config.notification_cooldown
                    ):
                        sent = notifications.send_notification(
                            config, f'GPU可用通知 - {config.gpu_model}',
                            f'发现 {len(candidates)} 台机器满足每台 {config.gpu_num} 张的需求。',
                            snapshot,
                        )
                        log('微信通知已发送' if sent else '微信通知发送失败，稍后重试')
                        # 失败时也限流，避免在推送服务异常时每轮轰炸。
                        last_notification = now
                    if config.stop_after_found:
                        return 0
            else:
                summary_idle = sum(info['idle_gpu_num'] for info in snapshot.values())
                if summary_idle >= config.gpu_num and rentable_count == 0:
                    log(f'地区汇总显示空闲 {summary_idle} 张，但本轮机器明细未找到可租机器'
                        f'（明细检查耗时 {scan_seconds:.1f} 秒）；两次查询间库存可能变化，'
                        '也可能是汇总与明细不同步或机器不可租')
                    counts = getattr(config, 'machine_scan', {})
                    if counts:
                        log(f"机器明细：检查 {counts['examined']} 台；不可租状态 {counts['disabled']} 台，"
                            f"实例容量已满 {counts['capacity_full']} 台，空闲卡不足 {counts['insufficient_idle']} 台")
                if source is not None:
                    if rentable_count:
                        log(f'可租机器 {rentable_count} 台 → 同配置 {same_config_count} 台'
                            f' → 符合费用上限 {len(candidates)} 台')
                    log('暂未找到同配置且符合费用上限的可租机器')
                elif summary_idle < config.gpu_num:
                    log('暂无可用GPU，继续监控...' if not once else '暂无可用GPU')
        except (AuthenticationError, PurchaseUncertainError) as exc:
            log(str(exc))
            return 2
        except (APIError, requests.RequestException) as exc:
            failures += 1
            # 不打印 requests 异常的原文，避免泄露认证信息或通知密钥。
            log(str(exc) if isinstance(exc, APIError) else '网络请求失败')
            if once:
                return 1
            delay = min(config.interval * 2 ** min(failures - 1, 4), 300)
        if once:
            return 0
        log(f'{delay:g} 秒后再次检查，按 Ctrl+C 停止')
        time.sleep(delay)


def main(argv=None):
    parser = argparse.ArgumentParser(description='autodl-monitor：AutoDL GPU 库存监控与自动租用')
    parser.add_argument('--once', action='store_true', help='只查询一次，不创建付费实例')
    parser.add_argument('--login', action='store_true', help='重新扫码登录')
    parser.add_argument('--region', nargs='+', help='本次查询的地区名称或代码，例如 北京B区 或 bj-B1')
    parser.add_argument('--gpu', help='本次查询的 GPU 型号，例如 "RTX 5090"')
    parser.add_argument('--interval', type=float, help='查询间隔秒数，大于 0')
    parser.add_argument('--no-popup', action='store_true', help='本次关闭本机弹窗')
    parser.add_argument('--test-popup', action='store_true', help='显示测试弹窗，不查询库存、不登录、不购买')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--list-instances', action='store_true', help='读取当前账号的源实例列表')
    actions.add_argument('--clone-preview', action='store_true', help='选择源实例并预览同配置克隆计划，不下单')
    actions.add_argument('--clone', action='store_true', help='选择源实例，确认后监控并克隆一次，含数据盘')
    parser.add_argument('--source', help='本次使用的源实例ID；省略时启动后交互选择')
    parser.add_argument('--max-price', type=float, help='克隆每小时总费用折算上限，含数据盘日费÷24')
    args = parser.parse_args(argv)
    try:
        config = Config()
        if args.no_popup:
            config.desktop_popup = False
        if args.test_popup:
            alert = DesktopAlert(enabled=True, cooldown=0)
            if alert.show('GPU监控 · 测试弹窗',
                          '这是一条测试提示，不代表发现了空闲 GPU。\n\n'
                          '正式监控发现满足需求的机器后，会在这里显示地区、型号和数量，'
                          '并播放提示音。\n\n弹窗不会暂停后台监控。'):
                log('已触发测试弹窗，此次未查询库存或创建实例')
                return 0
            log(alert.error or '无法显示测试弹窗')
            return 1
        if args.region:
            config.region_names, config.region_sign_list = split_region_arguments(args.region)
        if args.gpu:
            if not args.gpu.strip():
                raise ValueError('GPU 型号不能为空')
            config.gpu_model = args.gpu
        if args.interval is not None:
            if not math.isfinite(args.interval) or args.interval <= 0:
                raise ValueError('--interval 必须大于 0 且为有限数字')
            config.interval = args.interval
        if args.login or not config.has_authorization():
            if args.once and not args.login:
                log('尚未登录，请先使用 --login 扫码登录')
                return 2
            from login_helper import login_and_update_config
            config.Authorization = login_and_update_config(config_path=config.config_path)
        if args.list_instances:
            cloning.show_instances(cloning.list_instances(config))
            return 0
        clone_mode = args.clone or args.clone_preview or config.mode == 'monitor_and_clone'
        if (args.source or args.max_price is not None) and not clone_mode:
            raise ValueError('--source 和 --max-price 请与 --clone 或 --clone-preview 一起使用')
        if clone_mode:
            if args.gpu or args.region:
                raise ValueError('同配置克隆的地区、GPU 型号和卡数由源实例决定，请不要同时指定 --gpu 或 --region')
            if args.max_price is not None:
                if not math.isfinite(args.max_price) or args.max_price <= 0:
                    raise ValueError('--max-price 必须是大于 0 的有限数字')
                config.clone_max_price = args.max_price
            source = cloning.choose_source(
                config, args.source or config.clone_source_uuid,
                interactive=sys.stdin.isatty(),
            )
            cloning.prepare_config(config, source)
            print(cloning.describe_source(source), flush=True)
            if config.clone_max_price is not None:
                log(f'总费用折算上限：{config.clone_max_price:g} 元/小时（含数据盘日费÷24）')
            if args.clone_preview:
                log('以上仅为克隆计划，未创建或更改任何实例')
                return 0
            if not args.once:
                if config.clone_max_price is None:
                    raise ValueError('未提供费用上限，未启动自动克隆；请加 --max-price 金额 后重新运行')
                if not sys.stdin.isatty():
                    raise ValueError('自动克隆需要在本机交互终端确认以上计划')
                try:
                    confirmation = input('确认监控并创建一个克隆实例？输入 CLONE 确认，其余取消：').strip()
                except EOFError:
                    confirmation = ''
                if confirmation != 'CLONE':
                    log('已取消，未启动克隆')
                    return 0
            config.mode = 'monitor_and_clone'
        mode = '单次查询（不购买）' if args.once else (
            {'monitor_only': '仅监控', 'monitor_and_buy': '监控并购买',
             'monitor_and_clone': '监控并克隆一次（含数据盘）'}[config.mode]
        )
        selected = ', '.join(config.region_names + [
            region_name([code]) for code in config.region_sign_list
        ]) or '型号预设地区'
        log(f'运行模式：{mode} | 地区：{selected} | GPU：{config.gpu_model} | 需求：{config.gpu_num} 张')
        if config.desktop_popup and not args.once:
            log(f'本机弹窗已开启，重复提醒间隔至少 {config.popup_cooldown:g} 秒')
        if not config.sendkey:
            log('未配置 SendKey，微信推送未启用')
        return monitor(config, once=args.once)
    except KeyboardInterrupt:
        log('监控已停止')
        return 0
    except (ValueError, OSError, KeyError, APIError, requests.RequestException) as exc:
        if isinstance(exc, requests.RequestException):
            log('登录请求失败，请检查网络后重试')
        elif isinstance(exc, KeyError):
            log('登录接口响应不完整，请重新运行扫码登录')
        elif isinstance(exc, OSError):
            log('无法读取或保存配置，请检查文件权限')
        else:
            log(str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
