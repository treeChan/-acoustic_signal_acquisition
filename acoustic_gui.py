#!/usr/bin/env python3
"""声学矢量传感器上位机：P/X/Y/Z 时域、频域、时频域显示与同步录制。"""
from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pyqtgraph as pg
import serial
from PyQt5 import QtCore, QtGui, QtWidgets
from serial.tools import list_ports

from acoustic_acquisition import (
    Float32WavWriter,
    PCM_HEADROOM,
    STM32_PID,
    STM32_VID,
    USB_CONVERSION_FACTOR,
    USB_START_COMMAND,
    USB_STOP_COMMAND,
    export_h5_data_to_pcm_wav,
    pcm_full_scale_voltage,
)


CHANNELS = 4
SAMPLE_RATE = 32000
FRAME_SAMPLES = 3200
LABELS = ("P", "X", "Y", "Z")
COLORS = ("#58a6ff", "#3fb950", "#d29922", "#f778ba")


def default_output_dir() -> Path:
    """使用用户文档目录，避免打包后尝试写入只读的 .app 内部。"""
    return Path.home() / "Documents" / "AcousticVectorRecords"


class AcquisitionWorker(QtCore.QObject):
    data_ready = QtCore.pyqtSignal(object)
    monitoring_started = QtCore.pyqtSignal()
    recording_started = QtCore.pyqtSignal(str, str)
    recording_finalizing = QtCore.pyqtSignal(str, int)
    recording_completed = QtCore.pyqtSignal(str, str, int)
    recording_export_failed = QtCore.pyqtSignal(str)
    failed = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal()

    def __init__(self, port: str) -> None:
        super().__init__()
        self.port = port
        self._stop = threading.Event()
        self._record_stop = threading.Event()
        self._record_lock = threading.Lock()
        self._record_busy = threading.Event()
        self._conversion_thread: threading.Thread | None = None
        self._record_request: tuple[Path, float, float, float, float, str, int] | None = None

    def request_stop(self) -> None:
        self._stop.set()

    def request_recording(
        self, output_dir: Path, duration: float, sensitivity: float,
        azimuth_deg: float, elevation_deg: float, note: str, wav_bit_depth: int,
    ) -> bool:
        with self._record_lock:
            if self._record_request is not None or self._record_busy.is_set():
                return False
            if wav_bit_depth not in (16, 24, 32):
                return False
            self._record_request = (
                output_dir, duration, sensitivity, azimuth_deg, elevation_deg,
                note.strip(), wav_bit_depth,
            )
            self._record_busy.set()
            self._record_stop.clear()
            return True

    def request_record_stop(self) -> None:
        self._record_stop.set()

    def _take_record_request(self) -> tuple[Path, float, float, float, float, str, int] | None:
        with self._record_lock:
            request = self._record_request
            self._record_request = None
            return request

    @QtCore.pyqtSlot()
    def run(self) -> None:
        serial_port = None
        h5 = None
        wav = None
        dataset = None
        h5_path: Path | None = None
        wav_path: Path | None = None
        record_samples = 0
        record_target = 0
        record_peak_voltage = 0.0
        record_metadata: dict[str, Any] = {}
        raw_buffer = bytearray()

        def finish_recording_files(
            finished_h5_path: Path, finished_wav_path: Path, finished_samples: int,
            finished_metadata: dict[str, Any], completed_at: str,
        ) -> None:
            wav_bit_depth = int(finished_metadata["wav_bit_depth"])
            wav_encoding = "IEEE_FLOAT" if wav_bit_depth == 32 else "PCM"
            wav_details: dict[str, Any] = {
                "bit_depth": wav_bit_depth,
                "encoding": wav_encoding,
                "channels": list(LABELS),
                "sample_rate_hz": SAMPLE_RATE,
                "normalized": wav_bit_depth != 32,
            }
            if wav_bit_depth != 32:
                wav_details.update({
                    "normalization_scope": "one common gain for P/X/Y/Z",
                    "full_scale_voltage_v": finished_metadata["wav_full_scale_voltage_v"],
                    "headroom": PCM_HEADROOM,
                    "positive_pcm_full_scale": 32767 if wav_bit_depth == 16 else 8388607,
                    "voltage_reconstruction": (
                        "volts = pcm_integer / positive_pcm_full_scale * full_scale_voltage_v"
                    ),
                })
            sidecar = {
                "schema_version": 2,
                "recording_id": finished_h5_path.stem,
                "task": "sound_source_localization",
                "files": {"h5": finished_h5_path.name, "wav": finished_wav_path.name},
                "sample_rate_hz": SAMPLE_RATE,
                "channels": list(LABELS),
                "unit": "V",
                "h5_storage": {"dtype": "float32", "unit": "V", "normalized": False},
                "wav_storage": wav_details,
                "coordinate_system": {
                    "azimuth": "+X=0 deg, positive toward +Y",
                    "elevation": "XY plane=0 deg, positive toward +Z",
                },
                "ground_truth": {
                    "azimuth_deg": finished_metadata["azimuth_deg"],
                    "elevation_deg": finished_metadata["elevation_deg"],
                },
                "note": finished_metadata["note"],
                "quality": "unreviewed",
                "created_at": finished_metadata["created_at"],
                "completed_at": completed_at,
                "samples_per_channel": finished_samples,
                "duration_seconds": finished_samples / SAMPLE_RATE,
            }
            json_path = finished_h5_path.with_suffix(".json")
            json_path.write_text(
                json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            with (finished_h5_path.parent / "manifest.jsonl").open("a", encoding="utf-8") as manifest:
                manifest.write(json.dumps(sidecar, ensure_ascii=False) + "\n")
            self._record_busy.clear()
            self.recording_completed.emit(
                str(finished_h5_path.resolve()), str(finished_wav_path.resolve()), finished_samples
            )

        def close_recording() -> None:
            nonlocal h5, wav, dataset, h5_path, wav_path
            nonlocal record_samples, record_target, record_peak_voltage, record_metadata
            if dataset is None or h5 is None or h5_path is None or wav_path is None:
                return
            completed_at = datetime.now().isoformat(timespec="seconds")
            wav_bit_depth = int(record_metadata["wav_bit_depth"])
            full_scale_voltage = (
                pcm_full_scale_voltage(record_peak_voltage) if wav_bit_depth in (16, 24) else None
            )
            dataset.attrs["completed_at"] = completed_at
            dataset.attrs["samples_per_channel"] = record_samples
            dataset.attrs["duration_seconds"] = record_samples / SAMPLE_RATE
            dataset.attrs["wav_bit_depth"] = wav_bit_depth
            dataset.attrs["wav_encoding"] = "IEEE_FLOAT" if wav_bit_depth == 32 else "PCM"
            dataset.attrs["wav_normalized"] = wav_bit_depth != 32
            if full_scale_voltage is not None:
                dataset.attrs["wav_full_scale_voltage_v"] = full_scale_voltage
                dataset.attrs["wav_pcm_headroom"] = PCM_HEADROOM
                record_metadata["wav_full_scale_voltage_v"] = full_scale_voltage
            if wav is not None:
                wav.close()
            h5.close()
            finished_h5_path = h5_path
            finished_wav_path = wav_path
            finished_samples = record_samples
            finished_peak_voltage = record_peak_voltage
            finished_metadata = record_metadata.copy()
            h5 = wav = dataset = h5_path = wav_path = None
            record_samples = 0
            record_target = 0
            record_peak_voltage = 0.0
            record_metadata = {}
            self._record_stop.clear()
            if wav_bit_depth == 32:
                finish_recording_files(
                    finished_h5_path, finished_wav_path, finished_samples,
                    finished_metadata, completed_at,
                )
                return

            self.recording_finalizing.emit(str(finished_wav_path.resolve()), wav_bit_depth)

            def export_pcm() -> None:
                try:
                    export_h5_data_to_pcm_wav(
                        finished_h5_path, finished_wav_path, SAMPLE_RATE, CHANNELS,
                        wav_bit_depth, peak_voltage=finished_peak_voltage,
                    )
                    finish_recording_files(
                        finished_h5_path, finished_wav_path, finished_samples,
                        finished_metadata, completed_at,
                    )
                except Exception as exc:
                    self._record_busy.clear()
                    self.recording_export_failed.emit(
                        f"生成 {wav_bit_depth}-bit PCM WAV 失败：{type(exc).__name__}: {exc}；HDF5 已保留"
                    )

            self._conversion_thread = threading.Thread(
                target=export_pcm, name="pcm-wav-export", daemon=False
            )
            self._conversion_thread.start()

        try:
            serial_port = serial.Serial(self.port, 115200, timeout=0.2, write_timeout=1.0)
            serial_port.reset_input_buffer()
            serial_port.write(USB_START_COMMAND)
            serial_port.flush()
            self.monitoring_started.emit()
            while not self._stop.is_set():
                request = self._take_record_request()
                if request is not None:
                    if dataset is not None:
                        self._record_busy.clear()
                        self.failed.emit("已有录制正在进行，请先停止当前录制")
                    else:
                        output_dir, duration, sensitivity, azimuth_deg, elevation_deg, note, wav_bit_depth = request
                        output_dir.mkdir(parents=True, exist_ok=True)
                        h5_path = output_dir / f"acoustic_{datetime.now():%Y%m%d_%H%M%S}_4ch.h5"
                        wav_path = h5_path.with_suffix(".wav")
                        h5 = h5py.File(h5_path, "w")
                        wav = (
                            Float32WavWriter(wav_path, SAMPLE_RATE, CHANNELS)
                            if wav_bit_depth == 32 else None
                        )
                        dataset = h5.create_dataset(
                            "data", shape=(0, CHANNELS), maxshape=(None, CHANNELS),
                            chunks=(FRAME_SAMPLES, CHANNELS), dtype="<f4",
                        )
                        created_at = datetime.now().isoformat(timespec="seconds")
                        record_metadata = {
                            "azimuth_deg": azimuth_deg,
                            "elevation_deg": elevation_deg,
                            "note": note,
                            "created_at": created_at,
                            "wav_bit_depth": wav_bit_depth,
                        }
                        dataset.attrs.update({
                            "application": "acoustic_vector_gui",
                            "backend": "native-macos-usb",
                            "sensor_layout": "USP_3D",
                            "channel_labels": json.dumps(LABELS),
                            "channels": CHANNELS,
                            "sample_rate": SAMPLE_RATE,
                            "data_size": FRAME_SAMPLES,
                            "unit": "V",
                            "sensitivity_mv_pa": sensitivity,
                            "created_at": created_at,
                            "task": "sound_source_localization",
                            "ground_truth_azimuth_deg": azimuth_deg,
                            "ground_truth_elevation_deg": elevation_deg,
                            "coordinate_system": "+X=azimuth 0 deg; +Y=positive azimuth; +Z=positive elevation",
                            "note": note,
                        })
                        record_samples = 0
                        record_target = round(duration * SAMPLE_RATE) if duration else 0
                        self.recording_started.emit(str(h5_path.resolve()), str(wav_path.resolve()))

                if dataset is not None and self._record_stop.is_set():
                    close_recording()

                block = serial_port.read(max(FRAME_SAMPLES * CHANNELS * 4 - len(raw_buffer), 1))
                if block:
                    raw_buffer.extend(block)
                usable = len(raw_buffer) - len(raw_buffer) % (CHANNELS * 4)
                if not usable:
                    continue
                raw = bytes(raw_buffer[:usable])
                del raw_buffer[:usable]
                values = np.frombuffer(raw, dtype="<i4").reshape(-1, CHANNELS)
                volts = (values / USB_CONVERSION_FACTOR).astype(np.float32)
                self.data_ready.emit(volts.copy())

                if dataset is not None:
                    record_block = volts
                    if record_target:
                        record_block = record_block[:record_target - record_samples]
                    old_size = dataset.shape[0]
                    dataset.resize(old_size + len(record_block), axis=0)
                    dataset[old_size:] = record_block
                    if wav is not None:
                        wav.write(record_block)
                    if record_block.size:
                        record_peak_voltage = max(
                            record_peak_voltage, float(np.max(np.abs(record_block)))
                        )
                    record_samples += len(record_block)
                    if record_target and record_samples >= record_target:
                        close_recording()
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            try:
                close_recording()
            except Exception as exc:
                self.failed.emit(f"关闭录制文件失败：{exc}")
            if self._conversion_thread is not None:
                self._conversion_thread.join()
            if serial_port is not None:
                try:
                    serial_port.write(USB_STOP_COMMAND)
                    serial_port.flush()
                    time.sleep(0.1)
                except Exception:
                    pass
                serial_port.close()
            self.finished.emit()


class AcousticMainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("声学矢量信号采集系统 · PXYZ")
        self.resize(1480, 900)
        self.thread: QtCore.QThread | None = None
        self.worker: AcquisitionWorker | None = None
        self.is_recording = False
        self.is_finalizing = False
        self.samples_received = 0
        self.started_at = 0.0
        self.ring: deque[np.ndarray] = deque()
        self.ring_samples = 0
        self.latest_data = np.empty((0, CHANNELS), dtype=np.float32)
        self.tf_history: deque[tuple[float, np.ndarray]] = deque()
        self.tf_fft_size = 0
        self.tf_last_sample_count = 0
        self._build_ui()
        self.refresh_ports()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_display)
        self.timer.start(150)

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        title_row = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("声学矢量信号采集系统")
        title.setObjectName("title")
        title_row.addWidget(title)
        title_row.addStretch()
        self.state_badge = QtWidgets.QLabel("● 未连接")
        self.state_badge.setObjectName("stateBadge")
        title_row.addWidget(self.state_badge)
        layout.addLayout(title_row)

        controls = QtWidgets.QGroupBox("采集设置")
        grid = QtWidgets.QGridLayout(controls)
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setMinimumWidth(290)
        refresh = QtWidgets.QPushButton("刷新设备")
        refresh.clicked.connect(self.refresh_ports)
        self.duration_spin = QtWidgets.QDoubleSpinBox()
        self.duration_spin.setRange(0, 86400)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setSuffix(" s（0=持续）")
        self.sensitivity_spin = QtWidgets.QDoubleSpinBox()
        self.sensitivity_spin.setRange(0.001, 100000)
        self.sensitivity_spin.setValue(50.0)
        self.sensitivity_spin.setDecimals(3)
        self.sensitivity_spin.setSuffix(" mV/Pa")
        self.fft_combo = QtWidgets.QComboBox()
        self.fft_combo.addItems(("2048", "4096", "8192", "16384"))
        self.fft_combo.setCurrentText("8192")
        self.max_freq_spin = QtWidgets.QSpinBox()
        self.max_freq_spin.setRange(100, SAMPLE_RATE // 2)
        self.max_freq_spin.setValue(10000)
        self.max_freq_spin.setSingleStep(500)
        self.max_freq_spin.setSuffix(" Hz")
        self.unit_combo = QtWidgets.QComboBox()
        self.unit_combo.addItems(("mV", "V"))
        self.wav_depth_combo = QtWidgets.QComboBox()
        self.wav_depth_combo.addItem("16-bit PCM", 16)
        self.wav_depth_combo.addItem("24-bit PCM", 24)
        self.wav_depth_combo.addItem("32-bit float（推荐）", 32)
        self.wav_depth_combo.setCurrentIndex(2)
        self.wav_depth_combo.setToolTip(
            "只改变 WAV 文件编码；HDF5 始终保存未归一化的 float32 电压值。"
        )
        self.azimuth_spin = QtWidgets.QDoubleSpinBox()
        self.azimuth_spin.setRange(-180.0, 180.0)
        self.azimuth_spin.setDecimals(1)
        self.azimuth_spin.setSingleStep(5.0)
        self.azimuth_spin.setSuffix("°")
        self.elevation_spin = QtWidgets.QDoubleSpinBox()
        self.elevation_spin.setRange(-90.0, 90.0)
        self.elevation_spin.setDecimals(1)
        self.elevation_spin.setSingleStep(5.0)
        self.elevation_spin.setSuffix("°")
        self.note_edit = QtWidgets.QLineEdit()
        self.note_edit.setPlaceholderText("可选，例如：正前方扬声器、低信噪比")
        self.output_edit = QtWidgets.QLineEdit(str(default_output_dir()))
        browse = QtWidgets.QPushButton("选择目录")
        browse.clicked.connect(self.choose_output_dir)
        self.start_button = QtWidgets.QPushButton("▶ 开始显示")
        self.start_button.setObjectName("startButton")
        self.start_button.clicked.connect(self.start_acquisition)
        self.stop_button = QtWidgets.QPushButton("■ 停止显示")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_acquisition)
        self.record_button = QtWidgets.QPushButton("● 开始录制")
        self.record_button.setObjectName("recordButton")
        self.record_button.setEnabled(False)
        self.record_button.clicked.connect(self.start_recording)
        self.stop_record_button = QtWidgets.QPushButton("■ 停止录制")
        self.stop_record_button.setEnabled(False)
        self.stop_record_button.clicked.connect(self.stop_recording)

        grid.addWidget(QtWidgets.QLabel("USB 设备"), 0, 0)
        grid.addWidget(self.port_combo, 0, 1)
        grid.addWidget(refresh, 0, 2)
        grid.addWidget(QtWidgets.QLabel("采样率"), 0, 3)
        grid.addWidget(QtWidgets.QLabel("32000 Hz"), 0, 4)
        grid.addWidget(QtWidgets.QLabel("录制时长"), 0, 5)
        grid.addWidget(self.duration_spin, 0, 6)
        grid.addWidget(QtWidgets.QLabel("P 灵敏度"), 1, 0)
        grid.addWidget(self.sensitivity_spin, 1, 1)
        grid.addWidget(QtWidgets.QLabel("FFT 点数"), 1, 3)
        grid.addWidget(self.fft_combo, 1, 4)
        grid.addWidget(QtWidgets.QLabel("最高频率"), 1, 5)
        grid.addWidget(self.max_freq_spin, 1, 6)
        grid.addWidget(QtWidgets.QLabel("显示单位"), 0, 7)
        grid.addWidget(self.unit_combo, 0, 8)
        grid.addWidget(QtWidgets.QLabel("WAV 文件位深"), 1, 7)
        grid.addWidget(self.wav_depth_combo, 1, 8)
        grid.addWidget(QtWidgets.QLabel("保存目录"), 2, 0)
        grid.addWidget(self.output_edit, 2, 1, 1, 4)
        grid.addWidget(browse, 2, 5)
        display_buttons = QtWidgets.QHBoxLayout()
        display_buttons.addWidget(self.start_button)
        display_buttons.addWidget(self.stop_button)
        record_buttons = QtWidgets.QHBoxLayout()
        record_buttons.addWidget(self.record_button)
        record_buttons.addWidget(self.stop_record_button)
        grid.addLayout(display_buttons, 2, 6, 1, 2)
        grid.addLayout(record_buttons, 2, 8)
        truth_row = QtWidgets.QHBoxLayout()
        truth_row.addWidget(QtWidgets.QLabel("定位真值"))
        truth_row.addWidget(QtWidgets.QLabel("方位角"))
        truth_row.addWidget(self.azimuth_spin)
        truth_row.addWidget(QtWidgets.QLabel("俯仰角"))
        truth_row.addWidget(self.elevation_spin)
        truth_row.addWidget(QtWidgets.QLabel("备注"))
        truth_row.addWidget(self.note_edit, 1)
        truth_row.addWidget(QtWidgets.QLabel("坐标：+X=0°，朝+Y为方位正，朝+Z为俯仰正"))
        grid.addLayout(truth_row, 3, 0, 1, 9)
        layout.addWidget(controls)

        metrics = QtWidgets.QHBoxLayout()
        self.metric_labels: list[QtWidgets.QLabel] = []
        for name, color in zip(LABELS, COLORS):
            card = QtWidgets.QFrame()
            card.setObjectName("metricCard")
            card_layout = QtWidgets.QVBoxLayout(card)
            heading = QtWidgets.QLabel(f"{name} 通道")
            heading.setStyleSheet(f"color: {color}; font-weight: 700;")
            value = QtWidgets.QLabel("-- mV RMS")
            value.setObjectName("metricValue")
            card_layout.addWidget(heading)
            card_layout.addWidget(value)
            metrics.addWidget(card)
            self.metric_labels.append(value)
        spl_card = QtWidgets.QFrame()
        spl_card.setObjectName("metricCard")
        spl_layout = QtWidgets.QVBoxLayout(spl_card)
        spl_layout.addWidget(QtWidgets.QLabel("P 通道声压级"))
        self.spl_label = QtWidgets.QLabel("-- dB SPL")
        self.spl_label.setObjectName("metricValue")
        spl_layout.addWidget(self.spl_label)
        metrics.addWidget(spl_card)
        layout.addLayout(metrics)

        self.signal_tabs = QtWidgets.QTabWidget()
        spectrum_page = QtWidgets.QWidget()
        spectrum_page_layout = QtWidgets.QVBoxLayout(spectrum_page)
        spectrum_page_layout.setContentsMargins(4, 8, 4, 4)
        self.spectrum_group = QtWidgets.QGroupBox("实时频谱 · 单边 RMS 幅值")
        spectrum_group = self.spectrum_group
        spectrum_layout = QtWidgets.QVBoxLayout(spectrum_group)
        self.graphics = pg.GraphicsLayoutWidget()
        self.plots: list[pg.PlotItem] = []
        self.curves: list[pg.PlotDataItem] = []
        for index, (name, color) in enumerate(zip(LABELS, COLORS)):
            plot = self.graphics.addPlot(row=index // 2, col=index % 2, title=f"{name} 通道频谱")
            plot.showGrid(x=True, y=True, alpha=0.22)
            plot.setLabel("bottom", "频率", units="Hz")
            plot.setLabel("left", "幅值", units="mV")
            plot.getAxis("left").enableAutoSIPrefix(False)
            plot.setXRange(0, 10000, padding=0)
            plot.enableAutoRange(axis="y")
            curve = plot.plot(pen=pg.mkPen(color, width=1.5))
            self.plots.append(plot)
            self.curves.append(curve)
        spectrum_layout.addWidget(self.graphics)
        spectrum_page_layout.addWidget(spectrum_group)
        self.signal_tabs.addTab(spectrum_page, "频域信号")

        time_page = QtWidgets.QWidget()
        time_page_layout = QtWidgets.QVBoxLayout(time_page)
        time_page_layout.setContentsMargins(4, 8, 4, 4)
        self.time_group = QtWidgets.QGroupBox("实时波形 · 瞬时电压")
        time_group = self.time_group
        time_layout = QtWidgets.QVBoxLayout(time_group)
        time_controls = QtWidgets.QHBoxLayout()
        time_controls.addWidget(QtWidgets.QLabel("显示窗口"))
        self.time_window_combo = QtWidgets.QComboBox()
        self.time_window_combo.addItems(("20 ms", "50 ms", "100 ms", "200 ms", "500 ms", "1000 ms"))
        self.time_window_combo.setCurrentText("100 ms")
        time_controls.addWidget(self.time_window_combo)
        time_controls.addStretch()
        time_controls.addWidget(QtWidgets.QLabel("横轴：相对当前时刻 · 纵轴：通道电压"))
        time_layout.addLayout(time_controls)
        self.time_graphics = pg.GraphicsLayoutWidget()
        self.time_plots: list[pg.PlotItem] = []
        self.time_curves: list[pg.PlotDataItem] = []
        for index, (name, color) in enumerate(zip(LABELS, COLORS)):
            plot = self.time_graphics.addPlot(row=index // 2, col=index % 2, title=f"{name} 通道时域波形")
            plot.showGrid(x=True, y=True, alpha=0.22)
            plot.setLabel("bottom", "时间", units="ms")
            plot.setLabel("left", "电压", units="mV")
            plot.getAxis("left").enableAutoSIPrefix(False)
            plot.setXRange(-100, 0, padding=0)
            plot.enableAutoRange(axis="y")
            curve = plot.plot(pen=pg.mkPen(color, width=1.2))
            self.time_plots.append(plot)
            self.time_curves.append(curve)
        time_layout.addWidget(self.time_graphics)
        time_page_layout.addWidget(time_group)
        self.signal_tabs.addTab(time_page, "时域信号")

        time_frequency_page = QtWidgets.QWidget()
        time_frequency_page_layout = QtWidgets.QVBoxLayout(time_frequency_page)
        time_frequency_page_layout.setContentsMargins(4, 8, 4, 4)
        self.tf_group = QtWidgets.QGroupBox("实时声谱图 · 单边 RMS 幅值")
        tf_layout = QtWidgets.QVBoxLayout(self.tf_group)
        tf_controls = QtWidgets.QHBoxLayout()
        tf_controls.addWidget(QtWidgets.QLabel("历史长度"))
        self.tf_history_combo = QtWidgets.QComboBox()
        self.tf_history_combo.addItems(("5 s", "10 s", "20 s", "30 s"))
        self.tf_history_combo.setCurrentText("10 s")
        tf_controls.addWidget(self.tf_history_combo)
        tf_controls.addSpacing(18)
        tf_controls.addWidget(QtWidgets.QLabel("颜色幅值"))
        self.tf_color_legend = QtWidgets.QLabel("低                 高")
        self.tf_color_legend.setAlignment(QtCore.Qt.AlignCenter)
        self.tf_color_legend.setFixedWidth(180)
        self.tf_color_legend.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #440154, stop:0.25 #3b528b, stop:0.5 #21918c, "
            "stop:0.75 #5ec962, stop:1 #fde725); color: white; "
            "border: 1px solid #5c6370; border-radius: 3px; padding: 3px;"
        )
        tf_controls.addWidget(self.tf_color_legend)
        self.tf_scale_label = QtWidgets.QLabel("范围：-- mV RMS")
        tf_controls.addWidget(self.tf_scale_label)
        tf_controls.addStretch()
        tf_controls.addWidget(QtWidgets.QLabel("横轴：相对当前时刻 · 纵轴：频率 · PXYZ 共用色标"))
        tf_layout.addLayout(tf_controls)

        self.tf_graphics = pg.GraphicsLayoutWidget()
        self.tf_plots: list[pg.PlotItem] = []
        self.tf_images: list[pg.ImageItem] = []
        self.tf_lut = pg.colormap.get("viridis").getLookupTable(nPts=256)
        for index, name in enumerate(LABELS):
            plot = self.tf_graphics.addPlot(
                row=index // 2, col=index % 2, title=f"{name} 通道时频图"
            )
            plot.setLabel("bottom", "相对时间", units="s")
            plot.setLabel("left", "频率", units="Hz")
            plot.setXRange(-10, 0, padding=0)
            plot.setYRange(0, 10000, padding=0)
            plot.setLimits(xMax=0, yMin=0, yMax=SAMPLE_RATE // 2)
            image = pg.ImageItem(axisOrder="row-major")
            image.setLookupTable(self.tf_lut)
            image.hide()
            plot.addItem(image)
            self.tf_plots.append(plot)
            self.tf_images.append(image)
        tf_layout.addWidget(self.tf_graphics)
        time_frequency_page_layout.addWidget(self.tf_group)
        self.signal_tabs.addTab(time_frequency_page, "时频域信号")
        layout.addWidget(self.signal_tabs, 1)

        self.status_label = QtWidgets.QLabel("就绪 · CH1=P  CH2=X  CH3=Y  CH4=Z")
        self.status_label.setObjectName("statusLabel")
        layout.addWidget(self.status_label)
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #0d1117; color: #d8dee9; font-size: 13px; }
            QLabel#title { font-size: 23px; font-weight: 700; color: #f0f6fc; }
            QLabel#stateBadge { color: #8b949e; font-weight: 700; padding: 6px 12px; }
            QGroupBox { border: 1px solid #30363d; border-radius: 8px; margin-top: 10px; padding-top: 12px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
            QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox { background: #161b22; border: 1px solid #30363d; border-radius: 5px; padding: 6px; }
            QPushButton { background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 7px 12px; }
            QPushButton:hover { background: #30363d; }
            QPushButton:disabled { color: #555d68; }
            QPushButton#startButton { background: #238636; border-color: #2ea043; color: white; font-weight: 700; }
            QPushButton#recordButton { background: #a40e26; border-color: #da3633; color: white; font-weight: 700; }
            QFrame#metricCard { background: #161b22; border: 1px solid #30363d; border-radius: 8px; }
            QLabel#metricValue { font-size: 17px; font-weight: 650; color: #f0f6fc; }
            QLabel#statusLabel { color: #8b949e; padding: 3px; }
            QTabWidget::pane { border: 1px solid #30363d; border-radius: 7px; }
            QTabBar::tab { background: #161b22; border: 1px solid #30363d; padding: 8px 24px; margin-right: 2px; }
            QTabBar::tab:selected { background: #1f6feb; color: white; }
        """)
        pg.setConfigOptions(antialias=False, background="#0d1117", foreground="#8b949e")

    def refresh_ports(self) -> None:
        current = self.port_combo.currentData()
        self.port_combo.clear()
        found = []
        for port in list_ports.comports():
            if (port.vid, port.pid) == (STM32_VID, STM32_PID) or "STM32" in (port.description or "").upper():
                found.append(port)
        if found:
            for port in found:
                self.port_combo.addItem(f"{port.description} · {port.device}", port.device)
            if current:
                index = self.port_combo.findData(current)
                if index >= 0:
                    self.port_combo.setCurrentIndex(index)
            self.state_badge.setText("● 设备已连接")
            self.state_badge.setStyleSheet("color: #3fb950; font-weight: 700;")
        else:
            self.port_combo.addItem("未找到 STM32 采集卡", None)
            self.state_badge.setText("● 未连接")
            self.state_badge.setStyleSheet("color: #f85149; font-weight: 700;")

    def choose_output_dir(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择保存目录", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def start_acquisition(self) -> None:
        port = self.port_combo.currentData()
        if not port:
            QtWidgets.QMessageBox.warning(self, "没有设备", "请连接 STM32 声学采集卡并刷新设备。")
            return
        self.samples_received = 0
        self.started_at = time.monotonic()
        self.ring.clear()
        self.ring_samples = 0
        self.latest_data = np.empty((0, CHANNELS), dtype=np.float32)
        self.tf_history.clear()
        self.tf_fft_size = 0
        self.tf_last_sample_count = 0
        for curve in self.curves + self.time_curves:
            curve.clear()
        for image in self.tf_images:
            image.hide()
        self.tf_scale_label.setText(f"范围：-- {self.unit_combo.currentText()} RMS")
        self.worker = AcquisitionWorker(port)
        self.thread = QtCore.QThread(self)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.monitoring_started.connect(self.on_monitoring_started)
        self.worker.data_ready.connect(self.on_data)
        self.worker.recording_started.connect(self.on_recording_started)
        self.worker.recording_finalizing.connect(self.on_recording_finalizing)
        self.worker.recording_completed.connect(self.on_recording_completed)
        self.worker.recording_export_failed.connect(self.on_recording_export_failed)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.on_thread_finished)
        self.thread.start()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.record_button.setEnabled(True)
        self.port_combo.setEnabled(False)
        self.state_badge.setText("● 正在显示")
        self.state_badge.setStyleSheet("color: #3fb950; font-weight: 700;")
        self.status_label.setText("正在打开设备，当前不会写入文件…")

    def stop_acquisition(self) -> None:
        if self.worker:
            self.worker.request_stop()
            self.stop_button.setEnabled(False)
            self.record_button.setEnabled(False)
            self.stop_record_button.setEnabled(False)
            self.status_label.setText("正在停止显示；如有录制，将先安全封装文件…")

    def start_recording(self) -> None:
        if not self.worker:
            QtWidgets.QMessageBox.information(self, "请先开始显示", "请先点击“开始显示”，再开始录制。")
            return
        accepted = self.worker.request_recording(
            Path(self.output_edit.text()).expanduser(),
            self.duration_spin.value(), self.sensitivity_spin.value(),
            self.azimuth_spin.value(), self.elevation_spin.value(), self.note_edit.text(),
            int(self.wav_depth_combo.currentData()),
        )
        if accepted:
            self.record_button.setEnabled(False)
            self.stop_record_button.setEnabled(True)
            self.wav_depth_combo.setEnabled(False)
            self.status_label.setText(
                f"正在创建 HDF5 和 {self.wav_depth_combo.currentText()} WAV 录制文件…"
            )

    def stop_recording(self) -> None:
        if self.worker and self.is_recording:
            self.worker.request_record_stop()
            self.stop_record_button.setEnabled(False)
            self.status_label.setText("正在停止录制并封装 HDF5/WAV；波形显示将继续…")

    @QtCore.pyqtSlot()
    def on_monitoring_started(self) -> None:
        self.status_label.setText("实时显示已启动 · 未录制 · 仅保留最近 1 秒滚动数据")

    @QtCore.pyqtSlot(str, str)
    def on_recording_started(self, h5_path: str, _wav_path: str) -> None:
        self.is_recording = True
        self.is_finalizing = False
        self.state_badge.setText("● 正在显示并录制")
        self.state_badge.setStyleSheet("color: #f85149; font-weight: 700;")
        self.status_label.setText(f"正在录制：{Path(h5_path).name}")

    @QtCore.pyqtSlot(str, int)
    def on_recording_finalizing(self, wav_path: str, bit_depth: int) -> None:
        self.is_recording = False
        self.is_finalizing = True
        self.stop_record_button.setEnabled(False)
        self.state_badge.setText("● 正在显示并保存")
        self.state_badge.setStyleSheet("color: #d29922; font-weight: 700;")
        self.status_label.setText(
            f"采集已停止，正在生成 {bit_depth}-bit PCM WAV：{Path(wav_path).name}；实时显示继续"
        )

    @QtCore.pyqtSlot(object)
    def on_data(self, data: np.ndarray) -> None:
        self.samples_received += len(data)
        self.ring.append(data)
        self.ring_samples += len(data)
        keep = max(int(self.fft_combo.currentText()), SAMPLE_RATE)
        while self.ring and self.ring_samples - len(self.ring[0]) >= keep:
            self.ring_samples -= len(self.ring.popleft())
        self.latest_data = np.concatenate(tuple(self.ring), axis=0)[-keep:]

    def update_time_frequency(
        self, spectrum: np.ndarray, frequencies: np.ndarray,
        fft_size: int, max_freq: float, unit: str, unit_scale: float,
    ) -> None:
        """更新 P/X/Y/Z 共用线性色标的滚动声谱图。"""
        if self.tf_fft_size != fft_size:
            self.tf_history.clear()
            self.tf_fft_size = fft_size
            self.tf_last_sample_count = 0
        sample_time = self.samples_received / SAMPLE_RATE
        if self.samples_received != self.tf_last_sample_count:
            self.tf_history.append((sample_time, spectrum.astype(np.float32)))
            self.tf_last_sample_count = self.samples_received

        history_seconds = float(self.tf_history_combo.currentText().split()[0])
        while self.tf_history and sample_time - self.tf_history[0][0] > history_seconds:
            self.tf_history.popleft()
        if not self.tf_history:
            return

        frequency_indices = np.flatnonzero(frequencies <= max_freq)
        if len(frequency_indices) > 512:
            positions = np.linspace(0, len(frequency_indices) - 1, 512).astype(int)
            frequency_indices = frequency_indices[positions]
        if len(frequency_indices) < 2:
            return

        image_volts = np.stack(
            [frame[frequency_indices] for _timestamp, frame in self.tf_history], axis=1
        )
        level_v = float(np.max(image_volts))
        level_v = max(level_v, 1e-12)
        level_display = level_v * unit_scale
        history_width = min(
            history_seconds,
            max(0.15, sample_time - self.tf_history[0][0] + 0.15),
        )
        image_rect = QtCore.QRectF(-history_width, 0, history_width, max_freq)
        for channel, (plot, image) in enumerate(zip(self.tf_plots, self.tf_images)):
            image.setImage(
                image_volts[:, :, channel] * unit_scale,
                autoLevels=False,
                levels=(0.0, level_display),
                autoDownsample=True,
            )
            image.setRect(image_rect)
            image.show()
            plot.setXRange(-history_seconds, 0, padding=0)
            plot.setYRange(0, max_freq, padding=0)
        precision = 3 if unit == "mV" else 6
        self.tf_group.setTitle(
            f"实时声谱图 · 单边 RMS 幅值（{unit} RMS，PXYZ 共用色标）"
        )
        self.tf_scale_label.setText(
            f"范围：0–{level_display:.{precision}f} {unit} RMS"
        )

    def update_display(self) -> None:
        data = self.latest_data
        if len(data) == 0:
            return
        unit = self.unit_combo.currentText()
        unit_scale = 1000.0 if unit == "mV" else 1.0
        self.spectrum_group.setTitle(f"实时频谱 · 单边 RMS 幅值（{unit} RMS）")
        self.time_group.setTitle(f"实时波形 · 瞬时电压（{unit}）")
        recent = data[-min(len(data), SAMPLE_RATE):]
        rms = np.sqrt(np.mean(np.square(recent.astype(np.float64)), axis=0))
        for label, value in zip(self.metric_labels, rms):
            precision = 3 if unit == "mV" else 6
            label.setText(f"{value * unit_scale:.{precision}f} {unit} RMS")
        pressure_pa = rms[0] / (self.sensitivity_spin.value() / 1000.0)
        spl = 20 * np.log10(max(pressure_pa, 1e-20) / 20e-6)
        self.spl_label.setText(f"{spl:.1f} dB SPL")

        fft_size = min(int(self.fft_combo.currentText()), len(data))
        if fft_size < 64:
            return
        segment = data[-fft_size:].astype(np.float64)
        segment -= np.mean(segment, axis=0, keepdims=True)
        window = np.hanning(fft_size)
        spectrum = np.abs(np.fft.rfft(segment * window[:, None], axis=0))
        spectrum *= 2.0 / np.sum(window) / np.sqrt(2.0)
        spectrum[0] *= 0.5
        spectrum_display = spectrum * unit_scale
        frequencies = np.fft.rfftfreq(fft_size, 1.0 / SAMPLE_RATE)
        max_freq = self.max_freq_spin.value()
        visible = frequencies <= max_freq
        for channel, (plot, curve) in enumerate(zip(self.plots, self.curves)):
            curve.setData(frequencies[visible], spectrum_display[visible, channel])
            plot.setXRange(0, max_freq, padding=0)
            plot.setLabel("left", "幅值", units=unit)

        self.update_time_frequency(
            spectrum, frequencies, fft_size, max_freq, unit, unit_scale
        )

        window_ms = int(self.time_window_combo.currentText().split()[0])
        time_samples = min(len(data), round(SAMPLE_RATE * window_ms / 1000.0))
        time_data = data[-time_samples:]
        time_axis_ms = (np.arange(time_samples) - time_samples + 1) * 1000.0 / SAMPLE_RATE
        for channel, (plot, curve) in enumerate(zip(self.time_plots, self.time_curves)):
            curve.setData(time_axis_ms, time_data[:, channel] * unit_scale)
            plot.setXRange(-window_ms, 0, padding=0)
            plot.setLabel("left", "电压", units=unit)
        elapsed = time.monotonic() - self.started_at if self.started_at else 0
        if self.is_recording:
            mode_text = "正在录制"
        elif self.is_finalizing:
            mode_text = "正在生成 PCM WAV（采集已停止）"
        else:
            mode_text = "仅显示（不写文件）"
        self.status_label.setText(
            f"{mode_text} · 已显示 {self.samples_received:,} 点/通道 · 信号时长 {self.samples_received / SAMPLE_RATE:.2f} s"
            f" · 运行 {elapsed:.2f} s · FFT 分辨率 {SAMPLE_RATE / fft_size:.2f} Hz"
        )

    @QtCore.pyqtSlot(str, str, int)
    def on_recording_completed(self, h5_path: str, wav_path: str, samples: int) -> None:
        self.is_recording = False
        self.is_finalizing = False
        self.record_button.setEnabled(self.worker is not None)
        self.stop_record_button.setEnabled(False)
        self.wav_depth_combo.setEnabled(True)
        self.state_badge.setText("● 正在显示")
        self.state_badge.setStyleSheet("color: #3fb950; font-weight: 700;")
        self.status_label.setText(
            f"录制保存完成，实时显示继续 · {samples:,} 点/通道 · HDF5: {Path(h5_path).name} · WAV: {Path(wav_path).name}"
        )

    @QtCore.pyqtSlot(str)
    def on_recording_export_failed(self, message: str) -> None:
        self.is_recording = False
        self.is_finalizing = False
        self.record_button.setEnabled(self.worker is not None)
        self.stop_record_button.setEnabled(False)
        self.wav_depth_combo.setEnabled(True)
        self.state_badge.setText("● 正在显示")
        self.state_badge.setStyleSheet("color: #3fb950; font-weight: 700;")
        self.status_label.setText(message)
        QtWidgets.QMessageBox.critical(self, "WAV 保存失败", message)

    @QtCore.pyqtSlot(str)
    def on_failed(self, message: str) -> None:
        self.status_label.setText(f"采集失败：{message}")
        QtWidgets.QMessageBox.critical(self, "采集失败", message)

    def on_thread_finished(self) -> None:
        if self.thread:
            self.thread.deleteLater()
        self.thread = None
        self.worker = None
        self.is_recording = False
        self.is_finalizing = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.record_button.setEnabled(False)
        self.stop_record_button.setEnabled(False)
        self.wav_depth_combo.setEnabled(True)
        self.port_combo.setEnabled(True)
        self.state_badge.setText("● 设备已连接")
        self.state_badge.setStyleSheet("color: #3fb950; font-weight: 700;")

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.worker and self.thread and self.thread.isRunning():
            self.worker.request_stop()
            if not self.thread.wait(3000):
                event.ignore()
                QtWidgets.QMessageBox.warning(self, "仍在保存", "请稍候，采集文件正在安全关闭。")
                return
        event.accept()


def main() -> int:
    app = QtWidgets.QApplication([])
    app.setApplicationName("声学矢量信号采集系统")
    app.setStyle("Fusion")
    window = AcousticMainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
