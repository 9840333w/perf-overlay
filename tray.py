# -*- coding: utf-8 -*-
"""系统托盘图标。

开启鼠标点击穿透后窗口不再接收鼠标事件，全局热键也可能与其他程序冲突，
托盘图标提供始终可用的入口：左键打开设置，右键菜单，双击显示/隐藏。

图标优先使用 assets/tray.ico；不存在且 PIL 可用时自动生成，否则回退系统默认图标。
托盘仅在本程序运行期间存在，退出即移除。
"""

from __future__ import annotations

import os
import sys
import queue
import threading

# 冻结成 exe 后 __file__ 指向包内路径，配置、日志与 vendor 必须与 exe 同级
BASE_DIR = (os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
ICON_PATH = os.path.join(BASE_DIR, "assets", "tray.ico")

MENU_ITEMS: list[tuple[str | None, str]] = [
    ("settings", "设置面板…"),
    ("move_mode", "移动模式（临时显示底板，便于拖动）"),
    ("toggle_visible", "显示 / 隐藏悬浮窗"),
    ("toggle_click_through", "鼠标点击穿透（开启后窗口不接收鼠标）"),
    ("font_bigger", "字号增大"),
    ("font_smaller", "字号减小"),
    (None, ""),
    ("quit", "退出并释放硬件访问"),
]

# 不同 pywin32 版本的 win32con 缺少部分通知图标常量，这里统一定义
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x00000001, 0x00000002, 0x00000004
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0x00000000, 0x00000001, 0x00000002
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x00000010, 0x00000040
IDI_APPLICATION = 32512
HWND_MESSAGE = -3
TPM_LEFTALIGN, TPM_RETURNCMD, TPM_NONOTIFY = 0x0000, 0x0100, 0x0080
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0203, 0x0205, 0x007B

def _const(module, name: str, default):
    return getattr(module, name, default)

def _ensure_icon() -> bool:
    """生成托盘图标；失败时回退系统默认图标。"""
    if os.path.exists(ICON_PATH):
        return True
    try:
        from PIL import Image, ImageDraw

        os.makedirs(os.path.dirname(ICON_PATH), exist_ok=True)
        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((1, 1, 30, 30), radius=7, fill=(14, 16, 20, 225))
        for i, (h, color) in enumerate(((9, (226, 226, 226, 255)),
                                        (18, (77, 171, 247, 255)),
                                        (13, (61, 255, 139, 255)))):
            x = 6 + i * 8
            d.rectangle((x, 25 - h, x + 5, 25), fill=color)
        img.save(ICON_PATH, sizes=[(16, 16), (32, 32)])
        return True
    except Exception:
        return False

class TrayIcon(threading.Thread):
    """托盘线程；动作通过 queue 交给 Tk 主线程执行。"""

    def __init__(self, out: queue.Queue, tooltip: str = "性能悬浮窗") -> None:
        super().__init__(daemon=True, name="tray")
        self.out = out
        self.tooltip = tooltip
        self.available = False
        self.error: str | None = None
        self._alive = True
        self._hwnd = None
        self._nid: tuple | None = None
        self._ids = {i + 1: name for i, (name, _label) in enumerate(MENU_ITEMS) if name}

    def stop(self) -> None:
        self._alive = False
        try:
            import win32con
            import win32gui

            if self._hwnd:
                win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass

    # ------------------------------------------------------------------ 主体
    def run(self) -> None:
        try:
            import win32api
            import win32con
            import win32gui
        except Exception as exc:
            self.error = f"pywin32 不可用: {type(exc).__name__}: {exc}"
            return

        WM_TRAY = win32con.WM_USER + 7
        hicon = 0
        try:
            if _ensure_icon():
                hicon = win32gui.LoadImage(0, ICON_PATH, IMAGE_ICON, 16, 16,
                                           LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if not hicon:
                hicon = win32gui.LoadIcon(0, IDI_APPLICATION)
        except Exception:
            hicon = 0
        if not hicon:
            try:
                hicon = win32gui.LoadIcon(0, IDI_APPLICATION)
            except Exception:
                hicon = 0

        hinst = win32api.GetModuleHandle(None)
        class_name = "PerfOverlayTrayClass"
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wndproc                       # pywin32 支持直接传可调用对象
        wc.lpszClassName = class_name
        wc.hInstance = hinst
        try:
            try:
                win32gui.RegisterClass(wc)
            except Exception as exc:
                if "1412" not in str(exc):                  # 类已注册过（上次没退干净）
                    self.error = f"注册窗口类失败: {exc}"
                    return
            self._hwnd = win32gui.CreateWindowEx(0, class_name, "性能悬浮窗托盘", 0, 0, 0, 0, 0,
                                                 HWND_MESSAGE, 0, hinst, None)
        except Exception as exc:
            self.error = f"创建托盘窗口失败: {exc}"
            return
        if not self._hwnd:
            self.error = "创建托盘窗口返回空句柄"
            return

        self._nid = (self._hwnd, 1, NIF_MESSAGE | NIF_ICON | NIF_TIP,
                     WM_TRAY, hicon, self.tooltip[:127])
        try:
            win32gui.Shell_NotifyIcon(NIM_ADD, self._nid)
        except Exception as exc:
            self.error = f"添加托盘图标失败: {exc}"
            return
        self.available = True
        try:
            win32gui.PumpMessages()                          # 直到 WM_DESTROY 后 PostQuitMessage
        except Exception as exc:
            self.error = f"托盘消息循环异常: {exc}"
        finally:
            try:
                win32gui.Shell_NotifyIcon(NIM_DELETE, self._nid)
            except Exception:
                pass
            self.available = False

    # ------------------------------------------------------------------ 回调
    def _wndproc(self, hwnd, msg, wparam, lparam):
        import win32con
        import win32gui

        WM_TRAY = _const(win32con, "WM_USER", 0x0400) + 7
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event == win32con.WM_LBUTTONUP:
                self.out.put("settings")
            elif event == WM_LBUTTONDBLCLK:
                self.out.put("toggle_visible")
            elif event in (win32con.WM_RBUTTONUP, WM_CONTEXTMENU):
                self._popup_menu()
            return 0
        if msg in (win32con.WM_DESTROY, win32con.WM_CLOSE):
            try:
                win32gui.DestroyWindow(hwnd)
            except Exception:
                pass
            win32gui.PostQuitMessage(0)
            self._alive = False
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _popup_menu(self) -> None:
        import win32api
        import win32gui

        menu = win32gui.CreatePopupMenu()
        for index, (name, label) in enumerate(MENU_ITEMS):
            if name is None:
                win32gui.AppendMenu(menu, MF_SEPARATOR, 0, "")
            else:
                win32gui.AppendMenu(menu, MF_STRING, index + 1, label)
        try:
            x, y = win32api.GetCursorPos()
            win32gui.SetForegroundWindow(self._hwnd)          # 不置前会导致菜单无法正常收起
            chosen = win32gui.TrackPopupMenu(
                menu, TPM_LEFTALIGN | TPM_RETURNCMD | TPM_NONOTIFY, x, y, 0, self._hwnd, None)
            action = self._ids.get(int(chosen or 0))
            if action:
                self.out.put(action)
        except Exception as exc:
            self.error = f"托盘菜单异常: {type(exc).__name__}"
        finally:
            try:
                win32gui.DestroyMenu(menu)
            except Exception:
                pass

if __name__ == "__main__":
    q: queue.Queue = queue.Queue()
    tray = TrayIcon(q, "性能悬浮窗 · 托盘测试")
    tray.start()
    import time

    for _ in range(20):
        time.sleep(0.5)
        while not q.empty():
            print("动作:", q.get())
        if tray.error:
            print("错误:", tray.error)
            break
    print("available =", tray.available)
    tray.stop()
    time.sleep(0.5)
    print("已移除托盘")
