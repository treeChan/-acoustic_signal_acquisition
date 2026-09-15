# 声学矢量传感器采集系统（macOS / Windows）

本工具在 macOS 上直接通过 USB 串口采集 STM32 声学采集卡；Windows 和 TCP 数据分析仪仍可使用交付的 `DAQ Acquisition SDK v0.2.1`。

## 获取源码

```bash
git clone https://github.com/treeChan/-acoustic_signal_acquisition.git acoustic_signal_acquisition
cd acoustic_signal_acquisition
```

## 图形化上位机

在 Finder 中双击 `run_gui_macos.command`，或在终端运行：

```bash
cd acoustic_signal_acquisition
bash run_gui_macos.command
```

上位机把实时显示与文件录制分开控制：点击“开始显示”只打开设备并刷新图形，不创建文件；显示期间再点击“开始录制”才同步生成 HDF5 和 WAV。“停止录制”只关闭文件、波形仍继续，“停止显示”才关闭设备。显示模式仅在内存中保留最近 1 秒的有界滚动数据。

“WAV 文件位深”可选择 `16-bit PCM`、`24-bit PCM` 或 `32-bit float（推荐）`。这是文件编码选项，不会改变采集卡的 ADC/USB 原始数据格式。HDF5 始终保存未归一化的 float32 电压值，作为模型训练和定量分析的主数据。

上位机还提供设备状态、录制时长、保存位置、P 通道灵敏度、P/X/Y/Z 实时 RMS 和 P 通道 SPL。“频域信号”页显示四通道独立的单边 RMS 频谱，可设置 FFT 点数与频率上限；“时域信号”页显示四通道瞬时电压波形，可选择最近 20 ms 至 1000 ms 的时间窗口；“时频域信号”页显示 P/X/Y/Z 四幅滚动声谱图，历史长度可选 5、10、20 或 30 秒，横轴为相对当前时刻，纵轴为频率，颜色为单边 RMS 幅值。四幅声谱图共用同一色标，便于比较通道间幅值。

界面的 `mV/V` 单位选择会同时作用于顶部 RMS、时域纵轴、频域纵轴和时频图颜色标尺。时频图与频谱图共用当前 FFT 点数和最高频率设置。

声源定位标签只需填写方位角、俯仰角和可选备注，输入值会保留供下一次录制复用。坐标定义为：`+X=方位角 0°`，从 `+X` 朝 `+Y` 为方位角正方向，XY 平面为俯仰角 0°，朝 `+Z` 为俯仰角正方向。点击“开始录制”时锁定当前真值；结束后角度会写入 HDF5 属性，并生成同名 JSON 和目录中的 `manifest.jsonl`，便于模型批量加载。

打包版应用的默认保存目录是 `~/Documents/AcousticVectorRecords`，可在界面中改为任意可写目录。

## 接线

四通道 `USP_3D` 接线和数据列固定如下：

| 物理通道 | 数据列 | 信号 |
|---|---:|---|
| CH1 | 0 | P，声压 |
| CH2 | 1 | X 方向矢量 |
| CH3 | 2 | Y 方向矢量 |
| CH4 | 3 | Z 方向矢量 |

传感器 P/X/Y/Z 四路输出依次接入 CH1–CH4。供电、电压范围和极性以传感器厂家手册为准。不要在 USB 采集卡上盲目开启 IEPE；`--iepe` 只适用于明确支持该功能的 TCP 数据分析仪。

## macOS 安装与运行

需要 Python 3.10 或更新版本：

```bash
python3 -m pip install -r requirements.txt
cd acoustic_signal_acquisition
bash run_macos.command
```

程序会自动寻找 VID `0483`、PID `5740` 的 `STM32 Virtual ComPort`。当前电脑检测到的端口是 `/dev/cu.usbmodem356D356D31331`，也可显式指定：

```bash
bash run_macos.command --port /dev/cu.usbmodem356D356D31331
```

默认持续录制到 `Ctrl+C`。录制 60 秒、使用标定证书中的 P 通道灵敏度：

```bash
bash run_macos.command --duration 60 --sensitivity-mv-pa 50
```

macOS 首次访问 USB 设备时，系统或 Codex 可能询问设备访问权限，请允许。不要同时打开官方上位机或其他占用此串口的软件。

## 输出数据

每次采集会同时生成同名的 HDF5 和 WAV，例如 `records/acoustic_时间戳_4ch.h5` 与 `records/acoustic_时间戳_4ch.wav`。其中：

- `data` 形状为 `(总采样点数, 4)`，单位 V，列顺序为 P/X/Y/Z；
- 属性含 `sample_rate=32000`、通道标签、灵敏度、开始/完成时间和实际样本数；
- WAV 为四通道、32 kHz，通道顺序同样为 P/X/Y/Z；位深可选 16-bit PCM、24-bit PCM 或 32-bit IEEE float；
- 终端每秒显示四通道 RMS 电压，并按 `--sensitivity-mv-pa` 计算 P 通道声压与 dB SPL。

32-bit float WAV 不归一化，采样值直接以 V 保存，最适合保留测量幅值。16/24-bit PCM 在停止录制后，以 P/X/Y/Z 四通道共同峰值做一次统一归一化并保留 2% 余量；它不会改变通道间幅值比和相位，适合定位模型及普通音频软件。恢复绝对电压所需的 `full_scale_voltage_v`、PCM 位深和换算公式会同时写入同名 JSON、`manifest.jsonl` 和 HDF5 属性。HDF5 本身始终保留原始电压，因此不会因 WAV 降位深而丢失主数据精度。

部分只面向普通音乐的播放器可能不支持四通道 float WAV，建议用 MATLAB、Python/Scipy、Audacity 或专业声学软件读取。经典 WAV 单文件上限约 4 GB，在四通道 32 kHz float32 下约可连续保存 2 小时 20 分钟；16/24-bit PCM 可保存更长时间。

macOS 原生后端当前固定使用设备断电重连后的默认 32 kHz。交付资料没有公开切换采样率的 USB 命令，因此程序会拒绝在原生后端声明其他采样率，避免时间轴标注错误。

## Windows / TCP

Windows USB 或 TCP 数据分析仪需要安装交付的 Python 3.12 wheel，然后使用 `--backend sdk`：

```powershell
python acoustic_acquisition.py --backend sdk --mode usb --port COM5 --sample-rate 48000
python acoustic_acquisition.py --backend sdk --mode tcp --host 192.168.1.10 --tcp-port 5001
```
