"""One terminal wizard. Inputs are preferences; only a confirmed task can execute."""
import copy
import getpass
import json
import math
import sys
import time
from types import SimpleNamespace

import catalog
import cloning
from autodl_api import APIError, AuthenticationError
from desktop_alert import DesktopAlert
from task_store import TaskStore, task_plan, PLAN_FIELDS
import terminal_ui as ui

MODES = {'monitor_only': '仅监控', 'monitor_and_buy': '自动新建', 'monitor_and_clone': '自动克隆'}
STATUSES = {'waiting': '等待资源', 'paused': '已停止等待', 'submitting': '提交结果待核实',
            'uncertain': '提交结果待核实', 'accepted': '请求已受理', 'tracking': '等待实例准备完成'}


class Back(Exception):
    pass


def answer(prompt, default=None):
    text = ui.field(prompt, default)
    if text.lower() in ('b', 'q'):
        raise Back()
    return text if text else (str(default) if default is not None else '')


def number(prompt, default, *, integer=False, minimum=0):
    while True:
        text = answer(prompt, default)
        try:
            value = int(text) if integer else float(text)
            if not math.isfinite(value) or value < minimum:
                raise ValueError()
            return value
        except ValueError:
            ui.notice('请输入符合范围的数字。')


def yes(prompt, default=False):
    while True:
        text = answer(prompt + '（y/n）', 'y' if default else 'n').lower()
        if text in ('y', 'yes'):
            return True
        if text in ('n', 'no'):
            return False
        ui.notice('请输入 y 或 n。')


def choose(prompt, rows, *, label=str, multiple=False, defaults=None, page_size=8, description=None):
    """Search/paging never changes the underlying selection identity."""
    if not rows:
        raise ValueError('当前没有可选择的项目，请返回更换条件')
    filtered = list(range(len(rows)))
    query, page = '', 0
    while True:
        ui.section(prompt, description)
        if query:
            ui.hint('搜索：' + query)
            print()
        for index in filtered[page * page_size:(page + 1) * page_size]:
            ui.option(index + 1, label(rows[index]), selected=index in (defaults or []))
        ui.hint(f'共 {len(filtered)} 项  ·  第 {page + 1}/{max(1, math.ceil(len(filtered) / page_size))} 页')
        help_text = 'b 返回  ·  /关键词 搜索  ·  / 清除搜索'
        if len(filtered) > page_size:
            help_text += '  ·  n/p 翻页'
        ui.hint(help_text)
        hint = '选择序号（多选用逗号分隔）' if multiple else '选择序号'
        default_text = ','.join(str(i + 1) for i in (defaults or [])) or None
        text = answer(hint, default_text)
        if text.startswith('/'):
            query = text[1:].casefold()
            filtered = [i for i, row in enumerate(rows) if query in label(row).casefold()]
            page = 0
            continue
        if text.lower() == 'n':
            page = min(page + 1, max(0, (len(filtered) - 1) // page_size))
            continue
        if text.lower() == 'p':
            page = max(0, page - 1)
            continue
        try:
            indices = list(dict.fromkeys(int(part.strip()) - 1 for part in text.replace('，', ',').split(',')))
            if not indices or (not multiple and len(indices) != 1) or any(i not in filtered for i in indices):
                raise ValueError()
            return [rows[i] for i in indices] if multiple else rows[indices[0]]
        except ValueError:
            ui.notice('序号无效，请选择当前搜索结果中的序号。')


def ensure_login(config, *, force=False):
    if config.has_authorization() and not force:
        try:
            # Verify session against a read-only authenticated catalog.
            catalog.list_images(config, 'base', 'x86', 'nvidia')
            return
        except AuthenticationError:
            print('保存的登录凭证已失效，请重新登录。')
    while True:
        method = choose('登录方式', ['粘贴 authorization（隐藏输入）', '微信扫码登录'])
        if method.startswith('粘贴'):
            token = getpass.getpass('粘贴 authorization（输入不显示；留空返回）：').strip()
            if not token:
                continue
            previous = config.Authorization
            config.Authorization = token
            try:
                catalog.list_images(config, 'base', 'x86', 'nvidia')
            except (APIError, OSError):
                config.Authorization = previous
                print('凭证未能验证，请检查凭证或网络后重试。')
                continue
            config.update_authorization(token)
        else:
            from login_helper import login_and_update_config
            config.Authorization = login_and_update_config(config_path=config.config_path)
        print('登录成功，凭证已保存在本机。')
        return


def select_regions(config):
    rows = catalog.list_regions(config)
    default = [i for i, row in enumerate(rows) if row['name'] in config.region_names]
    selected = choose('选择地区（可多选）', rows, label=lambda row: row['name'], multiple=True, defaults=default)
    config.region_names, config.region_sign_list = [row['name'] for row in selected], []


def select_gpu(config):
    rows = catalog.list_gpu_types(config)
    default = [i for i, row in enumerate(rows) if row['gpu_name'] == config.gpu_model][:1]
    regions = ', '.join(config.region_names + config.region_sign_list)
    selected = choose(f'选择 GPU 型号 · {regions}', rows,
                      label=lambda row: f"{row['gpu_name']} · 空闲 {row.get('idle_gpu_num', '?')} / "
                                        f"总数 {row.get('total_gpu_num', '?')} 张 · {row['cpu_arch']} / {row['chip_corp']}",
                      defaults=default, description='仅显示所选地区的 GPU；空闲为 0 的型号也可继续等待。')
    config.gpu_model = selected['gpu_name']
    config.selected_gpu = selected


def select_amount(config):
    while True:
        value = number('每台需要几张卡（1～8）', config.gpu_num, integer=True, minimum=1)
        if value <= 8:
            config.gpu_num = value
            return
        ui.notice('最多选择 8 张卡。')


def choose_base_image(rows, saved):
    # Keep a true back stack inside the framework/version/Python/environment wizard.
    previous = next((row for row in rows if row['uuid'] == saved.get('uuid')), None)
    picks, depth = [], 0
    while True:
        selected = [row for row in rows if row['name'].split(' / ')[:depth] == picks[:depth]]
        try:
            if depth == 3:
                return choose('选择 CUDA / 环境版本', selected, label=lambda row: row['name'],
                              defaults=[i for i, row in enumerate(selected) if row['uuid'] == saved.get('uuid')])
            title = ['框架', '框架版本', 'Python 版本'][depth]
            options = list(dict.fromkeys(row['name'].split(' / ')[depth] for row in selected))
            old = picks[depth] if depth < len(picks) else (previous['name'].split(' / ')[depth] if previous else None)
            picked = choose('选择' + title, options, defaults=[options.index(old)] if old in options else None)
            picks = picks[:depth] + [picked]
            depth += 1
        except Back:
            if depth == 0:
                raise
            depth -= 1


def select_image(config):
    saved = config.image_selection or {}
    while True:
        kind = choose('镜像来源', ['官方基础镜像', '我的私有镜像'],
                      defaults=[1 if saved.get('kind') == 'private' else 0])
        kind = 'base' if kind == '官方基础镜像' else 'private'
        chip = config.selected_gpu
        rows = catalog.list_images(config, kind, chip['cpu_arch'], chip['chip_corp'])
        default = [i for i, row in enumerate(rows) if row['kind'] == saved.get('kind') and row['uuid'] == saved.get('uuid')]
        try:
            if kind == 'private':
                config.image_selection = choose('选择私有镜像', rows,
                                                label=lambda row: f"{row['name']} · CUDA {row['cuda_version'] or '未标注'}", defaults=default)
            else:
                config.image_selection = choose_base_image(rows, saved)
            config.image, config.private_image_uuid = '', ''
            return
        except Back:
            continue


def select_source(config):
    rows = cloning.list_instances(config)
    eligible = [row for row in rows if row.get('status') == 'shutdown' and row.get('charge_type') == 'payg']
    if not eligible:
        raise ValueError('没有已关机的按量计费源实例；请在官网处理后重试，程序不会自动关机')
    picked = choose('选择源实例（仅已关机、按量计费）', eligible,
                    label=lambda row: f"{row.get('name') or row.get('machine_alias') or row['uuid']} · "
                                      f"{row.get('region_name', '')} · {row.get('snapshot_gpu_alias_name', '')} × {row.get('req_gpu_amount', '?')} · {row['uuid']}",
                    defaults=[i for i, row in enumerate(eligible) if row['uuid'] == config.clone_source_uuid])
    source = cloning.load_source(config, picked)
    cloning.prepare_config(config, source)
    config.clone_source_uuid = source.uuid
    source_panel(source)


def source_panel(source):
    ui.panel('源实例配置', source_groups(source), notes=['复制系统盘和数据盘，保留原实例。'])


def source_groups(source):
    return [('来源', [('名称', source.name), ('实例 ID', source.uuid), ('地区', source.region_name)]),
            ('资源配置', [('GPU', f'{source.gpu_model} × {source.gpu_num} 张'),
                         ('每卡显存', f'{source.gpu_memory / cloning.GIB:g} GB'),
                         ('CPU / 内存', f'{source.cpu_cores:g} 核 / {source.memory_bytes / cloning.GIB:g} GB'),
                         ('系统盘', f'{source.system_disk_bytes / cloning.GIB:g} GB'),
                         ('数据盘', f'{source.data_disk_bytes / cloning.GIB:g} GB')])]


def select_disk(config):
    ui.section('数据盘扩容', '这里设置额外增加的容量，不包含平台默认数据盘。')
    if yes('额外扩容数据盘', config.expand_data_disk_gib > 0):
        while True:
            value = number('增加容量（GB，整数）', config.expand_data_disk_gib or 50, integer=True, minimum=1)
            if value <= 100000:
                config.expand_data_disk_gib = value
                return
            ui.notice('容量过大，请重新输入。')
    else:
        config.expand_data_disk_gib = 0


def select_budget(config):
    ui.section('费用上限', '按整个实例计算：GPU 小时费 + 额外数据盘日费 ÷ 24。')
    default = config.clone_max_price if config.mode == 'monitor_and_clone' else config.max_price_per_hour
    value = number('最高费用（元 / 小时）', default or 3, minimum=0.000001)
    if config.mode == 'monitor_and_clone':
        config.clone_max_price = value
    else:
        config.max_price_per_hour = value


def summary(config):
    groups = [('资源', [('模式', MODES[config.mode]),
                        ('地区', ', '.join(config.region_names + config.region_sign_list)),
                        ('GPU', f'{config.gpu_model} × {config.gpu_num} 张')])]
    if config.mode == 'monitor_and_buy':
        selection = config.image_selection
        image_rows = [('镜像来源', '官方基础镜像' if selection and selection['kind'] == 'base' else '我的私有镜像' if selection else '旧配置'),
                      ('镜像', selection['name'] if selection else '旧配置镜像')]
        if selection and selection.get('cuda_version'):
            image_rows.append(('CUDA', selection['cuda_version']))
        image_rows.append(('额外数据盘', f'+{config.expand_data_disk_gib} GB' if config.expand_data_disk_gib else '不额外扩容'))
        groups.append(('环境', image_rows))
        groups.append(('费用', [('整机上限', f'{config.max_price_per_hour:g} 元 / 小时'),
                               ('计算口径', 'GPU 小时费 + 额外数据盘日费 ÷ 24')]))
    if config.mode == 'monitor_and_clone':
        groups = [(None, [('模式', MODES[config.mode])])] + source_groups(config.clone_source)
        groups.append(('费用', [('整机上限', f'{config.clone_max_price:g} 元 / 小时')]))
    notes = [f'每 {config.interval:g} 秒检查  ·  无库存持续等待  ·  Ctrl+C 停止本机任务']
    if config.mode != 'monitor_only':
        notes.append('确认后自动创建 1 个实例；优先低价，同价按地区顺序。')
    if config.mode == 'monitor_and_clone':
        notes.append('复制系统盘和数据盘，保留原实例。')
    ui.panel('本次任务', groups, notes=notes)


def settings_menu(config):
    while True:
        item = choose('设置', ['检查间隔', 'macOS 弹窗与声音', '微信推送', '更新登录凭证', '退出登录', '返回主菜单'])
        if item == '返回主菜单':
            return
        try:
            if item == '检查间隔':
                config.interval = number('检查间隔（秒）', config.interval, minimum=1)
                config.retry_interval = number('明确下单失败后的等待间隔（秒）', config.retry_interval, minimum=1)
            elif item == 'macOS 弹窗与声音':
                config.desktop_popup = yes('开启弹窗与声音', config.desktop_popup)
            elif item == '微信推送':
                config.sendkey = getpass.getpass('输入 Server酱 SendKey（隐藏输入；留空关闭）：').strip()
            elif item == '更新登录凭证':
                ensure_login(config, force=True)
            elif item == '退出登录':
                from local_state import atomic_json
                value = json.loads(config.config_path.read_text())
                value['authorization'] = ''
                atomic_json(config.config_path, value)
                config.Authorization = ''
                print('已清除本机登录凭证。')
            config.save_preferences()
        except Back:
            continue


def build_plan(config, mode):
    config = copy.deepcopy(config)
    config.mode = mode
    ensure_login(config)
    steps = [select_source] if mode == 'monitor_and_clone' else [select_regions, select_gpu, select_amount]
    if mode == 'monitor_and_buy':
        steps += [select_image, select_disk, select_budget]
    elif mode == 'monitor_and_clone':
        steps += [select_budget]
    index = 0
    while True:
        try:
            if index < len(steps):
                steps[index](config)
                index += 1
                continue
            summary(config)
            ui.hint('y 开始  ·  n 取消  ·  b 返回修改')
            if yes('开始监控' if mode == 'monitor_only' else '确认开始自动租用', False):
                return config
            return None
        except Back:
            index -= 1
            if index < 0:
                raise
        except (ValueError, APIError) as exc:
            if isinstance(exc, AuthenticationError):
                raise
            print(str(exc))
            # A failed catalog or no eligible source must allow going back, not trap the user.
            if yes('重试当前步骤', False):
                continue
            index -= 1
            if index < 0:
                raise Back()


def track_existing(config, store):
    from main import finish_clone, finish_new
    result = dict(store.data['result'], region_name=', '.join(config.region_names))
    config.task_journal = store
    if config.mode == 'monitor_and_clone':
        return finish_clone(config, DesktopAlert(config.desktop_popup, config.popup_cooldown),
                            SimpleNamespace(uuid=config.clone_source_uuid), result)
    return finish_new(config, DesktopAlert(config.desktop_popup, config.popup_cooldown), result)


def run_task(config, store):
    from main import monitor
    config.task_journal = store
    config.stop_after_found = False  # Interactive monitoring always ends by user action.
    config.task_started_at = time.monotonic()
    try:
        code = monitor(config)
        if store.data['status'] == 'waiting':
            store.write(status='paused')
        if code:
            print('任务尚未完成，可从主菜单恢复；请先处理上述提示。')
    except KeyboardInterrupt:
        if store.data['status'] in ('waiting', 'paused'):
            store.write(status='paused')
            print('\n已停止等待。')
            try:
                if not yes('保留任务以便下次恢复', True):
                    store.write(status='cancelled')
            except (Back, KeyboardInterrupt, EOFError):
                pass
        else:
            print('\n已停止本机操作；平台可能仍在创建，任务记录已保留，不能重复下单。')
    finally:
        config.task_journal = None


def restore_plan(config, store):
    config = copy.deepcopy(config)
    plan = store.data['plan']
    for name in PLAN_FIELDS:
        if name in plan:
            setattr(config, name, copy.deepcopy(plan[name]))
    return config


def manage_task(config, store):
    if not store.active:
        print('没有未完成任务。')
        return
    action = choose('未完成任务', ['恢复任务 / 核实结果', '结束本机任务记录', '返回主菜单'])
    if action == '返回主菜单':
        return
    if action == '结束本机任务记录':
        if store.data['status'] in ('submitting', 'uncertain'):
            print('请求结果不确定，需要先查询并核实平台，再决定结束记录。')
        else:
            print('只结束本机任务记录，不会关闭、取消或释放平台上的实例。')
            if yes('确认结束此任务记录', False):
                store.write(status='cancelled')
            return
    resume_task(config, store)


def resume_task(config, store):
    if not store.active:
        print('没有未完成任务。')
        return
    current = restore_plan(config, store)
    ensure_login(current)
    status = store.data['status']
    if status in ('submitting', 'uncertain'):
        rows = cloning.list_instances(current)
        matches = store.reconcile(rows)
        ui.panel('上次请求待核实', [(None, [('提交时间', store.data['attempt']['submitted_at'])])],
                 notes=['以下是新出现且机器、卡数匹配的实例。请核对创建时间等信息后选择。'], style='warning')
        for index, row in enumerate(matches, 1):
            ui.option(index, f"{row['uuid']} · {row.get('status', '未知')} · {row.get('created_at', '创建时间未提供')}")
        if matches and yes('已核实其中一个就是上次创建的实例，继续跟踪', False):
            picked = choose('选择已核实的实例', matches, label=lambda row: row['uuid'])
            store.write(status='accepted', result={'instance_uuid': picked['uuid'],
                                                   'machine_id': picked['machine_id']})
            return track_existing(current, store)
        print('列表中暂未出现实例也不能证明下单失败。请到官网核实；本程序不会自动再次下单。')
        if yes('已在官网核实并处理上次请求，结束该任务记录（不会取消平台实例）', False):
            store.write(status='cancelled')
        return
    if status in ('accepted', 'tracking'):
        ui.panel('已受理的任务', [(None, [('实例 ID', store.data['result']['instance_uuid'])])],
                 notes=['接下来只跟踪该实例的进度。'])
        if yes('继续只读跟踪进度', True):
            return track_existing(current, store)
        return
    if current.mode == 'monitor_and_clone':
        source = cloning.choose_source(current, current.clone_source_uuid, interactive=False)
        if json.loads(json.dumps(source.signature())) != store.data['plan']['source_signature']:
            raise ValueError('源实例配置已改变，不能恢复原任务；请结束旧任务并重新选择')
        cloning.prepare_config(current, source)
    if current.mode == 'monitor_and_buy':
        catalog.refresh_image(current)
    summary(current)
    if yes('恢复此任务（付费任务将继续等待并自动创建一个实例）', False):
        store.write(status='waiting')
        run_task(current, store)
    elif yes('结束该任务并返回主菜单', False):
        store.write(status='cancelled')


def run(config):
    if not sys.stdin.isatty():
        print('交互菜单需要终端；自动化查询请使用 --once 等参数。')
        return 2
    store = TaskStore(config.state_dir / 'task.json')
    ui.section('autodl-monitor', '监控库存 · 自动新建 · 同配置克隆')
    while True:
        try:
            if store.active:
                ui.panel('未完成任务', [(None, [('模式', MODES.get(store.data['plan']['mode'], '未知')),
                                                ('状态', STATUSES[store.data['status']])])],
                         notes=['选择「恢复未完成任务」继续跟踪或核实结果。'], style='warning')
            mode = choose('主菜单', ['仅监控', '自动新建', '自动克隆', '恢复未完成任务', '设置 / 登录', '退出程序'])
            if mode == '退出程序':
                return 0
            if mode == '设置 / 登录':
                settings_menu(config)
                continue
            if mode == '恢复未完成任务':
                manage_task(config, store)
                continue
            if store.active:
                print('请先恢复或结束旧任务，防止重复创建实例。')
                continue
            key = next(key for key, value in MODES.items() if value == mode)
            planned = build_plan(config, key)
            if planned is None:
                print('已取消，未启动任务。')
                continue
            planned.save_preferences()
            # Keep user preferences (not runtime status) as the next wizard's defaults.
            for field in PLAN_FIELDS:
                if hasattr(planned, field):
                    setattr(config, field, copy.deepcopy(getattr(planned, field)))
            config.Authorization = planned.Authorization
            store.start(task_plan(planned))
            run_task(planned, store)
        except (Back, KeyboardInterrupt):
            print('\n已返回主菜单。')
        except EOFError:
            print('\n输入已关闭，退出程序；未完成任务可下次恢复。')
            return 0
        except AuthenticationError as exc:
            print(str(exc))
            try:
                ensure_login(config, force=True)
            except (Back, KeyboardInterrupt):
                pass
            except EOFError:
                return 0
        except (ValueError, APIError, OSError) as exc:
            print(str(exc) if not isinstance(exc, OSError) else '本机记录读写失败，请检查磁盘与权限；不能自动重新下单。')
