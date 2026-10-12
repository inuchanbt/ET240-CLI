# ET240 direct transport notes

Evidence: local Sniffer 1.08.26.0809, DeviceAp / SnrEng / PdUtil methods, five supplied traces,
and an attached ET240 with hardware edition 5 (SN3005, firmware A1 05 23 11 30 E2).
These notes describe the implemented subset.

## USB interfaces

- VID:PID: `1E4E:720E`.
- HID: interface 0, Windows HID interface GUID `4d1e55b2-f16f-11cf-88cb-001111000030`.
- WinUSB: interface 1, interface GUID `fce22465-21b7-4c3c-90c8-f9b25a24f5ab`.
- Confirmed endpoints: `83` bulk IN (512-byte maximum), `04` bulk OUT, `85` interrupt IN.
- HID/WinUSB pairing uses the same composite USB parent instance, including its serial.
- No driver replacement, libusb, .NET runtime, vendor executable, or vendor DLL is used.

HID uses report ID 0 and device-reported report lengths. Byte 1 is command 02 for GET_INFO
or 10 for SET_CONFIG. GET_INFO fields: bytes 1..12 device ID, byte 17 edition (5 = SN3005),
bytes 19..24 firmware. Remaining response bytes are not interpreted or exported.

SN3005 is the vendor's hardware edition name, separate from the USB serial number.
There is no individual serial-number or firmware-version allowlist. Capture currently requires
edition 5 because its telemetry format is implemented; the older SN3003 format differs.
The optional `--device` argument selects a USB serial or interface path, rather than restricting
which individual ET240 units are supported.

SET_CONFIG has 48 body bytes starting at report offset 2. Defaults:
body[0]=03 (CC1+CC2 edges), body[1]=00 (AUX disabled), body[2]=00 (BMC priority),
body[8]=03 (firmware sample index), body[9]=00 (USB4 disabled), body[10]=80 (VCONN enabled).
Body[16:30] = `43 27 02 00 43 27 08 00 00 00 FB 69 10 00`; all remaining bytes zero.
The fields follow the vendor initialization sequence, including its INA register configuration.

Start: send SET_CONFIG over HID, open WinUSB, initialize, read bulk IN.
Stop: free WinUSB, close its file handle. There is no guessed USB stop opcode.
HID reads/writes use overlapped I/O with cancellation and completion before buffer release.

## Capture framing

Each transfer: `EE type length_lo length_hi payload`. Length excludes the 4-byte header.
Native file: repeated `uint32_le transfer_length`, followed by the exact transfer bytes.

| Type | Payload |
|---|---|
| 81 | uint32 LE edge records; low 24 bits counter, high 8 bits GPIO; CC1/CC2 are bits 0/1 |
| 82 | AUX edges (not enabled/decoded here) |
| 83 | USB4 sideband data (not enabled/decoded here) |
| 85 | One immediate 64-byte telemetry record; also anchors the edge-counter epoch |
| 86 | Repeated 64-byte historical/polled telemetry records |

Synchronize after four consecutive immediate frames; discard startup/stale frames for decoding,
but retain all native transfers. Counter frequency: 192 MHz. Absolute ticks = epoch*2^24+counter.
The edge stream increments epoch when its low counter decreases; immediate telemetry reanchors it.
This uses exact rational conversion rather than the GUI's rounded 100ns DateTime coefficients.

Telemetry block: offset 0 epoch uint32, offset 4 counter+GPIO uint32, offset 8 eight ADC uint16,
offset 24 VBUS voltage/power/current as three uint32, offsets 36/48 CC1/CC2 voltage/power/current
as three uint32 each, offset 60 trailing bytes. Voltage/current words contain shifted signed
20-bit values. VBUS mV = signed20(word>>4)*25/128; IBUS mA = abs(signed20(word>>4))/32.
CC current uses /64. VBUS power W = raw_power*0.1/1000.
CC voltage V = ADC*0.0008058608/0.5454, matching the supplied board-divider configuration.
The ADC values retain their precision; GUI integer truncation/rounding is not reproduced.

If a type-86 payload contains trailing bytes beyond its complete 64-byte records, they are
counted and retained natively, and omitted from decoded telemetry, matching the vendor decoder.
They are never silently treated as complete samples. GPIO bit 7 on edges is counted as overflow.

## PD decoding and interchange

Edge half-bit classification follows the GUI's 0.9..2.6us / 2.6..6us timing windows, with integer
192 MHz thresholds. Decode BMC pairs, preamble, ordered set, 4b5b, PD header/data, CRC32 and EOP.
State spans arbitrary USB transfer boundaries. Both CC channels are independent.
CRC-invalid and malformed packet runs remain in JSONL/CSV; GoodCRC console filtering does not
discard records. The native input is always available for later decoder improvements.

`.records.bin` is explicitly DERIVED CY4500 interchange, with a 64-byte record layout.
It must not be described as captured ET240 hardware bytes. CSV keeps true ET240 voltage;
the interchange record and ccgx3 encode CY4500 ADC units and integer-us timestamps.
CC channel and original counters are preserved separately, as CY4500's record lacks a CC field.
Native replay is the lossless conversion path. `export-gui --records` cannot reconstruct wire CRC,
CC channel, fractional timestamps, or original ET240 precision from derived records alone.

Hardware trigger and CC termination commands are unimplemented: no verified ET240 equivalent.
No CY4500 opcode is sent to this device. The application configures only passive capture.
