# Validation record

Validated 2026-10-12 (Asia/Tokyo).

- `python -m unittest discover -s tests -v`: 16 tests passed.
- Five vendor example traces: 228 PD packets; payload bytes, CRC bytes/CRC result and SOP all matched.
- Synthetic edge streams crossed USB transfer boundaries at 1, 17, 37 and 127 words per transfer.
- Synthetic PPS source capabilities, request, ACCEPT, PS_RDY and voltage ramp produced a 10V transition analysis.
- CRC failures, strict framing, epoch wrap, hard reset, signed-current magnitude, output collisions,
  input preservation, independent scope/PD destinations and disconnect cleanup passed.
- Attached ET240: SN3005, firmware A1 05 23 11 30 E2, Windows HID + WinUSB.
- GET_INFO succeeded; confirmed bulk IN 0x83, bulk OUT 0x04, interrupt IN 0x85.
- 3-second capture (`captures/hardware_smoke.*`): 502 USB transfers, 499 telemetry samples, zero framing/decode errors.
- Native USB replay reproduced 499 telemetry samples.
- `live-status --count 3` succeeded and closed its session.
- Final `scope` test: 1 second, 167 transfers, 164 samples, separate PD and scope destinations.
- Final `capture --analyze-transitions`: 1 second, 168 transfers, 165 samples, saved files and four analysis outputs successfully.

The physical DUT path had approximately zero VBUS and no fresh PD packets during these hardware tests.
Live PD negotiations/EPR, loaded-voltage/current calibration, BIST and Analyzer Utility ccgx3 visual display
remain unverified. PD decoding was checked against the recorded vendor samples and synthetic streams.

Temporary analysis scripts, downloaded analysis tools/dependencies, and local decompilation files are
preserved in `research/`. None are runtime dependencies. The runnable CLI does not load proprietary code.
