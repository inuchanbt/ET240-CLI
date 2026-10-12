# ET240 CLI

English | [日本語](README.ja.md)

A Python command-line interface for the POWER-Z ET240 USB Power Delivery analyzer, with the main commands and CSV format of [CY4500 CLI](https://github.com/inuchanbt/cy4500-cli).

The ET240 is controlled directly through Windows HID and WinUSB. **No Sniffer.exe, SnifferCmd.exe, vendor DLL, or third-party Python package is needed at runtime.**

This initial version supports passive PD capture, voltage/current recording, native USB replay, Analyzer Utility export, and offline PPS/AVS transition analysis. Hardware access was verified on an SN3005; live PD negotiations, EPR operation, and measurement accuracy under load still need validation. See [Validation](VALIDATION.md) for the evidence and remaining limits.

## Requirements

- Windows 10 or 11 and Python 3.10 or later.
- An ET240 with its HID and WinUSB interfaces available. The verified device was SN3005, firmware `A1 05 23 11 30 E2`. Other editions are currently rejected.
- Close Sniffer and other capture applications before connecting from this CLI.

The CLI uses the existing Windows HID/WinUSB drivers; no driver replacement is required. Keep all Python files from this repository together. `requirements.txt` documents the Python requirement; there are no packages to install with pip.

## Quick start

Clone this repository or download and extract its ZIP, then run the commands in PowerShell from the repository folder:

```powershell
git clone https://github.com/inuchanbt/ET240-CLI.git
cd ET240-CLI
python .\et240_cli.py usb-info
python .\et240_cli.py version
python .\et240_cli.py live-status --count 10
python .\et240_cli.py capture
```

`capture` runs until Ctrl+C, then saves and closes the session. By default it records PD, waveforms, and a `.ccgx3` session. Valid GoodCRC messages are hidden only in the console; they remain in the saved data.

```powershell
# Capture for 10 seconds with an explicit output prefix
python .\et240_cli.py capture --seconds 10 --out-prefix .\captures\et240_test

# Omit the ccgx3 export
python .\et240_cli.py capture --seconds 10 --no-ccgx3

# Omit waveform output; telemetry is still received for timing and voltage
python .\et240_cli.py capture --seconds 10 --no-scope

# Also save native telemetry USB transfers separately
python .\et240_cli.py capture --seconds 10 --scope-raw

# Analyze PPS/AVS transitions after capture
python .\et240_cli.py capture --seconds 30 --analyze-transitions

# Save waveforms to wave.csv; companion PD/session files use wave_session.*
python .\et240_cli.py scope --seconds 5 --csv .\captures\wave.csv
```

Existing output files cause an error. Add `--force` only when you want to overwrite them. When multiple devices are connected, pass `--device USB_SERIAL` to `version`, `capture`, `scope`, or `live-status`. Use `usb-info` to list the devices, and `python .\et240_cli.py capture --help` for all capture options.

## Replay and offline analysis

These commands do not require an attached ET240:

```powershell
# Re-decode native ET240 USB transfers, including waveforms
python .\et240_cli.py export-gui --xfers .\captures\et240_test.xfers.bin --out-prefix .\captures\redecoded

# Convert derived CY4500-compatible records to CSV/ccgx3 (PD only)
python .\et240_cli.py export-gui --records .\captures\et240_test.records.bin --out-prefix .\captures\converted

# Import a vendor Sniffer .gz trace (PD only; waveforms are not imported)
python .\et240_cli.py export-gui --trace .\traces\01_pd_bmc.gz --out-prefix .\captures\example01

# Analyze synchronized PD and waveform CSV files
python .\et240_cli.py analyze-sync --pd-csv .\captures\et240_test.csv --scope-csv .\captures\et240_test.scope.csv --out-prefix .\captures\analysis
```

Vendor example traces are optional local inputs and are not included in this repository.

## CY4500 CLI compatibility

| Command / option | Support |
|---|---|
| `usb-info`, `version` | Read ET240 information directly |
| `capture --seconds / --until-ctrl-c / --out-prefix` | Supported; runs until Ctrl+C by default |
| `--scope / --no-scope / --scope-raw` | Supported |
| `--ccgx3 / --no-ccgx3 / --gui-csv` | Supported; ccgx3 is converted to CY4500 interchange format |
| `--hide-goodcrc / --show-goodcrc / --quiet` | Supported |
| `live-status`, `volt-amp` | Supported through an ET240 capture session |
| `scope --csv / --raw` | Supported |
| `analyze-sync`, `--analyze-transitions` | Reuse CY4500 PPS/AVS analysis |
| `export-gui --records` | Convert compatible records |
| `trigger*`, `termination*` | Unsupported; exit code 2 because no ET240 equivalent has been verified |
| CY4500EPR Python class API | Outside the compatibility scope; hardware access uses `et240_usb.ET240` |

Compatibility covers the main commands and data formats. Console text, JSON internals, and USB wire records differ. DP AUX and USB4 sideband decoding are outside this version's scope and are disabled during capture.

## Output files and units

The default prefix is `captures/et240_<timestamp>`.

| Suffix | Contents |
|---|---|
| `.csv` | PD CSV with the same 15 columns as CY4500 Utility |
| `.scope.csv` | Waveform CSV accepted by the CY4500 analysis, plus ET240 counters and CC measurements |
| `.xfers.bin` | **Original ET240 USB transfers**, each preceded by a little-endian uint32 length |
| `.scope.xfers.bin` | Native telemetry transfers when `--scope-raw` is enabled |
| `.records.bin` | **Derived 64-byte CY4500-compatible records**, not ET240 wire data |
| `.records.hex.txt` | Hexadecimal view of those derived records |
| `.records.jsonl` | PD payload, CRC, CC channel, raw timing, BMC intervals, and compatible decoded fields |
| `.ccgx3` | PD/waveform session converted to Analyzer Utility format |
| `.summary.txt`, `.metadata.json` | Counts, errors, synchronization, device information, units, and output paths |
| `.transitions.*`, `.transition_summary.*` | Optional PPS/AVS transition analysis |

PD CSV columns, in order:

```text
Sno,Ok,SOP,Message,Msg Id,Data Role,Power Role,Obj Count,Rev,Duration,Delta,Vbus(V),Data,Start Time,End Time
```

**Despite its name, the PD CSV `Vbus(V)` column contains integer millivolts**, matching the original CY4500 Utility format. Waveform columns `vbus_V` and `ibus_A` use volts and amperes.

PD and waveform timestamps share the ET240's 192 MHz device counter. Saved times are microseconds relative to the first synchronized telemetry sample; synchronization does not use host arrival times. Original counters are retained in JSONL and waveform CSV. PD voltage comes from the preceding telemetry sample; its CSV cell is empty if no preceding sample exists within one second.

Derived `.records.bin` timestamps are rounded to integer microseconds and stored in 32 bits. Its voltages, and the ccgx3 waveforms, are quantized to CY4500 ADC units. Use `.scope.csv` and `.records.jsonl` for the original ET240 numeric precision, and `.xfers.bin` for native replay. The ccgx3 file structure has been checked; display in Analyzer Utility has not yet been verified.

## Validation and limitations

- Verified device information, capture start/stop, and reopening on an attached SN3005.
- A three-second hardware capture saved 499 waveform samples; native replay reproduced those samples.
- All 228 PD packets across five vendor example traces matched payload bytes, CRC bytes/results, and SOP.
- Automated tests cover transfer boundaries, counter wrap, CRC failures, disconnect cleanup, overwrite protection, separate output destinations, and synthetic PPS transition analysis.
- The hardware tests had approximately zero VBUS and no fresh PD traffic. Live PD negotiation, EPR, and voltage/current accuracy under load remain unverified.
- Long BIST Carrier Mode 2 streams and extended packets exceeding the 64-byte interchange capacity are not fully compatible. Native USB transfers are preserved.

Run the tests with:

```powershell
python -m unittest discover -s .\tests -v
```

Tests do not require hardware. The optional vendor-trace comparison is skipped unless the vendor examples are present in the sibling folder `../PD_Sniffer v1.08.26.0809/trace_example`.

See [Protocol notes](PROTOCOL.md) for the implemented transport and [Validation](VALIDATION.md) for the recorded checks.

## License

[MIT](LICENSE). `ezpd_protocol.py`, `utility_export.py`, `pd_capture.py`, and the offline analysis reuse MIT-licensed code from [CY4500 CLI](https://github.com/inuchanbt/cy4500-cli). The original repository and vendor software are not runtime dependencies.
