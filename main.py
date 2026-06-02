from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
import streamlit as st

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - Streamlit shows this at runtime.
    serial = None
    list_ports = None


CURRENT_AMPS_PER_COUNT = 1e-16
DEFAULT_DATA_FILE = "data/egcf_rga.txt"
DEFAULT_BAUD_RATE = 9600
DEFAULT_PARTIAL_PRESSURE_SENSITIVITY_MA_PER_TORR = 0.1


@dataclass(frozen=True)
class RgaRecord:
    timestamp: str
    mass: int
    counts: int
    raw_line: str


def parse_rga_line(line: str) -> RgaRecord | None:
    line = line.strip().replace("\x00", "")
    if not line.startswith("R:"):
        return None

    row = next(csv.reader([line[2:]]), [])
    if len(row) < 3:
        return None

    timestamp = row[0].strip()
    try:
        mass = int(row[1])
        counts = normalize_signed_i32(int(row[2]))
    except ValueError:
        return None

    return RgaRecord(timestamp=timestamp, mass=mass, counts=counts, raw_line=line)


def parse_csv_record(line: str) -> RgaRecord | None:
    row = next(csv.reader([line]), [])
    if not row or row[0].lower() in {"timestamp", "time"} or len(row) < 3:
        return None

    try:
        mass = int(float(row[1]))
        counts = int(float(row[2]))
        return RgaRecord(
            timestamp=row[0].strip(),
            mass=mass,
            counts=normalize_signed_i32(counts),
            raw_line=f"R:{row[0].strip()},{mass},{counts}",
        )
    except ValueError:
        return None


def normalize_signed_i32(value: int) -> int:
    if value >= 2**31:
        return value - 2**32
    if value < -(2**31):
        return value + 2**32
    return value


def load_records(path: Path) -> list[RgaRecord]:
    if not path.exists():
        return []

    records: list[RgaRecord] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as file:
        for line in file:
            record = parse_rga_line(line) or parse_csv_record(line)
            if record is not None:
                records.append(record)
    return records


def records_to_frame(
    records: Iterable[RgaRecord],
    sensitivity_ma_per_torr: float,
    detector_gain: float,
) -> pd.DataFrame:
    data = [
        {
            "timestamp": record.timestamp,
            "mass": record.mass,
            "counts": record.counts,
            "raw_line": record.raw_line,
        }
        for record in records
    ]
    if not data:
        return pd.DataFrame(
            columns=["timestamp", "mass", "counts", "current_a", "pressure_torr", "raw_line"]
        )

    frame = pd.DataFrame(data)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["timestamp"])
    frame["current_a"] = frame["counts"] * CURRENT_AMPS_PER_COUNT

    sensitivity_a_per_torr = sensitivity_ma_per_torr * 1e-3
    divisor = sensitivity_a_per_torr * detector_gain
    frame["pressure_torr"] = frame["current_a"] / divisor if divisor > 0 else pd.NA
    frame = frame.sort_values(["timestamp", "mass"]).reset_index(drop=True)
    return frame


def append_rga_lines(path: Path, records: Iterable[RgaRecord]) -> int:
    records = list(records)
    if not records:
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as file:
        for record in records:
            file.write(f"{record.raw_line}\n")
    return len(records)


def available_serial_ports() -> list[str]:
    if list_ports is None:
        return []
    return [port.device for port in list_ports.comports()]


def close_serial() -> None:
    ser = st.session_state.get("serial")
    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass
    st.session_state["serial"] = None
    st.session_state["serial_key"] = None


def ensure_serial(port: str, baud_rate: int):
    if serial is None:
        raise RuntimeError("pyserial is not installed")

    serial_key = (port, baud_rate)
    ser = st.session_state.get("serial")
    if ser is not None and st.session_state.get("serial_key") == serial_key and ser.is_open:
        return ser

    close_serial()
    ser = serial.Serial(port=port, baudrate=baud_rate, timeout=0.05)
    ser.reset_input_buffer()
    st.session_state["serial"] = ser
    st.session_state["serial_key"] = serial_key
    return ser


def read_serial_lines(ser, max_lines: int, read_window_seconds: float) -> list[str]:
    lines: list[str] = []
    deadline = time.monotonic() + read_window_seconds

    while len(lines) < max_lines and time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            break
        lines.append(raw.decode("utf-8", errors="replace").strip())

    return lines


def draw_plot(frame: pd.DataFrame, y_column: str, masses: list[int], max_points: int) -> None:
    if frame.empty:
        st.info("No RGA records found.")
        return

    plot_frame = frame[frame["mass"].isin(masses)] if masses else frame
    if max_points > 0:
        plot_frame = plot_frame.tail(max_points)

    chart_data = plot_frame.pivot_table(
        index="timestamp",
        columns="mass",
        values=y_column,
        aggfunc="last",
    ).sort_index()
    chart_data.columns = [f"m/z {mass}" for mass in chart_data.columns]
    st.line_chart(chart_data, height=520)


def main() -> None:
    st.set_page_config(page_title="EGCF RGA Stream", layout="wide")
    st.title("EGCF RGA Stream")

    if "running" not in st.session_state:
        st.session_state["running"] = False
    if "serial" not in st.session_state:
        st.session_state["serial"] = None
        st.session_state["serial_key"] = None

    with st.sidebar:
        st.header("Serial")
        ports = available_serial_ports()
        port_options = ports + ["Manual"]
        selected_port = st.selectbox("Port", port_options, index=0 if ports else len(port_options) - 1)
        port = (
            st.text_input("Manual port", value="/dev/cu.usbmodem")
            if selected_port == "Manual"
            else selected_port
        )
        baud_rate = st.number_input("Baud", min_value=300, max_value=1_000_000, value=DEFAULT_BAUD_RATE)

        st.header("Data")
        data_file = Path(st.text_input("File", value=DEFAULT_DATA_FILE)).expanduser()
        sensitivity = st.number_input(
            "Sensitivity (mA/Torr)",
            min_value=0.000001,
            max_value=10.0,
            value=DEFAULT_PARTIAL_PRESSURE_SENSITIVITY_MA_PER_TORR,
            format="%.6f",
        )
        detector_gain = st.number_input(
            "Detector gain",
            min_value=0.000001,
            max_value=1_000_000.0,
            value=1.0,
            format="%.6f",
        )

        st.header("Plot")
        y_column = st.selectbox(
            "Y axis",
            options=["pressure_torr", "current_a", "counts"],
            format_func=lambda value: {
                "pressure_torr": "Pressure (Torr)",
                "current_a": "Current (A)",
                "counts": "Counts",
            }[value],
        )
        max_points = st.number_input("Max points", min_value=0, max_value=1_000_000, value=10_000)
        refresh_seconds = st.number_input("Refresh (s)", min_value=0.1, max_value=10.0, value=0.5)
        max_lines_per_refresh = st.number_input("Max lines/read", min_value=1, max_value=10_000, value=500)

        start_col, stop_col = st.columns(2)
        start_requested = start_col.button("Start", width="stretch")
        stop_requested = stop_col.button("Stop", width="stretch")

    if start_requested:
        st.session_state["running"] = True
    if stop_requested:
        st.session_state["running"] = False
        close_serial()

    status_placeholder = st.empty()

    if st.session_state["running"]:
        try:
            ser = ensure_serial(str(port), int(baud_rate))
            lines = read_serial_lines(
                ser,
                max_lines=int(max_lines_per_refresh),
                read_window_seconds=min(float(refresh_seconds), 1.0),
            )
            new_records = [record for line in lines if (record := parse_rga_line(line)) is not None]
            appended = append_rga_lines(data_file, new_records)
            status_placeholder.success(
                f"Reading {port} at {int(baud_rate)} baud. Appended {appended} RGA record(s)."
            )
        except Exception as exc:
            st.session_state["running"] = False
            close_serial()
            status_placeholder.error(f"Serial read stopped: {exc}")
    else:
        status_placeholder.info("Serial reader stopped.")

    records = load_records(data_file)
    frame = records_to_frame(records, sensitivity_ma_per_torr=float(sensitivity), detector_gain=float(detector_gain))

    metrics = st.columns(4)
    metrics[0].metric("Records", f"{len(frame):,}")
    metrics[1].metric("Masses", f"{frame['mass'].nunique() if not frame.empty else 0:,}")
    metrics[2].metric("Latest UTC", "n/a" if frame.empty else frame["timestamp"].max().isoformat())
    metrics[3].metric("File", str(data_file))

    masses = sorted(frame["mass"].dropna().astype(int).unique().tolist()) if not frame.empty else []
    selected_masses = st.multiselect("Masses", options=masses, default=masses)
    draw_plot(frame, y_column=y_column, masses=selected_masses, max_points=int(max_points))

    with st.expander("Latest records", expanded=False):
        st.dataframe(frame.tail(100), width="stretch")

    if st.session_state["running"]:
        time.sleep(float(refresh_seconds))
        st.rerun()


if __name__ == "__main__":
    main()
