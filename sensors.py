# -*- coding: utf-8 -*-
"""硬件数据采集层：CPU/GPU 温度与占用率、内存。

设计原则：
  * 硬件访问仅在运行期间进行，退出即释放，不安装服务、不注册开机自启。
  * 单个数据源失败不影响其余项目；取不到值返回 None，由界面显示 --。
  * 任何一路卡住都不能拖累其他项：慢调用放进独立线程，主循环只取上一次成功结果。
  * 数据源报错或本轮没刷新成功时清空自己的缓存值。宁可显示 --，
    也不会把一个历史数字一直挂着。
  * 非管理员权限下跳过需要 ring0 访问的 CPU 传感器，避免输出不可信数值。

数据源：
  CPU/GPU 温度与占用率、显存、功耗 -> vendor/lhm/LibreHardwareMonitorLib.dll（pythonnet 进程内调用）
  GPU 替代来源                    -> nvidia-smi
  CPU 温度替代来源                -> ACPI 热区（win32pdh 计数器，普通权限可读；回退 WMI）
  CPU 占用率、内存                 -> psutil
"""

from __future__ import annotations

import ctypes
import os
import sys
import subprocess
import threading
import time

# 冻结成 exe 后 __file__ 指向包内路径，配置、日志与 vendor 必须与 exe 同级
BASE_DIR = (os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(BASE_DIR, "vendor", "lhm")

# 需要 ring0 访问权限（管理员）的 CPU 传感器
CPU_ONLY_WITH_ADMIN = ("cpu_temp", "cpu_power", "cpu_clock")

# 传感器名称随厂商、型号和库版本变化（Intel 用 CPU Package、AMD 用 Core (Tctl/Tdie)、
# 核显用 D3D 3D……），所以匹配规则是「传感器类型 + 名称包含的关键字」，按别名先后取第一个。
# 每条规则：(键, 类型, 别名(小写包含), 排除词)；aliases 为空表示必须精确命中 total 之类的名称。
CPU_RULES = (
    ("cpu_temp", "Temperature",
     ("cpu package", "core (tctl/tdie)", "core (tctl)", "core max", "cpu (proximity)",
      "cpu proximity", "core (tcc)", "package", "proximity"),
     ("utilization", "load")),
    ("cpu_load", "Load", ("cpu total", "total cpu", "total"), ("core", "memory", "power")),
    ("cpu_power", "Power",
     ("cpu package", "cpu ia", "cpu gt", "package", "cpu total", "power"), ()),
)

GPU_RULES = (
    ("gpu_temp", "Temperature", ("gpu core", "core", "temperature", "edge", "hot spot"), ()),
    ("gpu_load", "Load", ("gpu core", "d3d 3d", "core", "3d"), ("memory", "vram")),
    ("gpu_power", "Power", ("gpu package", "gpu power", "power"), ()),
    ("gpu_mem_used", "SmallData",
     ("gpu memory used", "d3d dedicated memory used", "memory used", "dedicated memory"),
     ("total", "free")),
    ("gpu_mem_total", "SmallData",
     ("gpu memory total", "d3d dedicated memory total", "memory total", "dedicated memory size"), ()),
)

# 别名全落空时，允许按类型兜底取一个同类传感器的键（宁可读数来源略偏，也不要整项显示 --）
TYPE_FALLBACK = ("cpu_temp", "gpu_temp", "gpu_load", "gpu_power")

# 只有温度参与冻结判定：实测空闲时 ACPI 热区 95 秒内变化 81 次、最长连续相同仅 2 秒，
# 所以温度连续不变到 FREEZE_LIMIT 就是数据源没刷新，与负载高低无关。
# 占用率、功耗、频率不参与——它们本来就能合法地长时间恒定（空闲 0%、0W、稳定睿频）。
TEMP_KEYS = ("cpu_temp", "gpu_temp")
FREEZE_LIMIT = 90.0                 # 秒：温度连续不变多久算冻结（给慢速 BIOS 留余量）
FREEZE_MIN_SAMPLES = 20             # 同时至少采到这么多次，避免开机头几秒误判


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _enum_name(obj, attr: str) -> str:
    """将 .NET 枚举转换为简短字符串，兼容不同 pythonnet 版本的表示差异。"""
    for getter in (
        lambda: getattr(obj, attr).ToString(),
        lambda: str(getattr(obj, attr)),
        lambda: getattr(getattr(obj, attr), "name", ""),
    ):
        try:
            value = getter()
        except Exception:
            continue
        if value:
            return str(value).split(".")[-1]
    return ""


class LhmSource:
    """LibreHardwareMonitorLib 的进程内封装。

    打开后缓存需要读取的传感器对象，每轮只对相关 hardware 调用一次 Update()。
    Update() 在各机器上快慢差别很大（NVAPI、ring0 驱动），甚至可能挂住，所以采集放在
    独立线程里：主循环只取上一次成功的结果，永远不等待硬件调用。

    某个 hardware 本轮 Update 失败就不读它名下的传感器——库内部有缓存，直接读 Value
    得到的是一个不再变化的历史数字。
    """

    name = "librehardwaremonitor"
    STALL_LIMIT = 5.0          # 秒：这么久没有成功采集即视为卡住

    def __init__(self) -> None:
        self.available = False
        self.error: str | None = None
        self.gpu_name: str | None = None
        self.hw_status: dict[str, str] = {}
        self.stalled = False
        self._computer = None
        self._update_targets: list = []
        self._sensors: list[tuple[str, object, int, int]] = []   # (key, sensor, 目标序号, 厂商优先级)
        self._clocks: list[tuple[object, int, int]] = []
        self._gpu_of: dict[int, str] = {}
        self._lock = threading.Lock()
        self._values: dict[str, float] = {}
        self._last_ok = 0.0
        self._busy = False
        self._closed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 打开
    def open(self) -> bool:
        self._closed = False
        try:
            import clr  # pythonnet
        except Exception as exc:
            self.error = f"pythonnet 不可用: {type(exc).__name__}: {exc}"
            return False

        if not os.path.isdir(VENDOR_DIR):
            self.error = f"缺少目录 {VENDOR_DIR}"
            return False

        for dll in sorted(os.listdir(VENDOR_DIR)):
            if dll.lower().endswith(".dll"):
                try:
                    clr.AddReference(os.path.join(VENDOR_DIR, dll))
                except Exception:
                    pass

        try:
            from LibreHardwareMonitor.Hardware import Computer  # type: ignore

            computer = Computer()
            computer.IsCpuEnabled = True
            computer.IsGpuEnabled = True
            computer.IsMemoryEnabled = False
            computer.IsMotherboardEnabled = False
            computer.IsControllerEnabled = False
            computer.Open()
        except Exception as exc:
            self.error = f"初始化失败: {type(exc).__name__}: {exc}"
            return False

        if self._closed:                     # 用户在这期间退出了：立刻卸载，不留驱动
            try:
                computer.Close()
            except Exception:
                pass
            return False

        if not self._select(computer.Hardware):
            return False

        self._computer = computer
        self.available = True
        self._last_ok = time.perf_counter()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lhm")
        self._thread.start()
        self._wait_first(1.5)
        return True

    def _wait_first(self, timeout: float) -> None:
        """等第一轮采集结束，好让上层判断驱动能否读到 CPU 温度。"""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if not self._busy and self._values is not None and time.perf_counter() - self._last_ok < timeout:
                return
            time.sleep(0.05)

    @staticmethod
    def _list(hw):
        try:
            return list(hw.Sensors)
        except Exception:
            return []

    def _select(self, hardware) -> bool:
        """把枚举到的硬件按厂商优先级分组，逐台挑传感器。

        独显和核显都注册为采集目标：混合显卡笔记本的独显在空闲时可能完全读不到值，
        这时同一个键自动改用核显的读数，而不是显示 --。
        """
        cpus, gpus = [], []
        try:
            for hw in hardware:
                kind = _enum_name(hw, "HardwareType")
                if kind == "Cpu":
                    cpus.append(hw)
                elif kind.startswith("Gpu"):
                    gpus.append((kind, hw))
        except Exception as exc:
            self.error = f"枚举硬件失败: {exc}"
            return False

        order = {"GpuNvidia": 0, "GpuAmd": 1, "GpuIntel": 2}
        gpus.sort(key=lambda x: order.get(x[0], 9))

        self._sensors, self._clocks, self._update_targets = [], [], []
        self._gpu_of = {}                                  # 显卡优先级 -> 显卡名
        for prio, cpu in enumerate(cpus[:2]):              # 双路机器也只用前两颗
            idx = len(self._update_targets)
            self._update_targets.append(cpu)
            self._sensors += self._pick(cpu, idx, prio, CPU_RULES)
            for s in self._list(cpu):
                if _enum_name(s, "SensorType") == "Clock" and "bus" not in str(s.Name).lower():
                    self._clocks.append((s, idx, prio))

        for prio, (_kind, gpu) in enumerate(gpus[:3]):
            idx = len(self._update_targets)
            self._update_targets.append(gpu)
            self._gpu_of[prio] = self._gpu_label(gpu)
            self._sensors += self._pick(gpu, idx, prio, GPU_RULES)
        if self._gpu_of.get(0):
            self.gpu_name = self._gpu_of[0]

        if not self._sensors and not self._clocks:
            self.error = "未读到可用传感器"
            return False
        return True

    @staticmethod
    def _gpu_label(hw) -> str:
        try:
            return str(hw.Name).strip()[:32]
        except Exception:
            return ""

    def _pick(self, hw, idx: int, prio: int, rules) -> list:
        """按「类型 + 名称包含关键字」挑传感器，同一键只取第一个命中的别名。"""
        picked: list = []
        taken: set[str] = set()
        sensors = self._list(hw)
        for key, stype, aliases, excludes in rules:
            if key in taken:
                continue
            hit = None
            for alias in aliases:
                for s in sensors:
                    if _enum_name(s, "SensorType") != stype:
                        continue
                    name = str(s.Name).strip().lower()
                    if alias not in name or any(x in name for x in excludes):
                        continue
                    hit = s
                    break
                if hit is not None:
                    break
            if hit is None and key in TYPE_FALLBACK:
                hit = next((s for s in sensors
                            if _enum_name(s, "SensorType") == stype
                            and not any(x in str(s.Name).lower() for x in excludes)), None)
            if hit is not None:
                taken.add(key)
                picked.append((key, hit, idx, prio))
        return picked

    # ---------------------------------------------------------------- 关闭
    def close(self) -> None:
        self._closed = True
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)      # 采集线程可能挂在驱动调用上，不硬等
        try:
            if self._computer is not None:
                self._computer.Close()     # 卸载 ring0 驱动
        except Exception:
            pass
        self._computer = None
        self.available = False
        self.stalled = False
        self._sensors, self._clocks, self._update_targets = [], [], []
        with self._lock:
            self._values = {}

    # ---------------------------------------------------------------- 采集
    def update(self) -> None:
        """非阻塞触发：上一轮还没回来就跳过，主循环永远不等硬件。"""
        if not self.available or self._busy:
            return
        self._busy = True
        threading.Thread(target=self._collect, daemon=True, name="lhm-once").start()

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            if self._busy:
                continue
            self._busy = True
            self._collect()

    def _collect(self) -> None:
        try:
            ok: dict[int, bool] = {}
            for i, hw in enumerate(self._update_targets):
                try:
                    hw.Update()
                    ok[i] = True
                except Exception as exc:
                    ok[i] = False
                    self.hw_status[self._hw_label(i)] = "更新失败: %s" % type(exc).__name__
            found: dict[str, float] = {}
            best: dict[str, tuple[int, float]] = {}
            for key, sensor, idx, prio in self._sensors:
                if not ok.get(idx):
                    continue                     # 本轮没刷新：不读缓存值
                try:
                    value = sensor.Value
                    if value is None:
                        continue
                    value = float(value)
                except Exception:
                    continue
                cur = best.get(key)
                if cur is None or prio < cur[0]:
                    best[key] = (prio, value)
            win_gpu = None
            for key in ("gpu_temp", "gpu_load", "gpu_power", "gpu_mem_used"):
                if key in best:
                    found[key] = best[key][1]
                    if win_gpu is None:
                        win_gpu = best[key][0]
            for key, (_prio, value) in best.items():
                if key not in found:
                    found[key] = value
            if win_gpu is not None and self._gpu_of.get(win_gpu):
                self.gpu_name = self._gpu_of[win_gpu]      # 状态页显示真正在出数的显卡
            best_clock = None
            for s, idx, _prio in self._clocks:
                if not ok.get(idx):
                    continue
                try:
                    v = s.Value
                    if v is not None:
                        v = float(v)
                        if best_clock is None or v > best_clock:
                            best_clock = v
                except Exception:
                    continue
            if best_clock is not None:
                found["cpu_clock"] = best_clock / 1000.0      # MHz -> GHz

            if any(ok.values()):
                self._last_ok = time.perf_counter()
                self.stalled = False
                for i in range(len(self._update_targets)):
                    if ok.get(i):
                        self.hw_status[self._hw_label(i)] = "正常"
            else:
                found = {}      # 全部失败：清空，让界面显示 --，不挂历史值
            with self._lock:
                self._values = found
        except Exception as exc:
            self.error = f"采集异常: {type(exc).__name__}: {exc}"
            with self._lock:
                self._values = {}
        finally:
            if self.available and time.perf_counter() - self._last_ok > self.STALL_LIMIT:
                self.stalled = True
            self._busy = False

    def _hw_label(self, idx: int) -> str:
        try:
            return str(self._update_targets[idx].Name).strip()[:32]
        except Exception:
            return "硬件%d" % idx

    def snapshot(self) -> dict[str, float]:
        if self.stalled:
            return {}
        with self._lock:
            return dict(self._values)


class NvsmiSource:
    """LibreHardwareMonitor 不可用时，改用 nvidia-smi 读取 GPU 温度与占用率。"""

    name = "nvidia-smi"

    def __init__(self) -> None:
        self.available = False
        self.error: str | None = None
        self._exe = self._locate()
        self._last = 0.0
        self._lock = threading.Lock()
        self._values: dict[str, float] = {}
        if self._exe:
            self.update()
            with self._lock:
                self.available = bool(self._values)
        else:
            self.error = "找不到 nvidia-smi"

    @staticmethod
    def _locate():
        cands = [
            "nvidia-smi",
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe"),
        ]
        for c in cands:
            try:
                out = subprocess.run(
                    [c, "--query-gpu=name", "--format=csv,noheader"],
                    capture_output=True, timeout=4,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if out.returncode == 0 and out.stdout.strip():
                    return c
            except Exception:
                continue
        return None

    def update(self) -> None:
        if not self._exe:
            return
        now = time.perf_counter()
        if now - self._last < 0.95:
            return
        self._last = now
        q = "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total"
        try:
            out = subprocess.run([self._exe, q, "--format=csv,noheader,nounits"],
                                 capture_output=True, timeout=4,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            text = out.stdout.decode("utf-8", "ignore").strip()
        except Exception as exc:
            self.error = "nvidia-smi 调用失败: %s" % type(exc).__name__
            with self._lock:
                self._values = {}
            return
        if not text:
            self.error = "nvidia-smi 无输出"
            with self._lock:
                self._values = {}
            return
        try:
            temp, util, used, total = (float(x) for x in text.split(",")[:4])
        except Exception:
            self.error = "nvidia-smi 输出无法解析"
            with self._lock:
                self._values = {}
            return
        with self._lock:
            self._values = {"gpu_temp": temp, "gpu_load": util,
                            "gpu_mem_used": used, "gpu_mem_total": total}
        self.error = None

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._values)


class ThermalZoneSource:
    """ACPI 热区温度。

    优先使用 win32pdh 性能计数器（Thermal Zone Information\\Temperature）：普通权限即可读取，
    不经过 WMI 服务，也就不会出现 WMI 超时。计数器不存在的系统回退到
    WMI root\\wmi : MSAcpi_ThermalZoneTemperature（需要管理员权限）。

    有多个热区时不按名字硬挑：先并行观察若干次采样，谁随负载波动就用谁，都没有波动时
    退回名字优先。读数失败立即清空缓存值。
    """

    name = "acpi-thermalzone"
    PDH_OBJECTS = ("Thermal Zone Information", "热区信息")
    STALE_LIMIT = 3.0
    LEARN_CYCLES = 12          # 前 12 次采样用于比较各热区的波动
    MIN_SPREAD = 0.5           # 摄氏度：小于这个波动视为不动

    def __init__(self) -> None:
        self.available = False
        self.error: str | None = None
        self.zone: str | None = None
        self.channel: str | None = None       # pdh / wmi
        self.locked = False                   # 是否已经选定热区
        self._lock = threading.Lock()
        self._values: dict[str, float] = {}
        self._last = 0.0
        self._last_ok = 0.0
        self._min_interval = 1.0
        self._pdh = None
        self._svc = None
        self._hist: dict[str, list[float]] = {}

    # ---------------------------------------------------------------- 打开
    def open(self) -> bool:
        if self._open_pdh():
            self.channel = "pdh"
        elif self._open_wmi():
            self.channel = "wmi"
        else:
            self.error = self.error or "热区温度不可用（PDH 与 WMI 均无返回值）"
            return False
        self.update()
        with self._lock:
            self.available = bool(self._values)
        return self.available

    def _open_pdh(self) -> bool:
        try:
            import win32pdh
        except Exception as exc:
            self.error = "win32pdh 不可用: %s" % type(exc).__name__
            return False
        try:
            objects = win32pdh.EnumObjects(None, None, win32pdh.PERF_DETAIL_WIZARD)
        except Exception as exc:
            self.error = "PDH 枚举失败: %s" % type(exc).__name__
            return False
        obj = next((hint for hint in self.PDH_OBJECTS if hint in objects), None)
        if obj is None:
            obj = next((n for n in objects if "hermal" in n.lower() or "热" in n), None)
        if obj is None:
            self.error = "系统没有 Thermal Zone Information 计数器"
            return False
        try:
            counters, instances = win32pdh.EnumObjectItems(
                None, None, obj, win32pdh.PERF_DETAIL_WIZARD)
        except Exception as exc:
            self.error = "PDH 计数器枚举失败: %s" % type(exc).__name__
            return False

        # 温度计数器名随本地化与精度变化，逐实例按优先级挑一个可用的（单位在读取时自动识别）
        cands = self._temp_counter_candidates(counters) or ["Temperature"]
        query = win32pdh.OpenQuery(None)
        added = []
        for inst in instances:
            for cname in cands:
                try:
                    path = win32pdh.MakeCounterPath((None, obj, inst, None, -1, cname))
                    added.append((inst, win32pdh.AddCounter(query, path)))
                    break
                except Exception:
                    continue
        if not added:
            try:
                win32pdh.CloseQuery(query)
            except Exception:
                pass
            self.error = "PDH 热区计数器不可用"
            return False
        self._pdh = (win32pdh, query, added)
        return True

    @staticmethod
    def _temp_counter_candidates(counters):
        """按优先级挑温度计数器名，兼容本地化与高/低精度两种单位。

        实测 Thermal Zone Information 对象通常同时提供 High Precision Temperature
        （0.1K，读数约 3000）与 Temperature（1K，读数约 300），部分机器可能只有其一，
        或把名字本地化（如“高精度温度/温度”）。
        """
        ranked = []
        for c in counters:
            low = c.strip().lower()
            if "high precision" in low and ("temp" in low or "温" in c):
                ranked.append((0, c))
            elif low == "temperature":
                ranked.append((1, c))
            elif "temp" in low or "温" in c:
                ranked.append((2, c))
        ranked.sort(key=lambda x: x[0])
        return [c for _p, c in ranked]

    def _open_wmi(self) -> bool:
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            self._svc = win32com.client.GetObject("winmgmts:" + chr(92) * 2 + "." + chr(92)
                                                  + "root" + chr(92) + "wmi")
        except Exception as exc:
            self.error = "pywin32/WMI 不可用: %s" % type(exc).__name__
            return False
        rows, err = self._read_wmi()
        if err:
            self.error = err
            return False
        return bool(rows)

    # ---------------------------------------------------------------- 读取
    def update(self) -> None:
        now = time.perf_counter()
        if now - self._last < self._min_interval:
            return
        self._last = now
        if self.channel == "pdh":
            rows, err = self._read_pdh()
        elif self.channel == "wmi":
            rows, err = self._read_wmi()
        else:
            return
        if err:
            with self._lock:
                self._values = {}
            self.error = err
            return
        if not rows:
            with self._lock:
                self._values = {}
            self.error = "热区无有效读数"
            return
        chosen = self._choose(rows)
        self.zone = chosen[0]
        with self._lock:
            self._values = {"cpu_temp": round(chosen[1], 1), "cpu_temp_source": 1.0}
            self._last_ok = time.perf_counter()
        self.error = None

    def _choose(self, rows):
        """返回选用的 (实例名, 温度)。学习期内比较各热区波动，之后固定用选中的那个。"""
        for inst, temp in rows:
            hist = self._hist.setdefault(inst, [])
            hist.append(temp)
            del hist[:-self.LEARN_CYCLES]
        if not self.locked:
            if len(rows) == 1:
                self.locked = True
                self.zone = rows[0][0]
            else:
                settled = [(inst, max(h) - min(h)) for inst, h in self._hist.items()
                           if len(h) >= self.LEARN_CYCLES]
                if settled:
                    moving = [(inst, spread) for inst, spread in settled if spread >= self.MIN_SPREAD]
                    if moving:
                        self.zone = max(moving, key=lambda x: x[1])[0]
                    else:
                        prefer = [r[0] for r in rows
                                  if any(k in r[0].upper() for k in ("THRM", "CPU", "PCK", "TZ0"))]
                        self.zone = prefer[0] if prefer else rows[0][0]
                    self.locked = True
        if self.locked and self.zone:
            for row in rows:
                if row[0] == self.zone:
                    return row
        prefer = [r for r in rows if any(k in r[0].upper() for k in ("THRM", "CPU", "PCK", "TZ0"))]
        return prefer[0] if prefer else max(rows, key=lambda r: r[1])

    def _read_pdh(self):
        if self._pdh is None:
            return [], "PDH 未初始化"
        pdh, query, counters = self._pdh
        try:
            pdh.CollectQueryData(query)
        except Exception as exc:
            return [], "PDH 采集失败: %s" % type(exc).__name__
        rows = []
        for inst, handle in counters:
            try:
                _t, raw = pdh.GetFormattedCounterValue(handle, pdh.PDH_FMT_DOUBLE)
            except Exception:
                continue
            if isinstance(raw, (int, float)) and raw > 0:
                # 不同机器/计数器单位不一致：开尔文约 300，0.1 开尔文约 3000，按大小自动识别
                kelvin = raw / 10.0 if raw > 1000 else raw
                temp = kelvin - 273.15
                if 0 < temp < 120:
                    rows.append((str(inst), temp))
        return rows, None

    def _read_wmi(self):
        if self._svc is None:
            return [], "WMI 未初始化"
        try:
            rows = []
            for obj in self._svc.InstancesOf("MSAcpi_ThermalZoneTemperature"):
                temp = None
                instance = ""
                for pr in obj.Properties_:
                    if pr.Name == "CurrentTemperature":
                        try:
                            raw = pr.Value
                            if isinstance(raw, (int, float)) and raw > 0:
                                temp = raw / 10.0 - 273.15      # 开尔文千分位
                        except Exception:
                            temp = None
                    elif pr.Name == "InstanceName":
                        try:
                            instance = str(pr.Value)
                        except Exception:
                            instance = ""
                if temp is not None and 0 < temp < 120:
                    rows.append((instance, temp))
        except Exception as exc:
            code = getattr(exc, "hex_exc", None)
            msg = "WMI 读取失败: %s" % type(exc).__name__
            if code == 0x80041003:
                msg += "（拒绝访问：需要管理员权限）"
            elif code == 0x80041021:
                msg += "（WMI 超时）"
            return [], msg
        return rows, None

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            if not self._values:
                return {}
            if time.perf_counter() - self._last_ok > self.STALE_LIMIT:
                return {}                                # 超过 3 秒没有成功读数：按无数据对待
            return {k: v for k, v in self._values.items() if k != "cpu_temp_source"}

    def close(self) -> None:
        if self._pdh is not None:
            pdh, query, counters = self._pdh
            for _inst, handle in counters:
                try:
                    pdh.RemoveCounter(handle)
                except Exception:
                    pass
            try:
                pdh.CloseQuery(query)
            except Exception:
                pass
            self._pdh = None
        was_wmi = self.channel == "wmi"
        self._svc = None
        self.available = False
        self.channel = None
        if was_wmi:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass


class SensorHub:
    """把所有数据源合并成 UI 直接可用的字典。阻塞操作全部放在独立线程。"""

    def __init__(self, interval_ms: int = 1000, cpu_temp_source: str = "auto") -> None:
        self.interval = max(0.25, interval_ms / 1000.0)
        self.cpu_temp_source = cpu_temp_source
        self.admin = is_admin()
        self.lhm = LhmSource()
        self.thermal = ThermalZoneSource()
        self.nvsmi: NvsmiSource | None = None
        self.status: dict[str, object] = {
            "admin": self.admin, "lhm": False, "lhm_error": None, "lhm_hw": {},
            "lhm_stalled": False, "nvsmi": False, "nvsmi_error": None,
            "thermal": False, "thermal_channel": None, "thermal_zone": None,
            "thermal_error": None, "cpu_temp_from": None, "stale_keys": [],
            "probing": True,
        }
        try:
            import psutil  # noqa: F401
            self.psutil = True
        except Exception:
            self.psutil = False
            self.status["psutil_error"] = "psutil 未安装"
        self._values: dict[str, object] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sampler")
        self._boot: threading.Thread | None = None
        self._nv_tried = False
        self._watch: dict[str, list[tuple[float, float, float]]] = {}   # (时刻, 值, cpu占用)

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        """界面先出来，硬件探测放后台。

        不同机器的驱动初始化时间差别极大（Intel 核显几百毫秒，带 RGB 控制器的机器
        Computer.Open() 可能十几秒，WMI 热区还可能超时），同步等待会让用户以为程序卡死。
        """
        self._thread.start()
        self._boot = threading.Thread(target=self._open_sources, daemon=True, name="probe")
        self._boot.start()

    def _open_sources(self) -> None:
        try:
            if self.lhm.open():
                self.status["lhm"] = True
                self.status["gpu_name"] = self.lhm.gpu_name
            else:
                self.status["lhm_error"] = self.lhm.error

            # CPU 温度：优先使用驱动读数，不可用时改用 ACPI 热区
            if self.cpu_temp_source in ("auto", "acpi"):
                lhm_has_cpu_temp = False
                if self.lhm.available and self.cpu_temp_source != "acpi":
                    lhm_has_cpu_temp = self.lhm.snapshot().get("cpu_temp") is not None
                if not lhm_has_cpu_temp:
                    if self.thermal.open():
                        self.status["thermal"] = True
                        self.status["thermal_channel"] = self.thermal.channel
                        self.status["thermal_zone"] = self.thermal.zone
                    else:
                        self.status["thermal_error"] = self.thermal.error
        finally:
            self.status["probing"] = False

    def _ensure_nvsmi(self) -> None:
        """GPU 项有缺口时才探测 nvidia-smi：没有该命令的机器要几秒才能确认，放后台。"""
        if self.nvsmi is not None or self._nv_tried:
            return
        self._nv_tried = True
        threading.Thread(target=self._probe_nvsmi, daemon=True, name="nvsmi-probe").start()

    def _probe_nvsmi(self) -> None:
        try:
            self.nvsmi = NvsmiSource()
            self.status["nvsmi"] = self.nvsmi.available
            self.status["nvsmi_error"] = None if self.nvsmi.available else self.nvsmi.error
        except Exception as exc:
            self.nvsmi = None
            self.status["nvsmi_error"] = "探测失败: %s" % type(exc).__name__

    def stop(self) -> None:
        self._stop.set()
        boot = getattr(self, "_boot", None)
        if boot is not None and boot.is_alive():
            boot.join(timeout=3.0)     # 别和正在进行的 open() 抢，否则会漏掉驱动卸载
        try:
            self._thread.join(timeout=2.0)
        except Exception:
            pass
        self.lhm.close()      # 立刻释放 ring0 驱动，不留后台
        self.thermal.close()

    def set_interval_ms(self, interval_ms: int) -> None:
        self.interval = max(0.25, interval_ms / 1000.0)

    # ---------------------------------------------------------------- 循环
    def _loop(self) -> None:
        if self.psutil:
            import psutil
            psutil.cpu_percent(interval=None)          # 首次采样预热
        while not self._stop.is_set():
            t0 = time.perf_counter()
            snap = self._sample_once()
            with self._lock:
                self._values = snap
            self._stop.wait(max(0.0, self.interval - (time.perf_counter() - t0)))

    def _sample_once(self) -> dict[str, object]:
        out: dict[str, object] = {}
        lhm_cpu_temp = None
        if self.lhm.available:
            self.lhm.update()                          # 非阻塞，慢调用不会拖住这一轮
            vals = self.lhm.snapshot()
            if not self.admin:
                for k in CPU_ONLY_WITH_ADMIN:
                    vals.pop(k, None)      # 非管理员下这些传感器返回空值或不可信的 0
            else:
                lhm_cpu_temp = vals.get("cpu_temp")
            out.update(vals)
            self.status["lhm_stalled"] = self.lhm.stalled
            self.status["lhm_hw"] = dict(self.lhm.hw_status)

        if self.thermal.available:
            self.thermal.update()
            tz_vals = self.thermal.snapshot()
            if tz_vals.get("cpu_temp") is not None and (
                    self.cpu_temp_source == "acpi" or lhm_cpu_temp is None):
                out["cpu_temp"] = tz_vals["cpu_temp"]
                self.status["cpu_temp_from"] = "%s:%s" % (self.thermal.channel, self.thermal.zone)
            elif lhm_cpu_temp is not None:
                self.status["cpu_temp_from"] = "lhm"
            self.status["thermal_error"] = self.thermal.error
            if self.thermal.zone:
                self.status["thermal_zone"] = self.thermal.zone

        # LHM 在部分机器上看不见独显（驱动版本、混合显卡休眠），只要 GPU 项有缺口就用 nvidia-smi 补
        gpu_missing = any(out.get(k) is None for k in ("gpu_temp", "gpu_load", "gpu_mem_used"))
        if gpu_missing:
            self._ensure_nvsmi()
        if self.nvsmi is not None and gpu_missing:
            self.nvsmi.update()
            nv = self.nvsmi.snapshot()
            for k, v in nv.items():
                out.setdefault(k, v)
            self.status["nvsmi_error"] = None if nv else self.nvsmi.error

        if self.psutil:
            import psutil
            try:
                out["cpu_load"] = psutil.cpu_percent(interval=None)
            except Exception:
                pass
            try:
                vm = psutil.virtual_memory()
                out["mem_total"] = vm.total / 1024 ** 3
                out["mem_used"] = (vm.total - vm.available) / 1024 ** 3
                out["mem_percent"] = vm.percent
            except Exception:
                pass

        self._drop_static(out)

        if self.cpu_temp_source == "off":
            for k in ("cpu_temp", "cpu_temp_max", "cpu_temp_avg", "cpu_temp_hotspot"):
                out.pop(k, None)
            self.status["cpu_temp_from"] = "off"
        return out

    def _drop_static(self, out: dict[str, object]) -> None:
        """冻结检测：同一个数值连续不变到阈值就判定数据源没刷新，按无数据处理。

        判据只有时间，不需要「负载先波动」这个前提——真实传感器在空闲时也在抖
        （实测本机空闲热区 95 秒变化 81 次），所以一个数纹丝不动本身就是故障证据。
        判定是粘性的：一旦认定冻结就持续按无数据处理，直到该值真的变化才恢复。
        """
        now = time.perf_counter()
        stale = set(self.status.get("stale_keys") or [])
        for key in TEMP_KEYS:
            value = out.get(key)
            if not isinstance(value, (int, float)):
                self._watch.pop(key, None)
                stale.discard(key)
                continue
            watch = self._watch.get(key)
            if watch is None or abs(float(value) - watch[1]) > 1e-6:
                self._watch[key] = [now, float(value), 1]
                stale.discard(key)              # 值动了：数据源在刷新
                continue
            watch[2] += 1
            if watch[2] >= FREEZE_MIN_SAMPLES and now - watch[0] >= FREEZE_LIMIT:
                stale.add(key)
        for key in stale:
            out.pop(key, None)
        self.status["stale_keys"] = sorted(stale)

    # ---------------------------------------------------------------- 读取
    def read(self) -> dict[str, object]:
        with self._lock:
            return dict(self._values)


if __name__ == "__main__":
    import json
    import sys

    sys.path.insert(0, BASE_DIR)
    hub = SensorHub(1000)
    hub.start()
    print("admin =", hub.admin, "| status =", json.dumps(hub.status, ensure_ascii=False))
    try:
        for _ in range(int(sys.argv[1]) if len(sys.argv) > 1 else 5):
            time.sleep(1.0)
            vals = hub.read()
            print({k: (round(v, 2) if isinstance(v, float) else v) for k, v in vals.items()})
    finally:
        hub.stop()
        print("温度驱动已释放")
