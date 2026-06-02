# EGCF RGA Stream App

Streamlit app for reading RGA records from the EGCF firmware USB serial stream,
appending them to a log file, and plotting a live time series.

The firmware emits machine-readable RGA rows as:

```text
R:<utc_timestamp>,<mass>,<counts>
```

The app stores those raw `R:` rows so the selected log file remains compatible with
firmware SD-card logs. Existing raw firmware logs can be selected as the initial data
file; new serial records will be appended to the same file.

## Run

```sh
uv run streamlit run main.py
```

The firmware USB serial port defaults to `9600` baud.

## Pressure Conversion

The SRS RGA manual describes single-mass `MR` ion-current responses as 4-byte
two's-complement integers in units of `1e-16 A`, least-significant byte first. The
firmware converts those bytes to the decimal `<counts>` field.

The app computes:

```text
current_a = counts * 1e-16
pressure_torr = current_a / (partial_pressure_sensitivity_mA_per_torr * 1e-3 * detector_gain)
```

The default partial-pressure sensitivity is `0.1 mA/Torr`, which the manual lists as a
typical N2 value under default ionizer settings. Change it in the sidebar for calibrated
gas-specific pressure values.
