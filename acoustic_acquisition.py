#!/usr/bin/env python3
"""声学矢量传感器采集系统。

macOS USB 后端直接兼容 STM32 Virtual COM Port；Windows/TCP 后端使用官方
DAQ Acquisition SDK v0.2.1。USP_3D 数据列固定为 P、X、Y、Z。
"""
from __future__ import annotations

import argparse
import json
import platform
import queue
import signal
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np


SAMPLE_RATES = (32000, 44100, 48000, 96000)
USB_START_COMMAND = b"22"
USB_STOP_COMMAND = b"00"
USB_CONVERSION_FACTOR = 1e8
STM32_VID = 0x0483
STM32_PID = 0x5740


class Float32WavWriter:
    """流式写入标准 IEEE float32 多通道 WAV，并在关闭时补全 RIFF 长度。"""

    HEADER_SIZE = 44
    MAX_DATA_BYTES = 0xFFFFFFFF - 36

    def __init__(self, path: Path, sample_rate: int, channels: int) -> None:
        self.path = path
        self.sample_rate = sample_rate
        self.channels = channels
        self.data_bytes = 0
        self._file = path.open("w+b")
        byte_rate = sample_rate * channels * 4
        block_align = channels * 4
        self._file.write(struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36, b"WAVE", b"fmt ", 16, 3, channels,
            sample_rate, byte_rate, block_align, 32, b"data", 0,
        ))

    def write(self, data: np.ndarray) -> None:
        samples = np.ascontiguousarray(data, dtype="<f4")
        if samples.ndim != 2 or samples.shape[1] != self.channels:
            raise ValueError(f"WAV 数据必须为 (samples, {self.channels})")
        payload = samples.tobytes()
        if self.data_bytes + len(payload) > self.MAX_DATA_BYTES:
            raise RuntimeError("WAV 文件将超过经典 RIFF 的 4 GB 限制，请停止并新建录制")
        self._file.write(payload)
        self.data_bytes += len(payload)

    def close(self) -> None:
        if self._file.closed:
            return
        self._file.seek(4)
        self._file.write(struct.pack("<I", 36 + self.data_bytes))
        self._file.seek(40)
        self._file.write(struct.pack("<I", self.data_bytes))
        self._file.close()

    def __enter__(self) -> "Float32WavWriter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class PcmWavWriter:
    """流式写入 16/24-bit PCM WAV；输入必须是归一化到 [-1, 1] 的浮点数。"""

    HEADER_SIZE = 44
    MAX_DATA_BYTES = 0xFFFFFFFF - 36

    def __init__(self, path: Path, sample_rate: int, channels: int, bit_depth: int) -> None:
        if bit_depth not in (16, 24):
            raise ValueError("PCM WAV 位深只支持 16 或 24")
        self.path = path
        self.sample_rate = sample_rate
        self.channels = channels
        self.bit_depth = bit_depth
        self.sample_width = bit_depth // 8
        self.data_bytes = 0
        self._file = path.open("w+b")
        byte_rate = sample_rate * channels * self.sample_width
        block_align = channels * self.sample_width
        self._file.write(struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36, b"WAVE", b"fmt ", 16, 1, channels,
            sample_rate, byte_rate, block_align, bit_depth, b"data", 0,
        ))

    def write(self, data: np.ndarray) -> None:
        samples = np.asarray(data)
        if samples.ndim != 2 or samples.shape[1] != self.channels:
            raise ValueError(f"WAV 数据必须为 (samples, {self.channels})")
        normalized = np.clip(samples, -1.0, 1.0)
        if self.bit_depth == 16:
            payload = np.rint(normalized * 32767.0).astype("<i2").tobytes()
        else:
            values = np.rint(normalized * 8388607.0).astype("<i4").reshape(-1)
            byte_view = values.view(np.uint8).reshape(-1, 4)
            payload = byte_view[:, :3].tobytes()
        if self.data_bytes + len(payload) > self.MAX_DATA_BYTES:
            raise RuntimeError("WAV 文件将超过经典 RIFF 的 4 GB 限制，请停止并新建录制")
        self._file.write(payload)
        self.data_bytes += len(payload)

    def close(self) -> None:
        if self._file.closed:
            return
        self._file.seek(4)
        self._file.write(struct.pack("<I", 36 + self.data_bytes))
        self._file.seek(40)
        self._file.write(struct.pack("<I", self.data_bytes))
        self._file.close()

    def __enter__(self) -> "PcmWavWriter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


PCM_HEADROOM = 0.98


def pcm_full_scale_voltage(peak_voltage: float) -> float:
    """返回 PCM 满刻度对应的电压；四通道共用一个增益并保留 2% 余量。"""
    return peak_voltage / PCM_HEADROOM if peak_voltage > 0 else 1.0


def export_h5_data_to_pcm_wav(
    h5_path: Path,
    wav_path: Path,
    sample_rate: int,
    channels: int,
    bit_depth: int,
    peak_voltage: float | None = None,
    chunk_samples: int = 131072,
) -> float:
    """把 HDF5 的 float32 电压数据分块导出为 PCM WAV，返回满刻度电压。"""
    import h5py

    if bit_depth not in (16, 24):
        raise ValueError("PCM WAV 位深只支持 16 或 24")
    temp_path = wav_path.with_name(wav_path.name + ".part")
    try:
        with h5py.File(h5_path, "r") as h5:
            dataset = h5["data"]
            if dataset.ndim != 2 or dataset.shape[1] != channels:
                raise ValueError(f"HDF5 data 必须为 (samples, {channels})")
            if peak_voltage is None:
                peak_voltage = 0.0
                for start in range(0, dataset.shape[0], chunk_samples):
                    block = np.asarray(dataset[start:start + chunk_samples], dtype=np.float32)
                    if block.size:
                        peak_voltage = max(peak_voltage, float(np.max(np.abs(block))))
            full_scale_voltage = pcm_full_scale_voltage(float(peak_voltage))
            with PcmWavWriter(temp_path, sample_rate, channels, bit_depth) as wav:
                for start in range(0, dataset.shape[0], chunk_samples):
                    block = np.asarray(dataset[start:start + chunk_samples], dtype=np.float32)
                    wav.write(block / full_scale_voltage)
        temp_path.replace(wav_path)
        return full_scale_voltage
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise


@dataclass(frozen=True)
class AcquisitionConfig:
    mode: str
    backend: str
    port: str
    host: str
    tcp_port: int
    channels: int
    sample_rate: int
    frame_seconds: float
    sensor: str
    sensitivity_mv_pa: float
    output_dir: Path
    duration: float
    if_pu: int
    iepe_channels: tuple[int, ...]

    @property
    def data_size(self) -> int:
        return round(self.sample_rate * self.frame_seconds)

    @property
    def labels(self) -> list[str]:
        if self.channels == 4 and self.sensor == "USP_3D":
            return ["P", "X", "Y", "Z"]
        if self.channels == 4 and self.sensor == "PU_1D":
            return ["P", "U", "CH3", "CH4"]
        return [f"CH{i}" for i in range(1, self.channels + 1)]


def parse_args() -> AcquisitionConfig:
    parser = argparse.ArgumentParser(description="声学矢量传感器实时采集与 HDF5 录制")
    parser.add_argument("--mode", choices=("usb", "tcp"), default="usb")
    parser.add_argument("--backend", choices=("auto", "native", "sdk"), default="auto",
                        help="macOS USB 自动使用 native，其余情况使用官方 SDK")
    parser.add_argument("--port", default="auto", help="USB 串口；auto 自动寻找 STM32 设备")
    parser.add_argument("--host", default="192.168.1.10", help="TCP 数据分析仪 IP")
    parser.add_argument("--tcp-port", type=int, default=5001)
    parser.add_argument("--channels", type=int, default=4)
    parser.add_argument("--sample-rate", type=int, default=32000, choices=SAMPLE_RATES)
    parser.add_argument("--frame-seconds", type=float, default=0.1)
    parser.add_argument("--sensor", choices=("USP_3D", "PU_1D"), default="USP_3D")
    parser.add_argument("--sensitivity-mv-pa", type=float, default=50.0, help="P 通道灵敏度，mV/Pa")
    parser.add_argument("--output-dir", type=Path, default=Path("records"))
    parser.add_argument("--duration", type=float, default=0.0, help="秒；0 表示持续到 Ctrl+C")
    parser.add_argument("--if-pu", type=int, choices=(0, 1, 2), default=0)
    parser.add_argument("--iepe", default="", help="TCP IEPE 通道，例如 1,2,3,4")
    args = parser.parse_args()
    if args.channels < 1 or args.frame_seconds <= 0 or args.sensitivity_mv_pa <= 0 or args.duration < 0:
        parser.error("channels、frame-seconds、sensitivity-mv-pa 必须为正；duration 不可为负")
    try:
        iepe = tuple(int(item.strip()) for item in args.iepe.split(",") if item.strip())
    except ValueError:
        parser.error("--iepe 必须是逗号分隔的通道编号")
    if any(channel < 1 or channel > args.channels for channel in iepe):
        parser.error("--iepe 通道必须在 1 到 channels 之间")
    settings = vars(args)
    settings.pop("iepe")
    return AcquisitionConfig(**settings, iepe_channels=iepe)


def select_backend(cfg: AcquisitionConfig) -> str:
    if cfg.backend != "auto":
        return cfg.backend
    return "native" if cfg.mode == "usb" and platform.system() == "Darwin" else "sdk"


def find_usb_port(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise RuntimeError("缺少 pyserial，请运行：python3 -m pip install pyserial") from exc
    ports = list(list_ports.comports())
    matches = [p.device for p in ports if (p.vid, p.pid) == (STM32_VID, STM32_PID)]
    if not matches:
        matches = [p.device for p in ports if "STM32" in (p.description or "").upper()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        found = ", ".join(p.device for p in ports) or "无"
        raise RuntimeError(f"未找到 STM32 声学采集卡；当前串口：{found}")
    raise RuntimeError(f"发现多个 STM32 串口，请用 --port 指定：{', '.join(matches)}")


def report_data(data: np.ndarray, cfg: AcquisitionConfig, samples: int, elapsed: float) -> None:
    rms_v = np.sqrt(np.mean(np.square(data.astype(np.float64)), axis=0))
    readings = [f"{label}={value * 1000:.3f} mV" for label, value in zip(cfg.labels, rms_v)]
    pressure_pa = rms_v[0] / (cfg.sensitivity_mv_pa / 1000.0)
    spl_db = 20 * np.log10(max(pressure_pa, 1e-20) / 20e-6)
    print(f"[{elapsed:7.2f}s] {samples:10d} 点/通道 | " + " | ".join(readings)
          + f" | P={pressure_pa:.4g} Pa, {spl_db:.1f} dB SPL", flush=True)


def metadata(cfg: AcquisitionConfig, backend: str) -> dict[str, Any]:
    return {
        "application": "acoustic_signal_acquisition", "backend": backend,
        "sensor_layout": cfg.sensor, "channel_labels": json.dumps(cfg.labels, ensure_ascii=False),
        "sensitivity_mv_pa": cfg.sensitivity_mv_pa, "connection_mode": cfg.mode,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def run_native_usb(cfg: AcquisitionConfig, output: Path) -> int:
    if cfg.mode != "usb":
        raise RuntimeError("native 后端目前仅支持 USB；TCP 请使用 --backend sdk")
    if cfg.sample_rate != 32000:
        raise RuntimeError("macOS 原生后端目前仅支持设备默认 32000 Hz；请使用 --sample-rate 32000")
    try:
        import h5py
        import serial
    except ImportError as exc:
        raise RuntimeError("缺少依赖，请运行：python3 -m pip install pyserial numpy h5py") from exc

    port = find_usb_port(cfg.port)
    bytes_per_tuple = cfg.channels * 4
    read_size = cfg.data_size * bytes_per_tuple
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    sample_count = 0
    target_samples = round(cfg.duration * cfg.sample_rate) if cfg.duration else 0
    raw_buffer = bytearray()
    report_blocks: list[np.ndarray] = []
    serial_port = serial.Serial(port, 115200, timeout=0.2, write_timeout=1.0)
    try:
        serial_port.reset_input_buffer()
        serial_port.write(USB_START_COMMAND)
        serial_port.flush()
        wav_output = output.with_suffix(".wav")
        with h5py.File(output, "w") as h5, Float32WavWriter(
            wav_output, cfg.sample_rate, cfg.channels
        ) as wav:
            data_set = h5.create_dataset(
                "data", shape=(0, cfg.channels), maxshape=(None, cfg.channels),
                chunks=(cfg.data_size, cfg.channels), dtype="<f4",
            )
            for key, value in metadata(cfg, "native-macos-usb").items():
                data_set.attrs[key] = value
            data_set.attrs["channels"] = cfg.channels
            data_set.attrs["sample_rate"] = cfg.sample_rate
            data_set.attrs["data_size"] = cfg.data_size
            data_set.attrs["unit"] = "V"
            data_set.attrs["raw_dtype"] = "little-endian int32"
            started = time.monotonic()
            next_report = started + 1.0
            print(f"开始采集：{port}，4 通道，32000 Hz；输出：{output.resolve()}")
            while not stopping:
                if target_samples and sample_count >= target_samples:
                    break
                block = serial_port.read(max(read_size - len(raw_buffer), 1))
                if block:
                    raw_buffer.extend(block)
                usable = len(raw_buffer) - len(raw_buffer) % bytes_per_tuple
                if not usable:
                    continue
                raw = bytes(raw_buffer[:usable])
                del raw_buffer[:usable]
                values = np.frombuffer(raw, dtype="<i4").reshape(-1, cfg.channels)
                volts = (values / USB_CONVERSION_FACTOR).astype(np.float32)
                if target_samples:
                    volts = volts[:target_samples - sample_count]
                old_size = data_set.shape[0]
                data_set.resize(old_size + len(volts), axis=0)
                data_set[old_size:] = volts
                wav.write(volts)
                sample_count += len(volts)
                report_blocks.append(volts)
                now = time.monotonic()
                if now >= next_report:
                    report_data(np.concatenate(report_blocks), cfg, sample_count, now - started)
                    report_blocks.clear()
                    h5.flush()
                    next_report = now + 1.0
            data_set.attrs["completed_at"] = datetime.now().isoformat(timespec="seconds")
            data_set.attrs["samples_per_channel"] = sample_count
            data_set.attrs["duration_seconds"] = sample_count / cfg.sample_rate
    finally:
        try:
            serial_port.write(USB_STOP_COMMAND)
            serial_port.flush()
            time.sleep(0.1)
        finally:
            serial_port.close()
    print(f"采集完成：{sample_count} 点/通道，{sample_count / cfg.sample_rate:.3f} 秒")
    print(f"WAV 已保存：{output.with_suffix('.wav').resolve()}")
    return 0


def run_sdk(cfg: AcquisitionConfig, output: Path) -> int:
    try:
        from daq_acquisition import DaqAcquisition
        from daq_acquisition.daq_errors import DaqError
    except ImportError as exc:
        raise RuntimeError("当前模式需要官方 SDK；交付 wheel 仅支持 Windows x64 Python 3.12") from exc
    kwargs = dict(channels=cfg.channels, data_size=cfg.data_size, sample_rate=cfg.sample_rate)
    if cfg.mode == "usb":
        daq = DaqAcquisition.usb(port="COM5" if cfg.port == "auto" else cfg.port, **kwargs)
    else:
        daq = DaqAcquisition.tcp(host=cfg.host, tcp_port=cfg.tcp_port, if_pu=cfg.if_pu, **kwargs)
    frames = daq.subscribe(maxsize=100)
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    frame_count = 0
    wav = Float32WavWriter(output.with_suffix(".wav"), cfg.sample_rate, cfg.channels)
    try:
        if cfg.mode == "usb":
            result = daq.get_sample_rate()
            current = int(result["sample_rate"] if isinstance(result, dict) else result)
            if current != cfg.sample_rate:
                daq.set_sample_rate(cfg.sample_rate)
        else:
            print(f"TCP 设备：{daq.query_device()}")
            for channel in cfg.iepe_channels:
                daq.set_iepe(channel, True)
        daq.start_record(output, metadata=metadata(cfg, "official-sdk"))
        daq.start()
        started = time.monotonic()
        next_report = started
        while not stopping:
            elapsed = time.monotonic() - started
            if cfg.duration and elapsed >= cfg.duration:
                break
            try:
                frame = frames.get(timeout=0.5)
            except queue.Empty:
                continue
            frame_count += 1
            wav.write(frame.data)
            if time.monotonic() >= next_report:
                report_data(frame.data, cfg, frame_count * cfg.data_size, elapsed)
                next_report += 1.0
        return 0
    except DaqError as exc:
        print(f"采集卡错误：{exc.to_dict()}", file=sys.stderr)
        return 2
    finally:
        wav.close()
        daq.unsubscribe(frames)
        daq.close()


def main() -> int:
    cfg = parse_args()
    if cfg.sensor == "USP_3D" and cfg.channels != 4:
        raise SystemExit("USP_3D 必须使用 4 通道：CH1=P，CH2=X，CH3=Y，CH4=Z")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    output = cfg.output_dir / f"acoustic_{datetime.now():%Y%m%d_%H%M%S}_{cfg.channels}ch.h5"
    try:
        backend = select_backend(cfg)
        code = run_native_usb(cfg, output) if backend == "native" else run_sdk(cfg, output)
    except (RuntimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    if output.exists():
        print(f"HDF5 已保存：{output.resolve()}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
