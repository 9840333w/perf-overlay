# 第三方组件与许可

本项目的源代码采用 MIT 许可（见 `LICENSE`）。下列第三方组件按其各自的原许可分发，
本项目的许可不覆盖它们，也不改变其许可条款。

## 随程序分发的二进制组件

| 组件 | 版本 | 许可 | 分发位置 | 上游与源码 |
|---|---|---|---|---|
| LibreHardwareMonitorLib | 0.9.x | MPL-2.0 | `vendor/lhm/LibreHardwareMonitorLib.dll` | https://github.com/LibreHardwareMonitor/LibreHardwareMonitor |
| HidSharp | 2.1.0 | Apache-2.0 | `vendor/lhm/HidSharp.dll` | https://github.com/IntergatedCircuits/HidSharp |
| System.Memory / System.Buffers / System.Numerics.Vectors / System.Runtime.CompilerServices.Unsafe | .NET 基础库扩展 | MIT | `vendor/lhm/` | https://github.com/dotnet/machinelearning 与 https://github.com/dotnet/runtime |
| Intel PresentMon | 2.6.0 | MIT | `vendor/presentmon/presentmon.exe` | https://github.com/GameTechDev/PresentMon |

MPL-2.0 是文件级许可：`LibreHardwareMonitorLib.dll` 自身的源码可用上述地址获取，本项目只是按原样调用它，
不修改其源码，也不将其代码并入本项目文件。Apache-2.0 与 MIT 组件的完整许可文本可在对应上游仓库查看。

## 免安装 exe 版额外包含的运行库

exe 版由 PyInstaller 打包，除上表组件外还静态携带以下运行库，许可同样归各自作者：

| 组件 | 许可 |
|---|---|
| Python 3.12 | PSF-2.0 |
| tkinter / Tcl / Tk | Tcl/Tk 许可（BSD 风格） |
| pythonnet | MIT |
| pywin32 | PSF |
| Pillow | MIT-CMU |
| psutil | BSD-3-Clause |
| Microsoft Visual C++ 运行时 | 微软随附许可条款 |

## 依赖库（脚本版通过 pip 安装，不随仓库分发）

`psutil`（BSD-3-Clause）、`pythonnet`（MIT）、`pywin32`（PSF）、`Pillow`（MIT-CMU）。
版本要求见 `requirements.txt`。
