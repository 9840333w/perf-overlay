# -*- coding: utf-8 -*-
"""帧率采集：以子进程方式运行 Intel PresentMon（ETW 逐帧上报），统计目标进程的呈现次数。

PresentMon 读取 DXGI / D3D9 / D3D12 的 Present 事件，窗口、无边框与独占全屏模式均可覆盖。
DXGI 桌面复制的帧计数在部分显卡驱动上会高于实际呈现次数，且无法覆盖独占全屏，因此不采用。

仅在启用「帧率」时启动子进程；关闭该项或退出程序时子进程立即结束，不保留后台进程。
"""

from __future__ import annotations

import csv
import ctypes
import io
import os
import sys
import re
import subprocess
import threading
import time
from collections import defaultdict
from ctypes import wintypes

# 冻结成 exe 后 __file__ 指向包内路径，配置、日志与 vendor 必须与 exe 同级
BASE_DIR = (os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
PM_PATH = os.path.join(BASE_DIR, "vendor", "presentmon", "presentmon.exe")
SESSION_PREFIX = "PerfOverlayFps"          # 本程序会话名前缀，用于识别自身遗留的会话与子进程

CREATE_NO_WINDOW = 0x08000000
_KERNEL32 = ctypes.windll.kernel32
_USER32 = ctypes.windll.user32
_PROCESS_QUERY_LIMITED = 0x1000

def foreground_pid() -> int:
    """当前前台窗口（游戏）所属进程 ID。"""
    try:
        hwnd = _USER32.GetForegroundWindow()
        if not hwnd:
            return 0
        pid = wintypes.DWORD()
        _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0

def pid_name(pid: int) -> str | None:
    """PresentMon 非管理员下会把部分进程显示成 <unknown>，这里自己补一次。"""
    if not pid:
        return None
    try:
        handle = _KERNEL32.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if _KERNEL32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            _KERNEL32.CloseHandle(handle)
    except Exception:
        return None
    return None

def _run_hidden(args, timeout=15):
    return subprocess.run(args, capture_output=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW)

def sweep_leaked_sessions(prefix: str = SESSION_PREFIX) -> list[str]:
    """清理异常退出遗留的 ETW 会话。

    残留会话会导致新的采集启动失败或收不到内核事件。logman 为 Windows 自带命令。
    """
    stopped: list[str] = []
    try:
        out = _run_hidden(["logman", "query", "-ets"])
        text = out.stdout.decode("gbk", "ignore")
    except Exception:
        return stopped
    names = set()
    for line in text.splitlines():
        head = line.strip().split()
        if head and head[0].startswith(prefix):
            names.add(head[0])
    for name in sorted(names):
        try:
            _run_hidden(["logman", "stop", name, "-ets"], timeout=10)
            stopped.append(name)
        except Exception:
            pass
    return stopped

def sweep_orphans() -> int:
    """结束异常退出遗留的 presentmon 子进程。

    仅处理命令行中包含本程序会话名的进程，不影响用户自行运行的 PresentMon。
    """
    killed = 0
    try:
        import psutil
    except Exception:
        return 0
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            info = proc.info
            name = (info.get("name") or "").lower()
            cmd = " ".join(info.get("cmdline") or [])
            if info["pid"] == me or "presentmon" not in name:
                continue
            if SESSION_PREFIX in cmd:
                proc.terminate()
                try:
                    proc.wait(2)
                except Exception:
                    proc.kill()
                killed += 1
        except Exception:
            continue
    return killed

class FpsMonitor:
    """follow=foreground：以前台窗口进程为目标，取不到时回退到同名进程中出帧最多的；
    follow=busiest：以当前出帧最多的进程为目标。"""

    def __init__(self, window_ms: int = 1500, follow: str = "foreground",
                 exe_path: str = PM_PATH) -> None:
        self.exe_path = exe_path
        self.window_ms = max(500, int(window_ms))
        self.follow = follow
        self.status: dict[str, object] = {"running": False, "error": None, "target": None,
                                          "child_pid": None, "restarts": 0,
                                          "last_exit_code": None, "rows_seen": 0}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._events: dict[tuple[int, str], list[float]] = defaultdict(list)
        self._names: dict[int, str] = {}
        self._name_cache_at: dict[int, float] = {}
        self._now_ms: float | None = None
        self._session = f"{SESSION_PREFIX}-{os.getpid()}"

    # ------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        if not os.path.exists(self.exe_path):
            self.status["error"] = f"缺少 {self.exe_path}"
            return False
        self._stop.clear()
        self.status["error"] = None
        self._restarts = 0
        swept_procs = sweep_orphans()
        swept_sessions = sweep_leaked_sessions()
        if swept_procs or swept_sessions:
            self.status["cleaned"] = f"进程 {swept_procs} 个 / 会话 {len(swept_sessions)} 个"
        self._thread = threading.Thread(target=self._supervisor, daemon=True, name="fpsmon")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            self._terminate_child(proc)
        th = self._thread
        self._thread = None
        if th and th.is_alive():
            th.join(timeout=5.0)
        with self._lock:
            self._events.clear()
            self._names.clear()
            self._now_ms = None
        self.status["running"] = False
        self.status["child_pid"] = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def set_window_ms(self, window_ms: int) -> None:
        self.window_ms = max(500, int(window_ms))

    def set_follow(self, follow: str) -> None:
        self.follow = follow

    # ------------------------------------------------------------ 子进程
    def _supervisor(self) -> None:
        self._restarts = 0
        while not self._stop.is_set():
            started = time.perf_counter()
            code = self._run_once()
            self.status["last_exit_code"] = code
            if self._stop.is_set():
                break
            ran = time.perf_counter() - started
            if ran > 20:                       # 运行较长时间后退出（休眠/驱动重置）：不计入失败次数
                self._restarts = 0
            else:
                self._restarts += 1
                if self._restarts >= 6:
                    self.status["error"] = (self.status.get("error")
                                            or f"PresentMon 多次异常退出（最后退出码 {code}），已停止帧率采集")
                    break
            self._stop.wait(1.5 if self._restarts else 0.2)
        self.status["running"] = False
        self.status["child_pid"] = None

    def _run_once(self) -> int | None:
        args = [self.exe_path,
                "--output_stdout", "--no_console_stats",
                "--no_track_gpu", "--no_track_input",
                "--session_name", self._session]
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            self.status["error"] = f"无法启动 presentmon.exe: {type(exc).__name__}: {exc}"
            return None

        self._proc = proc
        self.status["running"] = True
        self.status["child_pid"] = proc.pid
        # 新会话的 TimeInMs 从 0 重新开始，需要清空时间轴
        with self._lock:
            self._events.clear()
            self._names.clear()
            self._now_ms = None

        err_thread = threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True)
        err_thread.start()
        try:
            stream = io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace", newline="")
            self._consume(stream)
        except Exception as exc:
            self.status["error"] = f"读取 PresentMon 输出失败: {type(exc).__name__}: {exc}"
        finally:
            self._terminate_child(proc)
            self._proc = None
            self.status["running"] = False
        return proc.returncode

    def _terminate_child(self, proc) -> None:
        """优先使用 PresentMon 的会话停止通道；直接结束进程会遗留 ETW 会话，影响后续采集。
        """
        if proc.poll() is not None:
            return
        try:
            _run_hidden([self.exe_path, "--session_name", self._session,
                         "--terminate_existing_session"], timeout=10)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
            return
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _drain_stderr(self, proc) -> None:
        """读取 PresentMon 的 stderr；仅在异常退出时记录为错误提示。"""
        try:
            data = proc.stderr.read() if proc.stderr else b""
        except Exception:
            return
        text = " ".join(data.decode("utf-8", "ignore").split())
        if not text:
            return
        self.status["stderr"] = text[-300:]
        if proc.poll() not in (0, None) and "already running" not in text:
            self.status["error"] = text[-200:]

    def _consume(self, stream) -> None:
        idx: dict[str, int] = {}
        rows = 0
        for fields in csv.reader(stream):
            if self._stop.is_set():
                return
            if not fields:
                continue
            if not idx:
                if fields[0] != "Application":
                    continue
                idx = {name: i for i, name in enumerate(fields)}
                if "ProcessID" not in idx or "TimeInMs" not in idx:
                    self.status["error"] = "PresentMon 输出列不符合预期"
                    return
                continue
            try:
                pid = int(fields[idx["ProcessID"]])
                now = float(fields[idx["TimeInMs"]])
            except (ValueError, IndexError):
                continue
            rows += 1
            # 一个进程可能持有多条交换链（多显示器、叠加层），按链分组统计才不会重复计数
            chain = fields[idx["SwapChainAddress"]] if "SwapChainAddress" in idx and \
                len(fields) > idx["SwapChainAddress"] else "0"
            name = fields[idx["Application"]] if len(fields) > idx["Application"] else ""
            with self._lock:
                self._events[(pid, chain)].append(now)
                if name and name != "<unknown>" and self._names.get(pid) != name:
                    self._names[pid] = name
                if self._now_ms is None or now > self._now_ms:
                    self._now_ms = now
                self.status["rows_seen"] = rows

    # ------------------------------------------------------------ 读数
    def snapshot(self) -> tuple[float | None, str | None, float | None]:
        """返回 (帧率, 目标进程名, 1% Low)；尚未收到数据时帧率为 None，界面显示 --。"""
        window_ms = float(self.window_ms)
        with self._lock:
            if self._now_ms is None:
                return None, None, None
            cut = self._now_ms - window_ms          # TimeInMs 与窗口都用毫秒
            # 每个进程取最快的一条交换链作为该进程的帧率
            per_pid: dict[int, float] = {}
            best_chain: dict[int, str] = {}
            for (pid, chain), ts in self._events.items():
                if len(ts) > 6000:
                    del ts[:-3000]
                rate = sum(1 for t in ts if t > cut) * 1000.0 / window_ms
                if rate >= per_pid.get(pid, 0):
                    per_pid[pid] = rate
                    best_chain[pid] = chain
            if not per_pid:
                return None, None, None
            busiest = max(per_pid, key=lambda p: per_pid[p])
            target: int | None
            if self.follow == "busiest":
                target = busiest
            else:
                fp = foreground_pid()
                target = fp if per_pid.get(fp) else None
                if target is None and fp:
                    # 前台为启动器/浏览器主进程时，实际出帧的通常是其同名子进程
                    fg_name = self._resolve_name(fp).lower()
                    if fg_name:
                        same = [p for p, r in per_pid.items()
                                if r > 0 and self._resolve_name(p).lower() == fg_name]
                        if same:
                            target = max(same, key=lambda p: per_pid[p])
                if target is None:
                    target = 0
            name = self._resolve_name(target) if target else None
            self.status["target"] = name
            self.status["busiest"] = f"{self._resolve_name(busiest)} {per_pid[busiest]:.0f}"
            self.status["per_pid"] = {
                f"{p}:{self._resolve_name(p)[:14]}": round(r, 1)
                for p, r in sorted(per_pid.items(), key=lambda x: -x[1])[:6]}
            if target == 0:
                return 0.0, None, None
            low = self._one_percent_low(target, best_chain.get(target))
            return per_pid.get(target, 0.0), name, low

    def _one_percent_low(self, pid: int, chain) -> float | None:
        """1% Low：更长窗口内最慢 1% 帧间隔的平均值，用于反映卡顿程度。"""
        if chain is None:
            return None
        ts = self._events.get((pid, chain)) or []
        span_ms = max(3000.0, float(self.window_ms) * 2)
        cut = (self._now_ms or 0) - span_ms
        times = sorted(t for t in ts if t > cut)
        if len(times) < 12:
            return None
        gaps = [b - a for a, b in zip(times, times[1:]) if 0 < b - a < 500]
        if len(gaps) < 10:
            return None
        gaps.sort()
        worst = gaps[:max(1, int(round(len(gaps) * 0.01)))]
        avg_worst = sum(worst) / len(worst)
        return 1000.0 / avg_worst if avg_worst > 0 else None

    def _resolve_name(self, pid: int) -> str:
        """进程名缓存，避免高频调用 OpenProcess。"""
        cached = self._names.get(pid)
        if cached and cached != "<unknown>":
            return cached
        if pid in self._name_cache_at and time.time() - self._name_cache_at[pid] < 30:
            return self._names.get(pid, "")
        resolved = pid_name(pid) or cached or ""
        self._names[pid] = resolved
        self._name_cache_at[pid] = time.time()
        return resolved

if __name__ == "__main__":
    import sys

    follow = sys.argv[2] if len(sys.argv) > 2 else "foreground"
    mon = FpsMonitor(window_ms=1500, follow=follow)
    print("子进程启动:", mon.start(), "|", mon.exe_path)
    try:
        for _ in range(int(sys.argv[1]) if len(sys.argv) > 1 else 15):
            time.sleep(1.0)
            fps, name, low = mon.snapshot()
            print(f"fps={'' if fps is None else round(fps, 1)!s:>7} "
                  f"1%low={'' if low is None else round(low, 1)!s:>7}  target={name}  "
                  f"running={mon.status['running']} rows={mon.status['rows_seen']} "
                  f"err={mon.status.get('error')}")
    finally:
        mon.stop()
        print("presentmon 子进程已结束")
