# -*- coding: utf-8 -*-
"""游戏性能实时悬浮窗。

* 背景透明（color key），标签与数值分色，每项数值颜色可单独设置
* 无边框、置顶、不抢焦点；右键任意位置打开设置面板，滚轮调整字号
* 位置：可拖动文字移动，另提供移动模式、四角锚点与 X/Y 坐标
* 硬件访问仅在运行期间进行，退出即释放温度驱动与帧率子进程，无常驻组件

入口：pythonw perf_overlay.pyw
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import colorchooser, font as tkfont, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fpsmon import FpsMonitor                     # noqa: E402
from sensors import SensorHub, is_admin           # noqa: E402

try:
    from tray import TrayIcon                      # noqa: E402
except Exception:                                  # pywin32 缺失时仍可运行
    TrayIcon = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "运行日志.txt")

# 透明键色。抗锯齿文字的边像素是字色与键色的混合值，只有与键色完全相同的像素才会被抠掉，
# 因此键色取近黑色：混合结果表现为文字自身的暗色描边，在任何画面上都清晰。
# 界面取色若与该色过于接近会被自动挪开，避免文字被当成背景抠掉。
TRANSKEY = "#010101"
DASH = "--"

# 文字统一使用宋体：小字号下为点阵渲染，可读性最好。
# 系统里没有宋体时按顺序回退，保证不会出现缺字形方块。
FONT_CANDIDATES = ("宋体", "SimSun", "NSimSun", "Microsoft YaHei UI", "Microsoft YaHei")
FIXED_FONT = "宋体"

# --------------------------------------------------------------------------- #
# Windows 常量
# --------------------------------------------------------------------------- #
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
GW_HWNDPREV = 3
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
ERROR_ALREADY_EXISTS = 183

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_NULL = 0x0312, 0x0000

HOTKEY_DEFAULTS: dict[str, str] = {
    "toggle_visible": "CTRL+ALT+H",
    "toggle_click_through": "CTRL+ALT+SHIFT+P",
    "settings": "CTRL+ALT+SHIFT+C",
    "move_mode": "CTRL+ALT+M",
    "font_smaller": "CTRL+ALT+-",
    "font_bigger": "CTRL+ALT+=",
}
HOTKEY_LABELS = {
    "toggle_visible": "显示 / 隐藏",
    "toggle_click_through": "点击穿透",
    "settings": "打开设置",
    "move_mode": "移动模式（拖动定位）",
    "font_smaller": "字号减小",
    "font_bigger": "字号增大",
}
MOD_NAMES = {"CTRL": MOD_CONTROL, "ALT": MOD_ALT, "SHIFT": MOD_SHIFT, "WIN": MOD_WIN}
VK_NAMES = {"SPACE": 0x20, "ESC": 0x1B, "TAB": 0x09, "ENTER": 0x0D, "BACKSPACE": 0x08,
            "INSERT": 0x2D, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23,
            "PAGEUP": 0x21, "PAGEDOWN": 0x22, "LEFT": 0x25, "UP": 0x26,
            "RIGHT": 0x27, "DOWN": 0x28, "-": 0xBD, "MINUS": 0xBD, "=": 0xBB,
            "EQUALS": 0xBB, "PLUS": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
            ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0}
for _i in range(1, 13):
    VK_NAMES[f"F{_i}"] = 0x70 + _i - 1

def parse_hotkey(spec: str) -> tuple[int, int] | None:
    """把 "CTRL+ALT+SHIFT+P" 解析成 (修饰键, 虚拟键码)。"""
    if not spec:
        return None
    parts = [p.strip().upper() for p in str(spec).replace(" ", "").split("+") if p.strip()]
    if not parts:
        return None
    mods = MOD_NOREPEAT
    key = parts[-1]
    for part in parts[:-1]:
        if part in MOD_NAMES:
            mods |= MOD_NAMES[part]
        else:
            return None
    if len(key) == 1:
        return mods, ord(key)
    if key in VK_NAMES:
        return mods, VK_NAMES[key]
    return None

# --------------------------------------------------------------------------- #
# 指标定义
# --------------------------------------------------------------------------- #
def _v(vals, key):
    value = vals.get(key)
    if value is None:
        return None
    try:
        if value != value:                # NaN
            return None
    except Exception:
        return None
    return value

class Metric:
    def __init__(self, key, name, short, color, requires, fmt, sample,
                 default_on=True, primary=None, threshold=None, unit=""):
        self.key, self.name, self.short, self.color = key, name, short, color
        self.requires, self.fmt, self.sample = requires, fmt, sample
        self.default_on = default_on
        self.primary = primary            # 用于阈值比较的主数值
        self.threshold = threshold        # 默认阈值（None 表示这项不参与变色）
        self.unit = unit

def _temp(app, value):
    """按设置输出温度。"""
    if app.cfg.get("temp_unit", "c") == "f":
        return value * 9.0 / 5.0 + 32.0, "℉"
    return value, "℃"

METRICS: list[Metric] = [
    Metric("cpu_temp", "CPU温度", "CPU℃", "#ff6b6b", ("cpu_temp",),
           lambda v, app: "{:.0f}{}".format(*_temp(app, _v(v, "cpu_temp"))), "888℃",
           primary=lambda v: _v(v, "cpu_temp"), threshold=85),
    Metric("cpu_load", "CPU占用", "CPU%", "#ffa94d", ("cpu_load",),
           lambda v, app: f"{_v(v,'cpu_load'):.0f}%", "888%",
           primary=lambda v: _v(v, "cpu_load"), threshold=90),
    Metric("gpu_temp", "GPU温度", "GPU℃", "#4dabf7", ("gpu_temp",),
           lambda v, app: "{:.0f}{}".format(*_temp(app, _v(v, "gpu_temp"))), "888℃",
           primary=lambda v: _v(v, "gpu_temp"), threshold=80),
    Metric("gpu_load", "GPU占用", "GPU%", "#63e6be", ("gpu_load",),
           lambda v, app: f"{_v(v,'gpu_load'):.0f}%", "888%"),
    Metric("mem", "内存", "内存", "#e79ef2", ("mem_used", "mem_total"),
           lambda v, app: (f"{_v(v,'mem_percent'):.0f}%"
                           if app.cfg.get("mem_mode") == "percent"
                           else f"{_v(v,'mem_used'):.1f}/{_v(v,'mem_total'):.1f}G"),
           "88.8/888.8G"),
    Metric("fps", "帧率", "FPS", "#ffd43b", ("fps",),
           lambda v, app: (f"{_v(v,'fps'):.0f}"
                           + (f" 低{v.get('_fps_low'):.0f}" if app.cfg.get("fps_show_low")
                              and v.get("_fps_low") else "")),
           "888 低888", primary=lambda v: _v(v, "fps"), threshold=30),
    Metric("cpu_clock", "CPU频率", "GHz", "#a9e34b", ("cpu_clock",),
           lambda v, app: f"{_v(v,'cpu_clock'):.2f}GHz", "88.88GHz", default_on=False),
    Metric("cpu_power", "CPU功耗", "CPU W", "#f783ac", ("cpu_power",),
           lambda v, app: f"{_v(v,'cpu_power'):.0f}W", "888W", default_on=False),
    Metric("gpu_mem", "显存", "显存", "#b197fc", ("gpu_mem_used", "gpu_mem_total"),
           lambda v, app: f"{_v(v,'gpu_mem_used')/1024:.1f}/{_v(v,'gpu_mem_total')/1024:.1f}G",
           "88.8/888.8G", default_on=False),
    Metric("gpu_power", "GPU功耗", "GPU W", "#66d9e8", ("gpu_power",),
           lambda v, app: f"{_v(v,'gpu_power'):.0f}W", "888W", default_on=False),
]
METRIC_BY_KEY = {m.key: m for m in METRICS}

DEFAULT_CONFIG: dict = {
    "font_size": 8,
    "label_font": FIXED_FONT,
    "value_font": FIXED_FONT,
    "label_color": "#ffffff",
    "separator": "：",
    "compact_labels": False,
    "line_spacing": 1.0,
    "hgap": 14,
    "layout": "vertical",
    "bg_mode": "transparent",
    "panel_color": "#0b0b0b",
    "text_shadow": True,
    "shadow_color": "#000000",
    "opacity": 1.0,
    "always_on_top": True,
    "click_through": False,
    "hotkeys_enabled": True,
    "interval_ms": 1000,
    "temp_unit": "c",
    "cpu_temp_source": "auto",
    "mem_mode": "gb",
    "fps_follow": "foreground",
    "fps_window_ms": 1500,
    "fps_show_low": False,
    "warn_enabled": True,
    "warn_color": "#ff2d2d",
    "thresholds": {m.key: m.threshold for m in METRICS if m.threshold is not None},
    "position": None,
    "enabled": {m.key: m.default_on for m in METRICS},
    "colors": {m.key: m.color for m in METRICS},
    "hotkeys": dict(HOTKEY_DEFAULTS),
    "warned": [],
}

def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass

# --------------------------------------------------------------------------- #
# 全局热键
# --------------------------------------------------------------------------- #
class HotkeyThread(threading.Thread):
    """独立线程 + 自己的消息循环，不去抢 Tk 的消息队列。"""

    def __init__(self, out: queue.Queue, specs: dict[str, str]) -> None:
        super().__init__(daemon=True, name="hotkeys")
        self.out = out
        self.specs = dict(specs)
        self.ids: dict[int, str] = {}
        self.registered: dict[str, str] = {}
        self.failed: dict[str, str] = {}
        self._alive = True
        self._tid = 0

    def run(self) -> None:
        msg = wintypes.MSG()
        # RegisterHotKey(NULL, ...) 将 WM_HOTKEY 投递给线程消息队列，队列必须先建立
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)      # PM_NOREMOVE
        handles = []
        for index, (name, spec) in enumerate(self.specs.items(), start=1):
            parsed = parse_hotkey(spec)
            if not parsed:
                self.failed[name] = f"{spec}（格式无法识别）"
                continue
            mods, vk = parsed
            try:
                ok = user32.RegisterHotKey(None, index, mods, vk)
            except Exception as exc:
                self.failed[name] = f"{spec}（{type(exc).__name__}）"
                continue
            if ok:
                self.ids[index] = name
                self.registered[name] = spec
                handles.append(index)
            else:
                self.failed[name] = f"{spec}（与其他程序冲突，错误码 {kernel32.GetLastError()}）"
        if not handles:
            return
        self._tid = kernel32.GetCurrentThreadId()
        while self._alive:
            try:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            except Exception:
                break
            if got in (0, -1, None):
                break
            if msg.message == WM_HOTKEY:
                name = self.ids.get(int(msg.wParam))
                if name:
                    self.out.put(name)
        for hid in handles:
            try:
                user32.UnregisterHotKey(None, hid)
            except Exception:
                pass

    def stop(self) -> None:
        self._alive = False
        if self._tid:
            try:
                user32.PostThreadMessageW(self._tid, WM_NULL, 0, 0)
            except Exception:
                pass

# --------------------------------------------------------------------------- #
# 主程序
# --------------------------------------------------------------------------- #
class Overlay:
    def __init__(self) -> None:
        self.cfg = self._load_config()
        self.hub = SensorHub(self.cfg["interval_ms"], self.cfg.get("cpu_temp_source", "auto"))
        self.fpsmon = FpsMonitor(self.cfg["fps_window_ms"], self.cfg["fps_follow"])
        self.hot_out: queue.Queue = queue.Queue()
        self.hot_thread: HotkeyThread | None = None
        self.tray = None
        self.settings_win: tk.Toplevel | None = None
        self._save_job: str | None = None
        self._visible = True
        self._drag = None
        self._move_mode = False
        self._saved_bg = None
        self._vars: dict[str, tk.BooleanVar] = {}
        self._string_vars: dict[str, tk.StringVar] = {}
        self._label_font = None
        self._value_font = None
        self._font_family = None
        self.items: list[tuple[str, list[int], list[int], dict]] = []
        self._last_texts: dict[str, str] = {}
        self._last_colors: dict[str, str] = {}
        self._target_name = None
        self._exiting = False

        self.root = tk.Tk()
        self.root.title("性能悬浮窗")
        self.root.overrideredirect(True)
        self.root.configure(bg=TRANSKEY)
        self.root.wm_attributes("-transparentcolor", TRANSKEY)
        self.root.attributes("-topmost", self.cfg["always_on_top"])
        self.canvas = tk.Canvas(self.root, bg=TRANSKEY, highlightthickness=0, bd=0,
                                takefocus=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        self.hwnd = None
        self.root.update_idletasks()
        self._grab_hwnd()
        self._apply_styles()
        self._bind()
        self.rebuild()
        self._place_from_config()

        self.hub.start()
        st = self.hub.status
        log(f"启动 pid={os.getpid()} 管理员={st.get('admin')} LHM={st.get('lhm')} "
            f"热区={st.get('thermal')}/{st.get('thermal_channel')} 热区名={st.get('thermal_zone')} "
            f"CPU温度来源={st.get('cpu_temp_from') or st.get('thermal_error')} "
            f"显卡={st.get('gpu_name')}")
        if self.cfg["enabled"].get("fps"):
            self.fpsmon.start()
        self._start_hotkeys()
        self._start_tray()

        self.root.after(120, self._tick)
        self.root.after(80, self._poll_hotkeys)
        self.root.after(2000, self._watch_zorder)

    # -------------------------------------------------------------- 配置
    def _load_config(self) -> dict:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                saved = json.load(fh)
            for k, v in saved.items():
                if k in ("enabled", "colors", "hotkeys", "thresholds") and isinstance(v, dict):
                    cfg[k].update({kk: vv for kk, vv in v.items() if kk in cfg[k]})
                elif k in cfg:
                    cfg[k] = v
        except FileNotFoundError:
            pass
        except Exception as exc:
            log(f"读取配置失败: {exc}")
        cfg["font_size"] = max(6, min(18, int(cfg.get("font_size", 8))))
        cfg["line_spacing"] = max(0.8, min(2.0, float(cfg.get("line_spacing", 1.0))))
        cfg["opacity"] = max(0.3, min(1.0, float(cfg.get("opacity", 1.0))))
        cfg["hgap"] = max(2, min(60, int(cfg.get("hgap", 14))))
        # 字体锁定：忽略旧配置里存的其他字体，避免不同机器上表现不一致
        cfg["label_font"] = FIXED_FONT
        cfg["value_font"] = FIXED_FONT
        return cfg

    def save(self, immediate: bool = False) -> None:
        if self._save_job:
            self.root.after_cancel(self._save_job)
            self._save_job = None

        def write() -> None:
            self._save_job = None
            try:
                parts = self.root.winfo_geometry().split("+")
                if len(parts) == 3:
                    self.cfg["position"] = [int(parts[1]), int(parts[2])]
            except Exception:
                pass
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                    json.dump(self.cfg, fh, ensure_ascii=False, indent=2)
            except Exception as exc:
                log(f"保存配置失败: {exc}")

        if immediate:
            write()
        else:
            self._save_job = self.root.after(500, write)

    # -------------------------------------------------------------- 窗口样式
    def _grab_hwnd(self) -> None:
        try:
            self.root.update_idletasks()
            self.hwnd = int(self.root.frame(), 16) or int(self.root.wm_frame(), 16)
        except Exception:
            self.hwnd = None

    def _apply_styles(self) -> None:
        if not self.hwnd:
            return
        try:
            style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
            style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            style |= WS_EX_TRANSPARENT if self.cfg["click_through"] else ~WS_EX_TRANSPARENT & style
            if not self.cfg["click_through"]:
                style &= ~WS_EX_TRANSPARENT
            user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style)
            user32.SetWindowPos(self.hwnd, 0, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                                | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        except Exception as exc:
            log(f"设置窗口样式失败: {exc}")
        self._apply_bg()

    def _apply_bg(self) -> None:
        """透明模式和底板模式切换；整体透明度用 SetLayeredWindowAttributes 同时保留 color key。"""
        panel = self.cfg.get("bg_mode") == "panel"
        alpha = max(0.3, min(1.0, float(self.cfg.get("opacity", 1.0))))
        try:
            if panel:
                color = self.cfg.get("panel_color", "#0b0b0b")
                self.root.attributes("-transparentcolor", "")
                self.root.configure(bg=color)
                self.canvas.configure(bg=color)
            else:
                self.root.attributes("-transparentcolor", TRANSKEY)
                self.root.configure(bg=TRANSKEY)
                self.canvas.configure(bg=TRANSKEY)
        except Exception as exc:
            log(f"背景设置失败: {exc}")
        try:
            if self.hwnd:
                if panel:
                    self.root.attributes("-alpha", alpha)
                elif alpha < 1.0:
                    key = (int(TRANSKEY[5:7], 16) | (int(TRANSKEY[3:5], 16) << 8)
                           | (int(TRANSKEY[1:3], 16) << 16))
                    user32.SetLayeredWindowAttributes(self.hwnd, key, int(alpha * 255), 0x01 | 0x02)
                else:
                    self.root.attributes("-alpha", 1.0)
        except Exception as exc:
            log(f"透明度设置失败: {exc}")

    def _set_topmost(self) -> None:
        try:
            self.root.attributes("-topmost", bool(self.cfg["always_on_top"]))
            if self.cfg["always_on_top"] and self.hwnd:
                user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            pass

    def _watch_zorder(self) -> None:
        """置顶带的顺序可能被系统重排：样式位还在，但窗口已经掉到普通窗口下面。
        每 2 秒沿 z 序往上查一次，只要发现有非置顶的可见窗口压在自己上面就重申置顶。
        只改 z 序，位置和大小都不动，用户无感。"""
        try:
            if self.cfg.get("always_on_top") and self.hwnd and self._visible:
                if self._is_covered():
                    user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0,
                                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                    now = time.time()
                    if now - getattr(self, "_z_log_at", 0.0) > 60:
                        self._z_log_at = now
                        log("检测到悬浮窗被普通窗口压住，已重申置顶")
        except Exception as exc:
            log(f"置顶检查失败: {type(exc).__name__}: {exc}")
        self.root.after(2000, self._watch_zorder)

    def _is_covered(self) -> bool:
        """沿 GW_HWNDPREV 往 z 序上方走：遇到非置顶的可见大窗口即视为被压住。"""
        hwnd = user32.GetWindow(self.hwnd, GW_HWNDPREV)
        for _ in range(200):
            if not hwnd:
                return False
            if bool(user32.IsWindowVisible(hwnd)):
                ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
                if not (ex & WS_EX_TOPMOST):
                    r = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(r))
                    if (r.right - r.left) * (r.bottom - r.top) > 4096:
                        return True
            hwnd = user32.GetWindow(hwnd, GW_HWNDPREV)
        return False

    # -------------------------------------------------------------- 字体
    def _resolve_font(self) -> str:
        """宋体优先；系统里真没有就按顺序退，结果缓存住。"""
        cached = getattr(self, "_font_family", None)
        if cached:
            return cached
        try:
            families = set(tkfont.families(self.root))
        except Exception:
            families = set()
        for name in FONT_CANDIDATES:
            if name in families or name.lower() in {f.lower() for f in families}:
                self._font_family = name
                return name
        self._font_family = FIXED_FONT
        return FIXED_FONT

    def _fonts(self) -> tuple[tkfont.Font, tkfont.Font]:
        """复用两个命名字体，只更新属性，不重复创建字体实例。"""
        size = int(self.cfg["font_size"])
        family = self._resolve_font()
        if self._label_font is None or self._value_font is None:
            names = set(tkfont.names(self.root))
            for name, attr in (("PerfOverlayLabel", "_label_font"), ("PerfOverlayValue", "_value_font")):
                font = tkfont.Font(name=name, exists=(name in names), root=self.root)
                setattr(self, attr, font)
        self._label_font.configure(family=family, size=size)
        self._value_font.configure(family=family, size=size)
        return self._label_font, self._value_font

    def _label_of(self, metric: Metric) -> str:
        name = metric.short if self.cfg.get("compact_labels") else metric.name
        return name + self.cfg.get("separator", "：")

    def active_metrics(self) -> list[Metric]:
        active = [m for m in METRICS if self.cfg["enabled"].get(m.key, m.default_on)]
        return active or [METRIC_BY_KEY["cpu_load"]]

    def rebuild(self) -> None:
        self.canvas.delete("all")
        self.items = []
        self._last_texts.clear()
        self._last_colors.clear()
        lf, vf = self._fonts()
        active = self.active_metrics()
        pad = max(1, round(int(self.cfg["font_size"]) * 0.5))
        gap = max(3, round(int(self.cfg["font_size"]) * 0.7))
        glyph_h = max(lf.metrics("linespace"), vf.metrics("linespace"))
        # 宋体的实际字高等于 linespace，所以行高必须额外留间隙，否则每行字贴字，
        # 小字号时整块糊成一团（6 磅最明显）。间隙按字高比例给，并保证至少 1px。
        row_h = max(glyph_h + max(1, round(glyph_h * 0.18)),
                    round(glyph_h * float(self.cfg.get("line_spacing", 1.0))))

        slots = []
        for m in active:
            label = self._label_of(m)
            slots.append({"metric": m, "label": label,
                          "label_w": lf.measure(label),
                          "value_w": max(vf.measure(m.sample), vf.measure(DASH))})
        label_col_w = max(s["label_w"] for s in slots)

        if self.cfg["layout"] == "horizontal":
            item_gap = max(2, min(60, int(self.cfg.get("hgap", 14))))
            x = pad
            for s in slots:
                s["lx"], s["vx"], s["y"] = x, x + s["label_w"] + 1, pad + row_h / 2
                x = s["vx"] + s["value_w"] + item_gap
            x_total, y_total = x - item_gap + pad, row_h + pad * 2
        else:
            x_total = pad + label_col_w + gap + max(s["value_w"] for s in slots) + pad
            y_total = pad * 2 + row_h * len(slots)
            for i, s in enumerate(slots):
                s["lx"] = pad
                s["vx"] = pad + label_col_w + gap
                s["y"] = pad + row_h * i + row_h / 2

        shadow = bool(self.cfg.get("text_shadow", True)) and self.cfg.get("bg_mode") != "panel"
        shadow_color = self.cfg.get("shadow_color", "#000000")
        for s in slots:
            m = s["metric"]
            lids = self._text(s["lx"], s["y"], s["label"], lf, self.cfg["label_color"],
                              shadow, shadow_color)
            vids = self._text(s["vx"], s["y"], DASH, vf, self.cfg["colors"].get(m.key, m.color),
                              shadow, shadow_color)
            self.items.append((m.key, lids, vids, s))

        self.canvas.configure(width=x_total, height=y_total)
        self._resize_keep_pos(x_total, y_total)
        self.canvas.configure(cursor="fleur" if not self.cfg["click_through"] else "")

    def _text(self, x, y, text, font, fill, shadow: bool, shadow_color: str) -> list[int]:
        """透明背景下垫一层 1px 深色副本，任何游戏画面上都能看清。"""
        ids: list[int] = []
        if shadow:
            ids.append(self.canvas.create_text(x + 1, y + 1, text=text, font=font,
                                               fill=shadow_color, anchor="w"))
        ids.append(self.canvas.create_text(x, y, text=text, font=font, fill=fill, anchor="w"))
        return ids

    def _resize_keep_pos(self, w: int, h: int) -> None:
        try:
            self.root.geometry(f"{w}x{h}+{self.root.winfo_x()}+{self.root.winfo_y()}")
        except Exception:
            self.root.geometry(f"{w}x{h}")

    def virtual_desktop(self) -> tuple[int, int, int, int]:
        try:
            return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
                    user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))
        except Exception:
            return 0, 0, 1920, 1080

    def _place_from_config(self) -> None:
        try:
            w = int(self.canvas.cget("width")) or 120
            h = int(self.canvas.cget("height")) or 30
        except Exception:
            w, h = 120, 30
        pos = self.cfg.get("position")
        vx, vy, vw, vh = self.virtual_desktop()
        if not pos:
            pos = [vx + max(8, int(vw * 0.02)), vy + max(8, int(vh * 0.02))]
        self.place_at(int(pos[0]), int(pos[1]), w, h)

    def place_at(self, x: int, y: int, w: int | None = None, h: int | None = None) -> None:
        vx, vy, vw, vh = self.virtual_desktop()
        try:
            w = w or int(self.canvas.cget("width"))
            h = h or int(self.canvas.cget("height"))
        except Exception:
            return
        x = max(vx, min(x, vx + vw - w - 2))
        y = max(vy, min(y, vy + vh - h - 2))
        try:
            self.root.geometry(f"+{int(x)}+{int(y)}")
        except Exception:
            pass
        self.save()

    def anchor(self, where: str) -> None:
        vx, vy, vw, vh = self.virtual_desktop()
        w = int(self.canvas.cget("width"))
        h = int(self.canvas.cget("height"))
        m = 12
        pos = {"tl": (vx + m, vy + m), "tr": (vx + vw - w - m, vy + m),
               "bl": (vx + m, vy + vh - h - m), "br": (vx + vw - w - m, vy + vh - h - m),
               "c": (vx + (vw - w) // 2, vy + (vh - h) // 2)}.get(where, (vx + m, vy + m))
        self.place_at(*pos, w, h)

    # -------------------------------------------------------------- 交互
    def _bind(self) -> None:
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Button-3>", lambda _e: self.open_settings())
        self.canvas.bind("<Button-2>", lambda _e: self.open_settings())
        self.canvas.bind("<MouseWheel>", self._wheel)

    def _drag_start(self, ev) -> None:
        if self.cfg["click_through"]:
            return
        self._drag = (ev.x_root - self.root.winfo_x(), ev.y_root - self.root.winfo_y())

    def _drag_move(self, ev) -> None:
        if not self._drag or self.cfg["click_through"]:
            return
        self.root.geometry(f"+{ev.x_root - self._drag[0]}+{ev.y_root - self._drag[1]}")

    def _drag_end(self, _ev) -> None:
        self._drag = None
        self.save()

    def _wheel(self, ev) -> None:
        self.change_font(1 if ev.delta > 0 else -1)

    def change_font(self, delta: int) -> None:
        size = max(6, min(18, int(self.cfg["font_size"]) + delta))
        if size == self.cfg["font_size"]:
            return
        self.cfg["font_size"] = size
        self.rebuild()
        self.save(immediate=True)
        self._flash(f"字号 {size}")

    def _flash(self, text: str) -> None:
        """右下角短暂提示，不弹框、不打断游戏。"""
        try:
            item = self.canvas.create_text(int(self.canvas.cget("width")) - 4,
                                           int(self.canvas.cget("height")) - 3,
                                           text=text, fill="#ffffff", anchor="se",
                                           font=self._fonts()[0], tags="flash")
            self.root.after(1000, lambda: self.canvas.delete(item))
        except Exception:
            pass

    # -------------------------------------------------------------- 移动模式
    def toggle_move_mode(self) -> None:
        self._move_mode = not self._move_mode
        if self._move_mode:
            self._saved_bg = self.cfg.get("bg_mode")
            self.cfg["bg_mode"] = "panel"
            self._apply_bg()
            self.rebuild()
            if self.cfg["click_through"]:
                self.cfg["click_through"] = False
                self._apply_styles()
            self._flash("移动模式：整块可拖")
            self.canvas.itemconfigure(tk.ALL, tags="")     # 触发重绘
            self._show_move_hint()
        else:
            self.cfg["bg_mode"] = self._saved_bg or "transparent"
            self._saved_bg = None
            self._apply_bg()
            self.rebuild()
            self.save()
            self._flash("位置已保存")

    def _show_move_hint(self) -> None:
        try:
            self.canvas.create_text(2, -1, text="按住任意位置拖动 · 再按 Ctrl+Alt+M 结束",
                                    fill="#ffd43b", anchor="nw", font=self._fonts()[0],
                                    tags="movehint")
        except Exception:
            pass

    # -------------------------------------------------------------- 开关
    def toggle_click_through(self) -> None:
        self.cfg["click_through"] = not self.cfg["click_through"]
        self._apply_styles()
        self.rebuild()
        self.save()
        self._flash("点击穿透：" + ("开" if self.cfg["click_through"] else "关"))

    def toggle_topmost(self) -> None:
        self.cfg["always_on_top"] = not self.cfg["always_on_top"]
        self._set_topmost()
        self.save()

    def toggle_visible(self) -> None:
        self._visible = not self._visible
        if self._visible:
            self.root.deiconify()
            self._apply_styles()
            self._set_topmost()
        else:
            self.root.withdraw()

    # -------------------------------------------------------------- 热键 / 托盘
    def _start_hotkeys(self) -> None:
        if not self.cfg.get("hotkeys_enabled", True):
            return
        specs = dict(HOTKEY_DEFAULTS)
        specs.update({k: v for k, v in self.cfg.get("hotkeys", {}).items()
                      if k in HOTKEY_DEFAULTS})
        self.cfg["hotkeys"] = specs
        self.hot_thread = HotkeyThread(self.hot_out, specs)
        self.hot_thread.start()
        time.sleep(0.3)
        if self.hot_thread.failed:
            log("热键注册失败: " + "; ".join(f"{k}={v}" for k, v in self.hot_thread.failed.items()))

    def _start_tray(self) -> None:
        self.tray = None
        if TrayIcon is None:
            return
        try:
            self.tray = TrayIcon(self.hot_out, "性能悬浮窗（左键设置 / 右键菜单 / 双击隐藏）")
            self.tray.start()
        except Exception as exc:
            self.tray = None
            log(f"托盘启动失败: {type(exc).__name__}: {exc}")

    def _poll_hotkeys(self) -> None:
        if self._exiting:
            return
        try:
            while True:
                name = self.hot_out.get_nowait()
                if name == "toggle_visible":
                    self.toggle_visible()
                elif name == "toggle_click_through":
                    self.toggle_click_through()
                elif name == "move_mode":
                    self.toggle_move_mode()
                elif name == "settings":
                    if not self._visible:
                        self.toggle_visible()
                    self.open_settings()
                elif name == "font_bigger":
                    self.change_font(1)
                elif name == "font_smaller":
                    self.change_font(-1)
                elif name == "quit":
                    self.quit()
        except queue.Empty:
            pass
        except Exception as exc:
            log(f"热键分发异常: {exc}")
        try:
            self.root.after(120, self._poll_hotkeys)
        except Exception:
            pass

    # -------------------------------------------------------------- 刷新
    def _tick(self) -> None:
        if self._exiting:
            return
        try:
            self._refresh()
        except tk.TclError:
            return                      # 窗口正在销毁，不用记日志
        except Exception as exc:
            log(f"刷新异常: {type(exc).__name__}: {exc}")
        try:
            self.root.after(200, self._tick)
        except Exception:
            pass

    def _refresh(self) -> None:
        vals = self.hub.read()
        if self.cfg["enabled"].get("fps"):
            fps, target, low = self.fpsmon.snapshot()
            if fps is not None:
                vals["fps"] = fps
                vals["_fps_low"] = low
            self._target_name = target
        for key, _lids, vids, _slot in self.items:
            metric = METRIC_BY_KEY[key]
            missing = any(_v(vals, k) is None for k in metric.requires)
            text = DASH if missing else ""
            if not missing:
                try:
                    text = metric.fmt(vals, self)
                except Exception:
                    text = DASH
            color = self._value_color(metric, vals, missing)
            if text != self._last_texts.get(key) or color != self._last_colors.get(key):
                # vids 里第一个是阴影层，最后一个是主层。阴影必须保持暗色，
                # 否则描边失效、小字号下笔画会发糊。
                for item in vids:
                    self.canvas.itemconfig(item, text=text)
                self.canvas.itemconfig(vids[-1], fill=color)
                self._last_texts[key] = text
                self._last_colors[key] = color

    def _value_color(self, metric: Metric, vals: dict, missing: bool) -> str:
        base = self.cfg["colors"].get(metric.key, metric.color)
        if missing or not self.cfg.get("warn_enabled", True) or metric.primary is None:
            return base
        threshold = self.cfg.get("thresholds", {}).get(metric.key)
        if threshold is None:
            return base
        value = metric.primary(vals)
        if value is None:
            return base
        if metric.key == "fps":                      # 帧率是低于阈值报警
            return self.cfg.get("warn_color", "#ff2d2d") if value < 30 else base
        return self.cfg.get("warn_color", "#ff2d2d") if value > threshold else base

    # -------------------------------------------------------------- 设置面板
    def open_settings(self) -> None:
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.lift()
            self.settings_win.attributes("-topmost", True)
            self._sync_vars()
            return
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("性能悬浮窗 · 设置")
        win.configure(padx=8, pady=6)
        win.attributes("-topmost", True)
        win.resizable(False, False)

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True)
        tab_items = ttk.Frame(nb, padding=10)
        tab_look = ttk.Frame(nb, padding=10)
        tab_fps = ttk.Frame(nb, padding=10)
        tab_pos = ttk.Frame(nb, padding=10)
        tab_info = ttk.Frame(nb, padding=10)
        for tab, label in ((tab_items, "显示项"), (tab_look, "外观"), (tab_fps, "帧率与采样"),
                           (tab_pos, "位置与热键"), (tab_info, "状态")):
            nb.add(tab, text=label)

        self._build_items_tab(tab_items)
        self._build_look_tab(tab_look)
        self._build_fps_tab(tab_fps)
        self._build_pos_tab(tab_pos)
        self._build_info_tab(tab_info)

        bottom = ttk.Frame(win)
        bottom.pack(fill="x", pady=(6, 0))
        ttk.Button(bottom, text="恢复默认", command=self._reset_all).pack(side="left")
        ttk.Button(bottom, text="打开所在文件夹", command=self._open_dir).pack(side="left", padx=6)
        ttk.Button(bottom, text="退出程序", command=self.quit).pack(side="right")
        ttk.Button(bottom, text="关闭", command=win.destroy).pack(side="right", padx=6)
        win.protocol("WM_DELETE_WINDOW", lambda: (self.save(immediate=True), win.destroy()))
        self._sync_vars()
        win.after(1200, lambda: self._update_status(win))

    # ---- 各标签页 ----
    def _build_items_tab(self, parent) -> None:
        head = ttk.Frame(parent)
        head.pack(fill="x")
        ttk.Label(head, text="勾选要显示的项目；左边色块=数值颜色，点一下可以换。",
                  foreground="#555").pack(side="left")
        box = ttk.Frame(parent)
        box.pack(fill="x", pady=(8, 0))
        for m in METRICS:
            row = ttk.Frame(box)
            row.pack(fill="x", pady=2)
            en_var = self._bool_var(m.key, "enabled")
            ttk.Checkbutton(row, text=f"{m.name}    （紧凑模式显示为 {m.short}）",
                            variable=en_var,
                            command=lambda k=m.key, v=en_var: self._set_enabled(k, v.get())
                            ).pack(side="left")
            sw = tk.Label(row, width=6, height=1, relief="solid", bd=1,
                          bg=self.cfg["colors"][m.key], cursor="hand2")
            sw.pack(side="right")
            sw.bind("<Button-1>", lambda _e, k=m.key, w=sw: self._pick_color(k, w))

        extra = ttk.LabelFrame(parent, text="可读性")
        extra.pack(fill="x", pady=(10, 0))
        for label, key in (("紧凑标签（CPU℃ / CPU% / FPS，占用更小）", "compact_labels"),
                           ("超阈值时数值变红提醒", "warn_enabled")):
            var = self._bool_var(key)
            ttk.Checkbutton(extra, text=label, variable=var,
                            command=lambda k=key, v=var: self._set_flag(k, v.get())
                            ).pack(anchor="w", padx=8, pady=2)

        warnrow = ttk.Frame(extra)
        warnrow.pack(fill="x", padx=8, pady=(2, 4))
        ttk.Label(warnrow, text="变红时用的颜色").pack(side="left")
        wsw = tk.Label(warnrow, width=6, relief="solid", bd=1,
                       bg=self.cfg["warn_color"], cursor="hand2")
        wsw.pack(side="left", padx=(4, 0))
        wsw.bind("<Button-1>", lambda _e: self._pick_color("__warn__", wsw))

        th = ttk.LabelFrame(parent, text="变红阈值（温度/占用超过时提醒，帧率低于时提醒）")
        th.pack(fill="x", pady=(8, 0))
        grid = ttk.Frame(th)
        grid.pack(fill="x", padx=8, pady=6)
        for i, (key, label) in enumerate((("cpu_temp", "CPU温度℃"), ("gpu_temp", "GPU温度℃"),
                                          ("cpu_load", "CPU占用%"))):
            cell = ttk.Frame(grid)
            cell.pack(side="left", padx=(0, 14))
            ttk.Label(cell, text=label).pack(anchor="w")
            var = tk.StringVar(value=str(self.cfg["thresholds"].get(key, "")))
            sp = ttk.Spinbox(cell, from_=1, to=120, textvariable=var, width=6)
            sp.pack(anchor="w")
            sp.bind("<FocusOut>", lambda _e, k=key, v=var: self._set_threshold(k, v))
            sp.bind("<Return>", lambda _e, k=key, v=var: self._set_threshold(k, v))
        cell = ttk.Frame(grid)
        cell.pack(side="left")
        ttk.Label(cell, text="帧率低于").pack(anchor="w")
        low_var = tk.StringVar(value=str(self.cfg["thresholds"].get("fps", 30)))
        sp = ttk.Spinbox(cell, from_=1, to=240, increment=5, textvariable=low_var, width=6)
        sp.pack(anchor="w")
        sp.bind("<FocusOut>", lambda _e, v=low_var: self._set_threshold("fps", v))
        sp.bind("<Return>", lambda _e, v=low_var: self._set_threshold("fps", v))

        src = ttk.LabelFrame(parent, text="CPU 温度来源（改完重启程序生效）")
        src.pack(fill="x", pady=(10, 0))
        srcrow = ttk.Frame(src)
        srcrow.pack(fill="x", padx=8, pady=4)
        self._radio(srcrow, "来源", [("自动", "auto"), ("仅驱动", "lhm"),
                                    ("仅热区", "acpi"), ("不读", "off")],
                    "cpu_temp_source")
        ttk.Label(src, foreground="#555", justify="left", text=(
            "自动：优先使用 LibreHardwareMonitor 的驱动读数；不可用时改用\n"
            "ACPI 热区温度。热区走系统性能计数器，普通权限也能读；\n"
            "驱动读数需要管理员权限。取不到时显示 --，不会重复上次的值。")
            ).pack(anchor="w", padx=8, pady=(0, 6))

    def _build_look_tab(self, parent) -> None:
        grid = ttk.Frame(parent)
        grid.pack(fill="x")
        self._spin(grid, 0, 0, "字号(磅)", 6, 18, 1,
                   lambda: self.cfg["font_size"],
                   lambda v: self._set("font_size", int(v)))
        self._spin(grid, 0, 2, "行距倍数", 0.8, 2.0, 0.1,
                   lambda: self.cfg["line_spacing"],
                   lambda v: self._set("line_spacing", round(float(v), 2)))
        self._spin(grid, 1, 0, "整体透明度", 0.3, 1.0, 0.05,
                   lambda: self.cfg["opacity"],
                   lambda v: self._set("opacity", round(float(v), 2), bg=True))
        self._spin(grid, 1, 2, "横排项间距", 2, 60, 1,
                   lambda: self.cfg.get("hgap", 14),
                   lambda v: self._set("hgap", int(v)),
                   extra="像素，仅横排布局生效")

        rows = ttk.Frame(parent)
        rows.pack(fill="x", pady=(10, 0))
        self._radio(rows, "排列", [("竖排", "vertical"), ("横排", "horizontal")], "layout")
        self._radio(rows, "背景", [("全透明", "transparent"), ("深色底板", "panel")], "bg_mode", bg=True)
        self._radio(rows, "温度单位", [("℃", "c"), ("℉", "f")], "temp_unit")
        self._radio(rows, "内存显示", [("用量", "gb"), ("百分比", "percent")], "mem_mode")

        colors = ttk.LabelFrame(parent, text="颜色")
        colors.pack(fill="x", pady=(10, 0))

        # 文字描边开关放在这里：它决定笔画锐利还是带暗边，和阴影颜色是同一件事
        svar = self._bool_var("text_shadow")
        self._shadow_check = ttk.Checkbutton(
            colors, text="文字描边（笔画外垫 1px 暗边，亮画面上更清晰；关掉后笔画更锐利）",
            variable=svar,
            command=lambda v=svar: (self._set_flag("text_shadow", v.get()),
                                    self._sync_shadow_swatch()))
        self._shadow_check.pack(anchor="w", padx=8, pady=(6, 0))

        crow = ttk.Frame(colors)
        crow.pack(fill="x", padx=8, pady=4)
        for label, key in (("标签颜色", "label_color"), ("底板颜色", "panel_color"),
                           ("阴影颜色", "shadow_color")):
            ttk.Label(crow, text=label).pack(side="left")
            sw = tk.Label(crow, width=6, relief="solid", bd=1, bg=self.cfg[key], cursor="hand2")
            sw.pack(side="left", padx=(2, 12))
            sw.bind("<Button-1>", lambda _e, k=key, w=sw: self._pick_color_guarded(k, w))
            if key == "shadow_color":
                self._shadow_sw = sw
        self._sync_shadow_swatch()

        # 实时预览：改任何外观都能马上看到效果
        prev = ttk.LabelFrame(parent, text="实时预览")
        prev.pack(fill="x", pady=(10, 0))
        self.preview = tk.Canvas(prev, height=72, highlightthickness=0, bd=0)
        self.preview.pack(fill="x", padx=6, pady=6)
        self._draw_preview()

    def _build_fps_tab(self, parent) -> None:
        box = ttk.LabelFrame(parent, text="帧率")
        box.pack(fill="x")
        rows = ttk.Frame(box)
        rows.pack(fill="x", padx=8, pady=6)
        self._radio(rows, "跟随", [("前台窗口（游戏）", "foreground"),
                                   ("当前出帧最多的进程", "busiest")], "fps_follow",
                    apply=self.fpsmon.set_follow)
        grid = ttk.Frame(box)
        grid.pack(fill="x", padx=8, pady=(0, 6))
        self._spin(grid, 0, 0, "统计窗口ms", 500, 4000, 250,
                   lambda: self.cfg["fps_window_ms"],
                   lambda v: self._set("fps_window_ms", int(v)),
                   apply=self.fpsmon.set_window_ms, extra="数值越大越平稳，越小越灵敏")
        low_var = self._bool_var("fps_show_low")
        ttk.Checkbutton(grid, text="同时显示 1% Low（最低 1% 帧的平均值）",
                        variable=low_var,
                        command=lambda v=low_var: self._set_flag("fps_show_low", v.get())
                        ).grid(row=0, column=3, padx=12)

        box2 = ttk.LabelFrame(parent, text="采样与资源")
        box2.pack(fill="x", pady=(10, 0))
        g2 = ttk.Frame(box2)
        g2.pack(fill="x", padx=8, pady=6)
        self._spin(g2, 0, 0, "采样间隔秒", 0.5, 5.0, 0.5,
                   lambda: round(self.cfg["interval_ms"] / 1000.0, 1),
                   lambda v: self._set("interval_ms", int(round(float(v) * 1000))),
                   apply=lambda val: self.hub.set_interval_ms(int(float(val) * 1000)),
                   extra="间隔越大越省资源")
        ttk.Label(box2, foreground="#555", justify="left", text=(
            "关闭「帧率」时不会启动 presentmon 子进程，整体占用约 0.5% CPU。\n"
            "温度/占用来自 LibreHardwareMonitor（进程内调用，不安装任何组件）；\n"
            "CPU 占用与内存来自 psutil。")).pack(anchor="w", padx=8, pady=(0, 6))

    def _build_pos_tab(self, parent) -> None:
        head = ttk.LabelFrame(parent, text="位置")
        head.pack(fill="x")
        row = ttk.Frame(head)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Button(row, text="进入进入移动模式（临时显示底板，便于拖动）",
                   command=self.toggle_move_mode).pack(side="left")
        ttk.Label(head, foreground="#555", justify="left", text=(
            "说明：背景透明时，只有绘制了内容的像素接收鼠标事件，空白处会穿透到下层窗口。\n"
            "因此拖动需按住文字本身；精确摆位可使用下面的移动模式、锚点或坐标。")
            ).pack(anchor="w", padx=8, pady=(0, 6))

        anchors = ttk.Frame(head)
        anchors.pack(fill="x", padx=8, pady=(0, 6))
        for label, where in (("左上", "tl"), ("右上", "tr"), ("左下", "bl"),
                             ("右下", "br"), ("居中", "c")):
            ttk.Button(anchors, text=label, width=6,
                       command=lambda w=where: self.anchor(w)).pack(side="left", padx=(0, 6))
        coord = ttk.Frame(head)
        coord.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(coord, text="X").pack(side="left")
        xvar = tk.StringVar(value="0")
        ttk.Entry(coord, textvariable=xvar, width=7).pack(side="left", padx=(2, 10))
        ttk.Label(coord, text="Y").pack(side="left")
        yvar = tk.StringVar(value="0")
        ttk.Entry(coord, textvariable=yvar, width=7).pack(side="left", padx=(2, 10))

        def apply_xy(*_a):
            try:
                self.place_at(int(xvar.get()), int(yvar.get()))
            except ValueError:
                pass

        for v in (xvar, yvar):
            v.trace_add("write", lambda *_a: apply_xy())
        self._pos_vars = (xvar, yvar)
        self._sync_pos_vars()

        hk = ttk.LabelFrame(parent, text="全局热键（修改后需重启程序生效）")
        hk.pack(fill="x", pady=(10, 0))
        grid = ttk.Frame(hk)
        grid.pack(fill="x", padx=8, pady=6)
        for i, (name, desc) in enumerate(HOTKEY_LABELS.items()):
            cell = ttk.Frame(grid)
            cell.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 14), pady=2)
            ttk.Label(cell, text=desc, width=16).pack(side="left")
            var = tk.StringVar(value=self.cfg["hotkeys"].get(name, ""))

            def on_hk(k=name, v=var):
                spec = v.get().strip().upper()
                if not parse_hotkey(spec):
                    v.set(self.cfg["hotkeys"].get(k, ""))
                    return
                self.cfg["hotkeys"][k] = spec
                self.save()

            entry = ttk.Entry(cell, textvariable=var, width=20)
            entry.pack(side="left")
            entry.bind("<FocusOut>", on_hk)
            entry.bind("<Return>", on_hk)
        ttk.Label(hk, foreground="#555", text="若与其他程序冲突，在此改成未占用的组合，"
                                              "支持 CTRL/ALT/SHIFT/WIN + 字母数字符号或 F1-F12。"
              ).pack(anchor="w", padx=8, pady=(0, 6))

        flags = ttk.Frame(parent)
        flags.pack(fill="x", pady=(10, 0))
        for label, key, fn in (("窗口置顶", "always_on_top", self._set_topmost),
                               ("鼠标点击穿透（点不到悬浮窗）", "click_through", self._apply_styles),
                               ("启用全局热键（重启后生效）", "hotkeys_enabled", None)):
            var = self._bool_var(key)

            def on_flag(k=key, v=var, f=fn):
                self.cfg[k] = bool(v.get())
                if f:
                    f()
                if k == "click_through":
                    self.rebuild()
                self.save()

            ttk.Checkbutton(flags, text=label, variable=var, command=on_flag).pack(anchor="w")

    def _build_info_tab(self, parent) -> None:
        self.info = tk.Text(parent, height=16, wrap="word", bd=1, relief="solid",
                            font=(self.cfg["label_font"], 9))
        self.info.pack(fill="both", expand=True)
        self.info.insert("1.0", self._status_text())
        self.info.configure(state="disabled")
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="打开运行日志", command=self._open_log).pack(side="left")
        ttk.Button(row, text="重新检测一次", command=lambda: self._update_status(None)
                   ).pack(side="left", padx=6)

    # ---- 小部件 helpers ----
    def _bool_var(self, key: str, store: str | None = None) -> tk.BooleanVar:
        """store=None 读 cfg[key]；store="enabled" 读 cfg["enabled"][key]。"""
        vid = f"{store}:{key}" if store else key
        var = self._vars.get(vid)
        if store == "enabled":
            current = bool(self.cfg["enabled"].get(key, METRIC_BY_KEY[key].default_on))
        else:
            current = bool(self.cfg.get(key, False))
        if var is None:
            var = tk.BooleanVar(value=current)
            self._vars[vid] = var
        return var

    def _set_flag(self, key: str, value: bool, rebuild: bool = True) -> None:
        if self.cfg.get(key) == value:
            return
        self.cfg[key] = value
        if key == "click_through":
            self._apply_styles()
        elif key == "always_on_top":
            self._set_topmost()
        if rebuild:
            self.rebuild()
            self._draw_preview()
        self.save()

    def _set_enabled(self, key: str, on: bool) -> None:
        on = bool(on)
        if bool(self.cfg["enabled"].get(key)) == on:
            return
        self.cfg["enabled"][key] = on
        if key == "fps":
            if on:
                self.fpsmon.start()
            else:
                self.fpsmon.stop()
        self.rebuild()
        self._draw_preview()
        self.save()

    def _spin(self, parent, row, col, label, lo, hi, step, get, setv, apply=None, extra=""):
        cell = ttk.Frame(parent)
        cell.grid(row=row, column=col, sticky="w", padx=(0, 16), pady=2)
        ttk.Label(cell, text=label, width=12).pack(side="left")
        var = tk.StringVar(value=str(get()))

        def commit(*_a):
            try:
                value = float(var.get()) if step != 1 else int(float(var.get()))
            except ValueError:
                var.set(str(get()))
                return
            setv(value)
            if apply:
                apply(value)
            self._apply(rebuild=True)

        def refresh(*_a):
            """获得焦点时先从配置读回当前值，避免用面板中的旧快照覆盖新设置。"""
            try:
                var.set(str(get()))
            except Exception:
                pass

        sp = ttk.Spinbox(cell, from_=lo, to=hi, increment=step, textvariable=var, width=7,
                         command=commit)
        sp.pack(side="left")
        sp.bind("<FocusIn>", refresh)
        sp.bind("<FocusOut>", commit)
        sp.bind("<Return>", commit)
        if extra:
            ttk.Label(cell, text=extra, foreground="#777").pack(side="left", padx=(4, 0))

    def _radio(self, parent, label, options, key, bg: bool = False, apply=None):
        cell = ttk.Frame(parent)
        cell.pack(anchor="w", pady=3)
        ttk.Label(cell, text=label, width=10).pack(side="left")
        var = tk.StringVar(value=str(self.cfg.get(key)))
        self._string_vars[key] = var

        def on_pick():
            self.cfg[key] = var.get()
            if apply:
                apply(var.get())
            self._apply(rebuild=True, bg=bg)

        for text, value in options:
            ttk.Radiobutton(cell, text=text, value=value, variable=var,
                            command=on_pick).pack(side="left", padx=(0, 8))

    def _apply(self, rebuild: bool = False, bg: bool = False) -> None:
        if bg:
            self._apply_bg()
        if rebuild:
            self.rebuild()
            self._draw_preview()
        # 设置项改动直接落盘（配置文件很小），不经防抖，避免退出时丢失。
        self.save(immediate=True)

    def _set(self, key, value, bg: bool = False, apply=None) -> None:
        if self.cfg.get(key) == value:
            return
        self.cfg[key] = value
        if apply:
            apply(value)
        self._apply(rebuild=True, bg=bg)

    def _set_threshold(self, key, var) -> None:
        try:
            value = int(float(var.get()))
        except ValueError:
            var.set(str(self.cfg["thresholds"].get(key, "")))
            return
        self.cfg["thresholds"][key] = value
        self.save()

    def _draw_preview(self) -> None:
        """设置面板中的样例预览：棋盘格表示透明区域，高度随字号自适应。"""
        try:
            self.preview.delete("all")
            lf, vf = self._fonts()
            glyph_h = max(lf.metrics("linespace"), vf.metrics("linespace"))
            row_h = max(glyph_h + max(1, round(glyph_h * 0.18)),
                        round(glyph_h * float(self.cfg.get("line_spacing", 1.0))))
            samples = [("CPU温度", "82℃", self.cfg["colors"].get("cpu_temp", "#ff6b6b")),
                       ("帧率", "144  低120", self.cfg["colors"].get("fps", "#ffd43b"))]
            height = row_h * len(samples) + 12
            self.preview.configure(height=height)
            cell = 8
            for yy in range(0, height, cell):
                for xx in range(0, 420, cell):
                    tone = "#c4c4c4" if ((xx // cell) + (yy // cell)) % 2 == 0 else "#eeeeee"
                    self.preview.create_rectangle(xx, yy, xx + cell, yy + cell,
                                                  fill=tone, outline=tone)
            shadow = bool(self.cfg.get("text_shadow", True))
            sep = self.cfg.get("separator", "：")
            y = 6 + row_h // 2
            for label, value, color in samples:
                text = label + sep
                if shadow:
                    self.preview.create_text(6, y + 1, text=text, font=lf,
                                             fill=self.cfg.get("shadow_color", "#000000"), anchor="w")
                self.preview.create_text(5, y, text=text, font=lf,
                                         fill=self.cfg["label_color"], anchor="w")
                width = lf.measure(text)
                if shadow:
                    self.preview.create_text(5 + width + 9, y + 1, text=value, font=vf,
                                             fill=self.cfg.get("shadow_color", "#000000"), anchor="w")
                self.preview.create_text(5 + width + 8, y, text=value, font=vf,
                                         fill=color, anchor="w")
                y += row_h
        except Exception:
            pass

    @staticmethod
    def _safe_color(hexa: str) -> str:
        """离透明键色太近的颜色会被抠成透明，自动挪开一点。"""
        try:
            r = int(hexa[1:3], 16); g = int(hexa[3:5], 16); b = int(hexa[5:7], 16)
            kr = int(TRANSKEY[1:3], 16); kg = int(TRANSKEY[3:5], 16); kb = int(TRANSKEY[5:7], 16)
            if abs(r - kr) <= 6 and abs(g - kg) <= 6 and abs(b - kb) <= 6:
                return "#%02x%02x%02x" % (min(255, kr + 24), min(255, kg + 24), min(255, kb + 24))
        except Exception:
            pass
        return hexa

    def _pick_color_guarded(self, key: str, widget) -> None:
        """阴影描边关掉时，阴影颜色不再可点，避免调了没效果。"""
        if key == "shadow_color" and not bool(self.cfg.get("text_shadow", True)):
            return
        self._pick_color(key, widget)

    def _sync_shadow_swatch(self) -> None:
        widget = getattr(self, "_shadow_sw", None)
        if widget is None:
            return
        on = bool(self.cfg.get("text_shadow", True))
        widget.configure(relief="solid" if on else "raised",
                         cursor="hand2" if on else "arrow")

    def _pick_color(self, key: str, swatch) -> None:
        target = {"__warn__": "warn_color", "__label__": "label_color"}.get(key, key)
        current = self.cfg.get(target, "#ffffff")
        rgb, hexa = colorchooser.askcolor(initialcolor=current,
                                          parent=self.settings_win or self.root,
                                          title="选择颜色")
        if not hexa:
            return
        hexa = self._safe_color(hexa)
        if target == "label_color":
            self.cfg["label_color"] = hexa
        elif target in ("warn_color", "panel_color", "shadow_color"):
            self.cfg[target] = hexa
        else:
            self.cfg["colors"][target] = hexa
        swatch.configure(bg=hexa)
        self._apply(rebuild=True, bg=target in ("panel_color", "shadow_color"))

    def _sync_vars(self) -> None:
        for vid, var in self._vars.items():
            try:
                if ":" in vid:
                    store, key = vid.split(":", 1)
                    current = bool(self.cfg["enabled"].get(key, METRIC_BY_KEY[key].default_on))
                else:
                    current = bool(self.cfg.get(vid, False))
                var.set(current)
            except Exception:
                pass
        for key, var in self._string_vars.items():
            try:
                var.set(str(self.cfg.get(key)))
            except Exception:
                pass
        self._sync_pos_vars()

    def _sync_pos_vars(self) -> None:
        if not getattr(self, "_pos_vars", None):
            return
        try:
            xvar, yvar = self._pos_vars
            xvar.set(str(self.root.winfo_x()))
            yvar.set(str(self.root.winfo_y()))
        except Exception:
            pass

    def _update_status(self, win) -> None:
        try:
            if getattr(self, "info", None) and self.info.winfo_exists():
                self.info.configure(state="normal")
                self.info.delete("1.0", "end")
                self.info.insert("1.0", self._status_text())
                self.info.configure(state="disabled")
            self._sync_pos_vars()
        except Exception:
            return
        try:
            if win is not None and win.winfo_exists():
                win.after(1500, lambda: self._update_status(win))
        except Exception:
            pass

    def _status_text(self) -> str:
        st = self.hub.status
        lines = []
        lines.append(f"权限：{'管理员' if st.get('admin') else '普通用户（驱动读数不可用，CPU 温度改用热区计数器）'}")
        if st.get("lhm"):
            hw = st.get("lhm_hw") or {}
            detail = "；".join(f"{k} {v}" for k, v in hw.items())
            if st.get("lhm_stalled"):
                lines.append("温度/占用：LibreHardwareMonitor 采集无响应，本轮已跳过（不影响其他项目）"
                             + (f"；{detail}" if detail else ""))
            else:
                lines.append(f"温度/占用：LibreHardwareMonitor 已加载，显卡 {st.get('gpu_name') or '未知'}"
                             + (f"；{detail}" if detail else ""))
        else:
            lines.append(f"温度/占用：LibreHardwareMonitor 不可用（{st.get('lhm_error')}）"
                         + ("，GPU 已改用 nvidia-smi" if st.get("nvsmi") else "")
                         + (f"（{st.get('nvsmi_error')}）" if st.get("nvsmi_error") else ""))
        from_ = st.get("cpu_temp_from")
        if from_ == "lhm":
            lines.append("CPU温度：来自 LibreHardwareMonitor 驱动读数。")
        elif st.get("thermal"):
            note = f"；最近一次读取失败：{st.get('thermal_error')}" if st.get("thermal_error") else ""
            lines.append(f"CPU温度：来自 ACPI 热区（通道 {st.get('thermal_channel')}，"
                         f"实例 {st.get('thermal_zone')}）{note}")
        elif not st.get("admin"):
            lines.append("CPU温度：无可用来源。驱动读数需要管理员权限，"
                         "请关掉本程序后双击「启动(管理员).bat」。")
        else:
            lines.append("CPU温度：两个数据源均无返回值（"
                         f"LHM={st.get('lhm_error')}，热区={st.get('thermal_error')}）")
        stale = st.get("stale_keys") or []
        if stale:
            names = "、".join(METRIC_BY_KEY[k].name for k in stale if k in METRIC_BY_KEY)
            lines.append(f"读数长时间没有变化的项目：{names} —— 数据源未在刷新，已按无数据处理显示 --")
        fstat = self.fpsmon.status
        if not self.cfg["enabled"].get("fps"):
            lines.append("帧率：已关闭（勾选后才会启动 presentmon.exe 子进程）")
        elif fstat.get("running"):
            lines.append(f"帧率：采集中，目标进程 {self._target_name or '未识别'}"
                         f"（子进程 {fstat.get('child_pid')}，已统计 {fstat.get('rows_seen')} 帧）")
            lines.append(f"    当前出帧最多的进程：{fstat.get('busiest')}")
            lines.append(f"    各进程帧率：{fstat.get('per_pid')}")
        else:
            lines.append(f"帧率：未运行（{fstat.get('error') or '等待数据'}）")
        tray_ok = getattr(self, "tray", None) and self.tray.available
        lines.append("托盘图标：" + ("已就绪（左键=设置，右键=菜单，双击=隐藏）。Win11 可能收在右下角 ^ 里。"
                                    if tray_ok else
                                    f"不可用（{getattr(self.tray, 'error', None) or 'pywin32 未安装'}）"))
        if self.hot_thread:
            if self.hot_thread.failed:
                lines.append("以下热键与其他程序冲突，可在「位置与热键」中修改："
                             + "; ".join(f"{HOTKEY_LABELS.get(k, k)}→{v}"
                                         for k, v in self.hot_thread.failed.items()))
            else:
                lines.append("热键全部生效：" + "，".join(self.hot_thread.registered.values()))
        elif not self.cfg.get("hotkeys_enabled", True):
            lines.append("热键已关闭。")
        lines.append(f"配置：{CONFIG_PATH}")
        lines.append(f"日志：{LOG_PATH}")
        return "\n".join(lines)

    def _reset_all(self) -> None:
        if not messagebox.askyesno("恢复默认", "把显示项、颜色、字号、位置等全部恢复成默认值？",
                                   parent=self.settings_win or self.root):
            return
        keep_pos = None
        self.cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        self.cfg["position"] = keep_pos
        self._vars.clear()
        self._string_vars.clear()
        self.hub.set_interval_ms(self.cfg["interval_ms"])
        self.fpsmon.set_window_ms(self.cfg["fps_window_ms"])
        self.fpsmon.set_follow(self.cfg["fps_follow"])
        self._apply_bg()
        self.rebuild()
        self._place_from_config()
        self.save(immediate=True)
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.destroy()
            self.settings_win = None
        self.open_settings()

    def _open_log(self) -> None:
        try:
            if not os.path.exists(LOG_PATH):
                with open(LOG_PATH, "w", encoding="utf-8") as fh:
                    fh.write("暂无异常记录。\n")
            os.startfile(LOG_PATH)
        except Exception as exc:
            log(f"打开日志失败: {exc}")

    @staticmethod
    def _open_dir() -> None:
        try:
            os.startfile(BASE_DIR)
        except Exception:
            pass

    # -------------------------------------------------------------- 退出
    def quit(self) -> None:
        if self._exiting:
            return
        self._exiting = True
        try:
            self.save(immediate=True)
            if self.hot_thread:
                self.hot_thread.stop()
            if getattr(self, "tray", None):
                try:
                    self.tray.stop()
                    self.tray.join(timeout=2.0)
                except Exception:
                    pass
            self.fpsmon.stop()
            self.hub.stop()
        except Exception as exc:
            log(f"退出清理异常: {exc}")
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    def run(self) -> None:
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            if not self._exiting:
                self.quit()

# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
_single_handle = None

def ensure_single_instance() -> bool:
    global _single_handle
    try:
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, "Local\\QoderPerfOverlay")
        _single_handle = handle
        return kernel32.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True

def preflight(cfg: dict) -> list[str]:
    """缺什么直接说清楚，只提醒一次。"""
    from fpsmon import PM_PATH as _PM
    from sensors import VENDOR_DIR as _VENDOR

    problems: list[str] = []
    warned = set(cfg.get("warned", []))
    try:
        import psutil  # noqa: F401
    except Exception:
        if "psutil" not in warned:
            problems.append("psutil 没装 → CPU占用、内存 显示 --")
    try:
        import clr  # noqa: F401
    except Exception:
        if "pythonnet" not in warned:
            problems.append("pythonnet 没装 → CPU/GPU 温度与 GPU占用 显示 --")
    if not os.path.isdir(_VENDOR):
        problems.append(f"缺少 {_VENDOR}（LibreHardwareMonitor 的 dll）→ 温度类项目不可用")
    if cfg["enabled"].get("fps") and not os.path.exists(_PM) and "presentmon" not in warned:
        problems.append(f"缺少 {_PM} → 帧率不可用")
    if problems:
        cfg["warned"] = sorted(warned | {p.split(" ")[0] for p in problems})
    return problems

def main() -> int:
    if not ensure_single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("已在运行", "性能悬浮窗已经在一个实例里运行了。\n"
                                      "可以点任务栏右下角的托盘图标（左键=设置，右键=菜单）。")
        root.destroy()
        return 0
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
        cfg.update({k: v for k, v in saved.items() if k in cfg})
        cfg["enabled"] = {**DEFAULT_CONFIG["enabled"], **saved.get("enabled", {})}
    except Exception:
        pass
    problems = preflight(cfg)
    if problems:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("缺少组件", "以下项目会显示 --：\n\n· " + "\n· ".join(problems)
                               + "\n\n双击「安装依赖.bat」可以自动补齐 Python 库。")
        root.destroy()
    try:
        app = Overlay()
    except Exception as exc:
        import traceback
        log("启动失败:\n" + traceback.format_exc())
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("启动失败", f"{type(exc).__name__}: {exc}\n\n详见 运行日志.txt")
        root.destroy()
        return 1
    app.run()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
