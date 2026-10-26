"""macOS 原生弹窗：独立进程显示，不阻塞库存查询。"""

import shutil
import subprocess
import sys
import time


ALERT_SCRIPT = '''
on run argv
    set alertTitle to item 1 of argv
    set alertMessage to item 2 of argv
    activate
    beep 2
    display dialog alertMessage with title alertTitle buttons {"知道了"} default button "知道了" with icon caution giving up after 300
end run
'''


class DesktopAlert:
    def __init__(self, enabled=True, cooldown=300):
        self.enabled = enabled
        self.cooldown = cooldown
        self.error = None
        self._last_attempt = None
        self._process = None

    def show(self, title, message, *, force=False):
        """最多显示一个窗口；关闭后仍需满足冷却时间。"""
        self.error = None
        if not self.enabled:
            return False
        if self._process is not None:
            status = self._process.poll()
            if status is None:
                if not force:
                    return False
                try:
                    self._process.terminate()
                    self._process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=1)
                except ProcessLookupError:
                    pass
                except OSError:
                    self.error = '无法替换原弹窗，请查看终端中的克隆结果'
                    return False
                status = 0
            self._process = None
            if status != 0:
                self.error = '弹窗未能正常显示，请检查本机桌面会话'
        now = time.monotonic()
        if not force and self._last_attempt is not None and now - self._last_attempt < self.cooldown:
            return False
        self._last_attempt = now
        if sys.platform != 'darwin' or not shutil.which('osascript'):
            self.error = '本机原生弹窗目前支持 macOS；请使用终端提示或配置微信推送'
            return False
        try:
            # 文案通过参数传递，不拼入脚本，型号中的引号也不会被执行。
            self._process = subprocess.Popen(
                ['osascript', '-e', ALERT_SCRIPT, title, message],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError:
            self.error = '无法启动本机弹窗，监控仍会继续'
            return False
        return True
