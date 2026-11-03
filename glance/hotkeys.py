# -*- coding: utf-8 -*-
"""全局快捷键：解析 'ctrl+alt+d'，在专门的线程里注册并收消息。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading

VK_MAP = {chr(ord("a") + i): 0x41 + i for i in range(26)}
VK_MAP.update({str(i): 0x30 + i for i in range(10)})
VK_MAP.update({f"f{i}": 0x70 + i - 1 for i in range(1, 13)})
VK_MAP.update({"space": 0x20, "tab": 0x09, "esc": 0x1B, "escape": 0x1B})

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_APP_RELOAD = 0x8001
WM_APP_TEMP = 0x8002     # wParam = 键码，lParam = 1 注册 / 0 取消
WM_APP_PAUSE = 0x8003    # wParam = 1 暂停全部快捷键 / 0 恢复
TEMP_BASE = 0x1000       # 临时快捷键的编号起点（编号 = TEMP_BASE + 键码）
VK_ESCAPE, VK_LEFT, VK_RIGHT = 0x1B, 0x25, 0x27
TEMP_NAMES = {VK_ESCAPE: "key:esc", VK_LEFT: "key:left", VK_RIGHT: "key:right"}
PAUSE_LIMIT = 30         # 暂停后最多多久自动恢复（录快捷键的窗口没正常失焦时兜底）


def parse_hotkey(spec: str) -> tuple[int, int]:
    mods, vk = 0, None
    for p in (x.strip().lower() for x in (spec or "").split("+")):
        if p in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif p in ("alt", "menu"):
            mods |= MOD_ALT
        elif p == "shift":
            mods |= MOD_SHIFT
        elif p in ("win", "super"):
            mods |= MOD_WIN
        elif p in VK_MAP:
            vk = VK_MAP[p]
    if vk is None:
        raise ValueError(f"快捷键里没有主键: {spec}")
    return mods | MOD_NOREPEAT, vk


def valid_hotkey(spec: str) -> bool:
    """至少要 Ctrl / Alt / Win 之一加一个主键（只带 Shift 会吃掉正常打字）。"""
    try:
        mods, _vk = parse_hotkey(spec)
    except ValueError:
        return False
    return bool(mods & (MOD_CONTROL | MOD_ALT | MOD_WIN))


class HotkeyThread:
    def __init__(self, get_hotkeys, on_hotkey, log=print):
        self._get = get_hotkeys
        self._on = on_hotkey
        self._log = log
        self._tid = 0
        self.status: dict[str, bool] = {}
        self.registered = threading.Event()
        self._resume_timer: threading.Timer | None = None
        threading.Thread(target=self._run, daemon=True, name="hotkeys").start()

    def reload(self) -> None:
        if self._tid:
            self.registered.clear()
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_APP_RELOAD, 0, 0)

    def set_temp(self, vk: int, on: bool) -> None:
        """临时独占一个单键（浮窗开着时的 Esc / ← / →）：按键被一瞥收下，不会再传给后面的程序。"""
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_APP_TEMP, vk, 1 if on else 0)

    def pause(self, on: bool) -> None:
        """设置里录新快捷键时先放开全部快捷键，不然按下已占用的组合会直接触发功能、录不进去。"""
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_APP_PAUSE, 1 if on else 0, 0)
        if self._resume_timer is not None:
            self._resume_timer.cancel()
            self._resume_timer = None
        if on:
            self._resume_timer = threading.Timer(PAUSE_LIMIT, self.pause, args=(False,))
            self._resume_timer.daemon = True
            self._resume_timer.start()

    def _run(self):
        user32 = ctypes.windll.user32
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        reg: dict[int, str] = {}
        temp: dict[int, str] = {}

        def unregister_all():
            for idx in list(reg):
                user32.UnregisterHotKey(None, idx)
            reg.clear()

        def register_all():
            unregister_all()
            status = {}
            for idx, (action, spec) in enumerate(self._get().items(), start=1):
                if not spec:
                    continue
                try:
                    mods, vk = parse_hotkey(spec)
                except ValueError:
                    status[action] = False
                    continue
                ok = bool(user32.RegisterHotKey(None, idx, mods, vk))
                status[action] = ok
                if ok:
                    reg[idx] = action
                else:
                    self._log(f"[热键] {spec} 注册失败（可能被别的软件占用）")
            self.status = status
            self.registered.set()

        try:
            register_all()
            msg = ctypes.wintypes.MSG()
            while True:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, -1):
                    break
                if msg.message == WM_HOTKEY and (msg.wParam in reg or msg.wParam in temp):
                    action = reg.get(msg.wParam) or temp[msg.wParam]
                    threading.Thread(target=self._on, args=(action,), daemon=True).start()
                elif msg.message == WM_APP_RELOAD:
                    register_all()
                elif msg.message == WM_APP_PAUSE:
                    if msg.wParam:
                        unregister_all()
                    else:
                        register_all()
                elif msg.message == WM_APP_TEMP and msg.wParam in TEMP_NAMES:
                    idx = TEMP_BASE + msg.wParam
                    if msg.lParam and idx not in temp:
                        if user32.RegisterHotKey(None, idx, MOD_NOREPEAT, msg.wParam):
                            temp[idx] = TEMP_NAMES[msg.wParam]
                    elif not msg.lParam and idx in temp:
                        user32.UnregisterHotKey(None, idx)
                        del temp[idx]
        except Exception as e:
            self._log(f"[热键] 线程异常 {e!r}")
