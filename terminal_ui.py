"""Small, width-aware terminal presentation. No network or task execution here."""
import os
import re
import shutil
import sys
import unicodedata
from datetime import datetime

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
PALETTE = {'accent': '36', 'muted': '90', 'success': '32', 'warning': '33', 'error': '31', 'bold': '1'}


def plain(value):
    return ANSI.sub('', str(value)).replace('\r', '').replace('\t', '  ')


def cells(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
               for c in plain(value))


def terminal_width():
    return max(24, min(88, shutil.get_terminal_size((88, 30)).columns - 2))


def tone(value, style):
    if sys.stdout.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb':
        return f'\033[{PALETTE[style]}m{value}\033[0m'
    return str(value)


def wrap(value, width):
    """Wrap by displayed cells, including Chinese, without dropping IDs or long names."""
    lines = []
    for paragraph in plain(value).split('\n'):
        current, used = '', 0
        for character in paragraph:
            size = cells(character)
            if used + size > width and current:
                lines.append(current.rstrip())
                current, used = '', 0
            current += character
            used += size
        lines.append(current.rstrip())
    return lines or ['']


def render_panel(title, groups=(), *, notes=(), width=None):
    """Return a monochrome panel suitable for both terminals and redirected logs."""
    width = terminal_width() if width is None else max(24, width)
    inner = width - 4
    groups = list(groups)
    label_width = min(12, max((cells(key) for _, rows in groups for key, _ in rows), default=0))
    content = []
    for heading, rows in groups:
        if content:
            content.append('')
        if heading:
            content.extend(wrap(heading, inner))
            content.append('')
        for key, value in rows:
            if inner < 42 or cells(key) > label_width:
                content.extend(wrap(key, inner))
                content.extend('  ' + line for line in wrap(value, inner - 2))
            else:
                prefix = plain(key) + ' ' * (label_width - cells(key)) + '  '
                parts = wrap(value, inner - label_width - 2)
                content.append(prefix + parts[0])
                content.extend(' ' * (label_width + 2) + line for line in parts[1:])
    if notes:
        if content:
            content.append('')
        for note in notes:
            content.extend(wrap(note, inner))
    title_lines = wrap(title, width - 6)
    top = '╭─ ' + title_lines[0] + ' ' + '─' * (width - cells(title_lines[0]) - 5) + '╮'
    for line in reversed(title_lines[1:]):
        content.insert(0, line)
    return '\n'.join([top] + ['│ ' + line + ' ' * (inner - cells(line)) + ' │' for line in content]
                     + ['╰' + '─' * (width - 2) + '╯'])


def panel(title, groups=(), *, notes=(), style='accent'):
    rendered = render_panel(title, groups, notes=notes)
    lines = rendered.splitlines()
    print()
    print(tone(lines[0], style))
    for line in lines[1:-1]:
        print(line)
    print(tone(lines[-1], style))
    print()


def section(title, description=None):
    print()
    for line in wrap(title, terminal_width() - 4):
        print('  ' + tone(line, 'accent'))
    print('  ' + tone('─' * min(terminal_width() - 2, 52), 'muted'))
    if description:
        hint(description)
    print()


def hint(message):
    for line in wrap(message, terminal_width() - 4):
        print('  ' + tone(line, 'muted'))


def notice(message):
    print()
    for index, line in enumerate(wrap(message, terminal_width() - 4)):
        print('  ' + tone(('! ' if index == 0 else '  ') + line, 'warning'))


def field(prompt, default=None):
    """Put the editable input on its own line, separate from explanatory copy."""
    print()
    for line in wrap(prompt, terminal_width() - 4):
        print('  ' + line)
    if default is not None:
        hint(f'直接回车使用：{default}')
    return input('  › ').strip()


def option(index, text, *, selected=False):
    parts = plain(text).split(' · ')
    prefix = f'  {index:>2}  '
    marker = '  [上次选择]' if selected else ''
    lines = wrap(parts[0] + marker, terminal_width() - cells(prefix))
    print(tone(prefix + lines[0], 'accent') if selected else prefix + lines[0])
    for line in lines[1:]:
        print(' ' * cells(prefix) + line)
    if len(parts) > 1:
        for line in wrap('  ·  '.join(parts[1:]), terminal_width() - cells(prefix)):
            print(' ' * cells(prefix) + tone(line, 'muted'))
    print()


def message(text):
    prefix = f'  {datetime.now():%H:%M:%S}  '
    lines = wrap(text, terminal_width() - cells(prefix))
    print(prefix + lines[0], flush=True)
    for line in lines[1:]:
        print(' ' * cells(prefix) + line, flush=True)


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


def scan(config, snapshot, candidates, *, checks, elapsed, scan_seconds, rentable_count, same_config_count):
    from regions import region_name
    idle = sum(row['gpu_idle_num'] for row in candidates)
    summary_idle = sum(row['idle_gpu_num'] for row in snapshot.values())
    rows = []
    for region, info in snapshot.items():
        name = info.get('region_name') or region_name(region)
        value = f"空闲 {info['idle_gpu_num']} / 总计 {info['total_gpu_num']} 张" if info.get('listed', True) else '该区域当前未列出此型号'
        rows.append((name, value))
    groups = [(None, [('GPU', f'{config.gpu_model} × {config.gpu_num} 张 / 台'),
                       ('检查次数', f'第 {checks} 次'), ('任务用时', duration(elapsed))]), ('地区库存', rows)]
    notes = []
    source = getattr(config, 'clone_source', None)
    if candidates:
        groups.append(('机器明细', [('可租机器', f'{len(candidates)} 台'), ('空闲卡数', f'{idle} 张')]))
        notes.append('发现可用GPU：机器明细满足本次卡数需求。')
        if summary_idle != idle:
            notes.append(f'汇总快照空闲 {summary_idle} 张，机器明细空闲 {idle} 张；以机器明细判断。')
    else:
        groups.append((None, [('当前状态', '暂无可用GPU，继续等待' if not source else '等待同配置且符合费用上限的机器')]))
        if summary_idle >= config.gpu_num and rentable_count == 0:
            notes.append(f'地区汇总显示空闲 {summary_idle} 张，机器明细未找到可租机器。')
            notes.append('汇总与明细可能不同步，或机器当前不可租。')
    if source:
        notes.append(f'筛选：可租 {rentable_count} 台 → 同配置 {same_config_count} 台 → 预算内 {len(candidates)} 台。')
    if summary_idle >= config.gpu_num and rentable_count == 0:
        counts = getattr(config, 'machine_scan', {})
        if counts:
            notes.append(f"扫描 {counts['examined']} 台 · 不可租 {counts['disabled']} 台 · "
                         f"容量满 {counts['capacity_full']} 台 · 卡数不足 {counts['insufficient_idle']} 台")
    notes.append(f'明细检查 {scan_seconds:.1f} 秒  ·  Ctrl+C 停止本机任务')
    panel(f'资源检查 · {datetime.now():%H:%M:%S}', groups, notes=notes,
          style='success' if candidates else 'accent')
