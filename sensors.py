# -*- coding: utf-8 -*-
"""硬件数据采集层：CPU/GPU 温度与占用率、内存。

设计原则：
  * 硬件访问仅在运行期间进行，退出即释放，不安装服务、不注册开机自启。
  * 单个数据源失败不影响其余项目；取不到值返回 None，由界面显示 --。
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
import subprocess
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(BASE_DIR, "vendor", "lhm")

# 需要 ring0 访问权限（管理员）的 CPU 传感器
CPU_ONLY_WITH_ADMIN = ("cpu_temp", "cpu_power", "cpu_clock")

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

    打开后缓存需要读取的传感器对象，每轮只对相关 hardware 调用一次 Update()，
    再读取这些对象的 Value，避免每轮遍历全部传感器。
    """

    name = "librehardwaremonitor"

    def __init__(self) -> None:
        self.available = False
        self.error: str | None = None
        self.gpu_name: str | None = None
        self._computer = None
        self._update_targets: list = []
        self._sensors: list[tuple[str, object]] = []      # (key, sensor) 单值
        self._clocks: list = []                            # CPU 频率取最大值
        self._lock = threading.Lock()
        self._values: dict[str, float] = {}

    # ---------------------------------------------------------------- 打开
    def open(self) -> bool:
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

        cpus, gpus = [], []
        try:
            for hw in computer.Hardware:
                kind = _enum_name(hw, "HardwareType")
                if kind == "Cpu":
                    cpus.append(hw)
                elif kind.startswith("Gpu"):
                    gpus.append((kind, hw))
        except Exception as exc:
            self.error = f"枚举硬件失败: {exc}"
            return False

        # GPU 优先独显（NVIDIA > AMD > Intel 核显），核显一般没有温度传感器
        order = {"GpuNvidia": 0, "GpuAmd": 1, "GpuIntel": 2}
        gpus.sort(key=lambda x: order.get(x[0], 9))
        gpu = gpus[0][1] if gpus else None

        self._sensors, self._clocks, self._update_targets = [], [], []
        if cpus:
            cpu = cpus[0]
            self._update_targets.append(cpu)
            self._sensors += self._pick(cpu, [
                (("Temperature", "cpu package"), "cpu_temp"),
                (("Temperature", "core (tctl/tdie)"), "cpu_temp"),
                (("Temperature", "core max"), "cpu_temp"),
                (("Load", "cpu total"), "cpu_load"),
                (("Power", "cpu package"), "cpu_power"),
            ])
            for s in self._list(cpu):
                if _enum_name(s, "SensorType") == "Clock" and "bus" not in str(s.Name).lower():
                    self._clocks.append(s)

        if gpu is not None:
            self._update_targets.append(gpu)
            self._sensors += self._pick(gpu, [
                (("Temperature", "gpu core"), "gpu_temp"),
                (("Load", "gpu core"), "gpu_load"),
                (("Power", "gpu package"), "gpu_power"),
                (("Power", "gpu power"), "gpu_power"),
                (("SmallData", "gpu memory used"), "gpu_mem_used"),
                (("SmallData", "d3d dedicated memory used"), "gpu_mem_used"),
                (("SmallData", "gpu memory total"), "gpu_mem_total"),
            ])
            try:
                self.gpu_name = str(gpu.Name)
            except Exception:
                pass

        if not self._sensors and not self._clocks:
            self.error = "未读到可用传感器"
            return False

        self._computer = computer
        self.available = True
        self.update()
        return True

    @staticmethod
    def _list(hw):
        try:
            return list(hw.Sensors)
        except Exception:
            return []

    def _pick(self, hw, rules) -> list:
        """按 (传感器类型, 名称) 优先级列表挑选第一个命中的传感器。"""
        picked = []
        sensors = self._list(hw)
        for (stype, keyword), key in rules:
            if any(k == key for k, _ in picked):
                continue
            for s in sensors:
                if _enum_name(s, "SensorType") != stype:
                    continue
                if str(s.Name).strip().lower() != keyword:
                    continue
                picked.append((key, s))
                break
        return picked

    # ---------------------------------------------------------------- 关闭
    def close(self) -> None:
        try:
            if self._computer is not None:
                self._computer.Close()     # 卸载 ring0 驱动
        except Exception:
            pass
        self._computer = None
        self.available = False
        self._sensors, self._clocks, self._update_targets = [], [], []
        with self._lock:
            self._values = {}

    # ---------------------------------------------------------------- 采集
    def update(self) -> None:
        if not self.available:
            return
        for hw in self._update_targets:
            try:
                hw.Update()
            except Exception:
                continue
        found: dict[str, float] = {}
        for key, sensor in self._sensors:
            try:
                value = sensor.Value
                if value is None:
                    continue
                found[key] = float(value)
            except Exception:
                continue
        if self._clocks:
            best = None
            for s in self._clocks:
                try:
                    v = s.Value
                    if v is not None:
                        v = float(v)
                        if best is None or v > best:
                            best = v
                except Exception:
                    continue
            if best is not None:
                found["cpu_clock"] = best / 1000.0      # MHz -> GHz
        with self._lock:
            self._values = found

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._values)

class NvsmiSource:
    """LibreHardwareMonitor 不可用时，改用 nvidia-smi 读取 GPU 温度与占用率。"""

    name = "nvidia-smi"

    def __init__(self) -> None:
        self.available = False
        self._exe = self._locate()
        self._last = 0.0
        self._lock = threading.Lock()
        self._values: dict[str, float] = {}
        if self._exe:
            self.update()
            with self._lock:
                self.available = bool(self._values)

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
        except Exception:
            return
        if not text:
            return
        try:
            temp, util, used, total = (float(x) for x in text.split(",")[:4])
        except Exception:
            return
        with self._lock:
            self._values = {"gpu_temp": temp, "gpu_load": util,
                            "gpu_mem_used": used, "gpu_mem_total": total}

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._values)

class ThermalZoneSource:
    """ACPI 热区温度。

    优先使用 win32pdh 性能计数器（Thermal Zone Information\\Temperature）：普通权限即可读取，
    不经过 WMI 服务，也就不会出现 WMI 超时。计数器不存在的系统回退到
    WMI root\\wmi : MSAcpi_ThermalZoneTemperature（需要管理员权限）。

    读数失败时立即清空缓存值：宁可界面显示 --，也不会重复输出上一次的历史数值。
    """

    name = "acpi-thermalzone"
    PDH_OBJECTS = ("Thermal Zone Information", "热区信息")
    PDH_COUNTER = "Temperature"
    STALE_LIMIT = 3.0

    def __init__(self) -> None:
        self.available = False
        self.error: str | None = None
        self.zone: str | None = None
        self.channel: str | None = None       # pdh / wmi
        self._lock = threading.Lock()
        self._values: dict[str, float] = {}
        self._last = 0.0
        self._last_ok = 0.0
        self._min_interval = 1.0
        self._pdh = None
        self._svc = None

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
            objects = win32pdh.EnumObjects(None, None, 0)
        except Exception as exc:
            self.error = "PDH 枚举失败: %s" % type(exc).__name__
            return False
        obj = next((hint for hint in self.PDH_OBJECTS if hint in objects), None)
        if obj is None:
            obj = next((n for n in objects if "hermal" in n or "热区" in n), None)
        if obj is None:
            self.error = "系统没有 Thermal Zone Information 计数器"
            return False
        try:
            instances = win32pdh.EnumObjectItems(None, None, obj, win32pdh.PERF_DETAIL_WIZARD)[1]
        except Exception as exc:
            self.error = "PDH 计数器枚举失败: %s" % type(exc).__name__
            return False
        query = win32pdh.OpenQuery(None)
        counters = []
        for inst in instances:
            try:
                path = win32pdh.MakeCounterPath((None, obj, inst, None, -1, self.PDH_COUNTER))
                counters.append((inst, win32pdh.AddCounter(query, path)))
            except Exception:
                continue
        if not counters:
            try:
                win32pdh.CloseQuery(query)
            except Exception:
                pass
            self.error = "PDH 热区计数器不可用"
            return False
        self._pdh = (win32pdh, query, counters)
        return True

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
        # 一台机器可能有多个热区：优先名字里带 CPU/THRM/PCK/TZ0 的，否则取最高的那个
        prefer = [r for r in rows if any(k in r[0].upper() for k in ("THRM", "CPU", "PCK", "TZ0"))]
        chosen = prefer[0] if prefer else max(rows, key=lambda r: r[1])
        self.zone = chosen[0]
        with self._lock:
            self._values = {"cpu_temp": round(chosen[1], 1), "cpu_temp_source": 1.0}
            self._last_ok = time.perf_counter()
        self.error = None

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
                temp = raw - 273.15                      # 计数器直接以开尔文返回
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
        self.status: dict[str, object] = {"admin": self.admin, "lhm": False,
                                          "lhm_error": None, "nvsmi": False,
                                          "thermal": False, "thermal_channel": None,
                                          "thermal_zone": None, "thermal_error": None,
                                          "cpu_temp_from": None}
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

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self.lhm.open():
            self.status["lhm"] = True
            self.status["gpu_name"] = self.lhm.gpu_name
        else:
            self.status["lhm_error"] = self.lhm.error
            self.nvsmi = NvsmiSource()
            self.status["nvsmi"] = self.nvsmi.available

        # CPU 温度：优先使用驱动读数，不可用时改用 ACPI 热区
        if self.cpu_temp_source in ("auto", "acpi"):
            lhm_has_cpu_temp = False
            if self.lhm.available and self.cpu_temp_source != "acpi":
                self.lhm.update()
                lhm_has_cpu_temp = self.lhm.snapshot().get("cpu_temp") is not None
            if not lhm_has_cpu_temp:
                if self.thermal.open():
                    self.status["thermal"] = True
                    self.status["thermal_channel"] = self.thermal.channel
                    self.status["thermal_zone"] = self.thermal.zone
                else:
                    self.status["thermal_error"] = self.thermal.error
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
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
            self.lhm.update()
            vals = self.lhm.snapshot()
            if not self.admin:
                for k in CPU_ONLY_WITH_ADMIN:
                    vals.pop(k, None)      # 非管理员下这些传感器返回空值或不可信的 0
            else:
                lhm_cpu_temp = vals.get("cpu_temp")
            out.update(vals)

        if self.thermal.available:
            self.thermal.update()
            tz_vals = self.thermal.snapshot()
            if tz_vals.get("cpu_temp") is not None and (
                    self.cpu_temp_source == "acpi" or lhm_cpu_temp is None):
                out["cpu_temp"] = tz_vals["cpu_temp"]
                self.status["cpu_temp_from"] = "%s:%s" % (self.thermal.channel, self.thermal.zone)
            elif lhm_cpu_temp is not None:
                self.status["cpu_temp_from"] = "lhm"
            if self.thermal.error:
                self.status["thermal_error"] = self.thermal.error
            else:
                self.status["thermal_error"] = None

        if not self.lhm.available and self.nvsmi is not None:
            self.nvsmi.update()
            for k, v in self.nvsmi.snapshot().items():
                out.setdefault(k, v)

        if self.cpu_temp_source == "off":
            for k in ("cpu_temp", "cpu_temp_max", "cpu_temp_avg", "cpu_temp_hotspot"):
                out.pop(k, None)
            self.status["cpu_temp_from"] = "off"

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
        return out

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
