#!/usr/bin/env python3
"""Extract DJ-Controller USB-MIDI packets from a USBPcap .pcap file."""

from __future__ import annotations

import argparse
import csv
import json
import struct
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable


PCAP_MICRO_MAGIC = {
    b"\xd4\xc3\xb2\xa1": ("<", False),
    b"\xa1\xb2\xc3\xd4": (">", False),
    b"\x4d\x3c\xb2\xa1": ("<", True),
    b"\xa1\xb2\x3c\x4d": (">", True),
}

USBPCAP_LINKTYPE = 249

CIN_LENGTHS = {
    0x2: 2,
    0x3: 3,
    0x4: 3,
    0x5: 1,
    0x6: 2,
    0x7: 3,
    0x8: 3,
    0x9: 3,
    0xA: 3,
    0xB: 3,
    0xC: 2,
    0xD: 2,
    0xE: 3,
    0xF: 1,
}

STATUS_NAMES = {
    0x80: "note_off",
    0x90: "note_on",
    0xA0: "poly_pressure",
    0xB0: "control_change",
    0xC0: "program_change",
    0xD0: "channel_pressure",
    0xE0: "pitch_bend",
}

# Subset from the official DJ-Controller MIDI message list. Deck A is channel 1,
# Deck B channel 2, Deck C channel 3, Deck D channel 4, effects A/C channel 5,
# effects B/D channel 6, and mixer controls channel 7.
NOTE_NAMES = {
    11: "PLAY/PAUSE",
    12: "CUE",
    62: "TIME/AUTO CUE",
    63: "TIME/AUTO CUE + SHIFT",
    66: "Rotary selector press",
    67: "Rotary selector press + SHIFT",
    68: "BACK",
    69: "BACK + SHIFT",
    71: "PLAY/PAUSE + SHIFT",
    72: "CUE + SHIFT",
}

CC_NAMES = {
    1: "Jog dial outer",
    2: "Jog dial top",
    4: "TEMPO slider",
    16: "AUTO BEAT LOOP control",
    64: "Rotary selector turn",
    65: "Rotary selector turn + SHIFT",
}

MIXER_CC_NAMES = {
    3: "TRIM channel A",
    4: "TRIM channel B",
    7: "EQ HI channel A",
    8: "EQ HI channel B",
    11: "EQ MID channel A",
    12: "EQ MID channel B",
    15: "EQ LOW channel A",
    16: "EQ LOW channel B",
    19: "Channel fader A",
    20: "Channel fader B",
    23: "Crossfader",
    24: "Master level",
    35: "TRIM channel C",
    36: "TRIM channel D",
    39: "EQ HI channel C",
    40: "EQ HI channel D",
    43: "EQ MID channel C",
    44: "EQ MID channel D",
    47: "EQ LOW channel C",
    48: "EQ LOW channel D",
    51: "Channel fader C",
    52: "Channel fader D",
}


@dataclass
class PcapRecord:
    packet_index: int
    ts_seconds: int
    ts_fraction: int
    timestamp_utc: str
    relative_seconds: float
    payload: bytes


@dataclass
class UsbPcapPacket:
    packet_index: int
    timestamp_utc: str
    relative_seconds: float
    header_len: int
    irp_id: int
    status: int
    function: int
    info: int
    bus: int
    device: int
    endpoint: int
    transfer: int
    data_length: int
    payload: bytes


def byte_hex(data: bytes) -> str:
    return " ".join(f"{value:02X}" for value in data)


def parse_int_auto(value: str) -> int:
    return int(value, 0)


def read_pcap_records(path: Path) -> tuple[dict[str, int | str | bool], Iterable[PcapRecord]]:
    handle = path.open("rb")
    magic = handle.read(4)
    if magic not in PCAP_MICRO_MAGIC:
        handle.close()
        raise ValueError(f"Unsupported pcap magic: {byte_hex(magic)}")

    endian, nanoseconds = PCAP_MICRO_MAGIC[magic]
    header_rest = handle.read(20)
    if len(header_rest) != 20:
        handle.close()
        raise ValueError("Truncated pcap global header")

    version_major, version_minor, thiszone, sigfigs, snaplen, network = struct.unpack(
        f"{endian}HHIIII", header_rest
    )
    metadata: dict[str, int | str | bool] = {
        "versionMajor": version_major,
        "versionMinor": version_minor,
        "thiszone": thiszone,
        "sigfigs": sigfigs,
        "snaplen": snaplen,
        "network": network,
        "endian": endian,
        "nanoseconds": nanoseconds,
    }

    def iterator() -> Iterable[PcapRecord]:
        with handle:
            first_ts: float | None = None
            packet_index = 0
            divisor = 1_000_000_000 if nanoseconds else 1_000_000
            while True:
                packet_header = handle.read(16)
                if not packet_header:
                    return
                if len(packet_header) != 16:
                    raise ValueError(f"Truncated pcap packet header at packet {packet_index}")
                ts_sec, ts_frac, included_len, original_len = struct.unpack(
                    f"{endian}IIII", packet_header
                )
                payload = handle.read(included_len)
                if len(payload) != included_len:
                    raise ValueError(f"Truncated pcap payload at packet {packet_index}")
                timestamp = ts_sec + (ts_frac / divisor)
                if first_ts is None:
                    first_ts = timestamp
                timestamp_utc = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                yield PcapRecord(
                    packet_index=packet_index,
                    ts_seconds=ts_sec,
                    ts_fraction=ts_frac,
                    timestamp_utc=timestamp_utc,
                    relative_seconds=timestamp - first_ts,
                    payload=payload,
                )
                packet_index += 1

    return metadata, iterator()


def parse_usbp_packet(record: PcapRecord) -> UsbPcapPacket | None:
    data = record.payload
    if len(data) < 27:
        return None
    header_len = struct.unpack_from("<H", data, 0)[0]
    if header_len < 27 or header_len > len(data):
        return None

    irp_id = struct.unpack_from("<Q", data, 2)[0]
    status = struct.unpack_from("<I", data, 10)[0]
    function = struct.unpack_from("<H", data, 14)[0]
    info = data[16]
    bus = struct.unpack_from("<H", data, 17)[0]
    device = struct.unpack_from("<H", data, 19)[0]
    endpoint = data[21]
    transfer = data[22]
    data_length = struct.unpack_from("<I", data, 23)[0]
    payload = data[header_len:]
    if data_length <= len(payload):
        payload = payload[:data_length]

    return UsbPcapPacket(
        packet_index=record.packet_index,
        timestamp_utc=record.timestamp_utc,
        relative_seconds=record.relative_seconds,
        header_len=header_len,
        irp_id=irp_id,
        status=status,
        function=function,
        info=info,
        bus=bus,
        device=device,
        endpoint=endpoint,
        transfer=transfer,
        data_length=data_length,
        payload=payload,
    )


def iter_usb_midi_events(payload: bytes) -> Iterable[tuple[int, bytes, int, int]]:
    for offset in range(0, len(payload) - 3, 4):
        packet = payload[offset : offset + 4]
        cable = (packet[0] >> 4) & 0x0F
        cin = packet[0] & 0x0F
        length = CIN_LENGTHS.get(cin, 0)
        if length == 0:
            continue
        yield offset, packet, cable, cin


def annotate_midi(midi_bytes: bytes) -> dict[str, str | int | None]:
    if not midi_bytes:
        return {
            "statusName": None,
            "channel": None,
            "controlName": None,
            "controlGroup": None,
            "action": None,
        }

    status = midi_bytes[0]
    status_class = status & 0xF0
    channel = (status & 0x0F) + 1 if 0x80 <= status <= 0xEF else None
    status_name = STATUS_NAMES.get(status_class, "system_or_unknown")
    data1 = midi_bytes[1] if len(midi_bytes) > 1 else None
    data2 = midi_bytes[2] if len(midi_bytes) > 2 else None

    control_name = None
    control_group = None
    action = None
    if channel is not None:
        if channel <= 4:
            control_group = f"Deck {chr(ord('A') + channel - 1)}"
            if status_class in (0x80, 0x90) and data1 is not None:
                control_name = NOTE_NAMES.get(data1)
                if status_class == 0x80 or data2 == 0:
                    action = "release"
                else:
                    action = "press"
            elif status_class == 0xB0 and data1 is not None:
                control_name = CC_NAMES.get(data1)
                action = "value"
        elif channel == 7:
            control_group = "Mixer"
            if status_class == 0xB0 and data1 is not None:
                control_name = MIXER_CC_NAMES.get(data1)
                action = "value"
        elif channel in (5, 6):
            control_group = "Effects"
            action = "value" if status_class == 0xB0 else None

    return {
        "statusName": status_name,
        "channel": channel,
        "controlName": control_name,
        "controlGroup": control_group,
        "action": action,
    }


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_report(args: argparse.Namespace) -> dict[str, object]:
    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = args.prefix
    midi_csv = output_dir / f"{prefix}-midi-{stamp}.csv"
    hid_csv = output_dir / f"{prefix}-hid-{stamp}.csv"
    summary_json = output_dir / f"{prefix}-summary-{stamp}.json"

    metadata, records = read_pcap_records(input_path)
    if metadata["network"] != USBPCAP_LINKTYPE:
        raise ValueError(f"Expected USBPcap linktype 249, got {metadata['network']}")

    endpoint_counts: Counter[str] = Counter()
    device_counts: Counter[str] = Counter()
    midi_rows: list[dict[str, object]] = []
    hid_rows: list[dict[str, object]] = []
    packet_count = 0
    parsed_usb_packets = 0
    first_relative = None
    last_relative = None

    for record in records:
        packet_count += 1
        first_relative = record.relative_seconds if first_relative is None else first_relative
        last_relative = record.relative_seconds
        packet = parse_usbp_packet(record)
        if packet is None:
            continue
        parsed_usb_packets += 1
        endpoint_key = f"bus={packet.bus} device={packet.device} endpoint=0x{packet.endpoint:02X}"
        endpoint_counts[endpoint_key] += 1
        device_counts[f"bus={packet.bus} device={packet.device}"] += 1

        if args.bus is not None and packet.bus != args.bus:
            continue
        if args.device is not None and packet.device != args.device:
            continue

        if packet.endpoint == args.midi_endpoint:
            for offset, usb_midi_packet, cable, cin in iter_usb_midi_events(packet.payload):
                length = CIN_LENGTHS[cin]
                midi_bytes = usb_midi_packet[1 : 1 + length]
                annotation = annotate_midi(midi_bytes)
                data1 = midi_bytes[1] if len(midi_bytes) > 1 else None
                data2 = midi_bytes[2] if len(midi_bytes) > 2 else None
                row: dict[str, object] = {
                    "packet_index": packet.packet_index,
                    "timestamp_utc": packet.timestamp_utc,
                    "relative_seconds": f"{packet.relative_seconds:.6f}",
                    "bus": packet.bus,
                    "device": packet.device,
                    "endpoint_hex": f"0x{packet.endpoint:02X}",
                    "payload_offset": offset,
                    "usb_midi_packet_hex": byte_hex(usb_midi_packet),
                    "cable": cable,
                    "cin_hex": f"0x{cin:X}",
                    "midi_hex": byte_hex(midi_bytes),
                    "status_hex": f"0x{midi_bytes[0]:02X}" if midi_bytes else "",
                    "data1_decimal": data1 if data1 is not None else "",
                    "data1_hex": f"0x{data1:02X}" if data1 is not None else "",
                    "data2_decimal": data2 if data2 is not None else "",
                    "data2_hex": f"0x{data2:02X}" if data2 is not None else "",
                    **annotation,
                }
                midi_rows.append(row)

        if args.include_hid and packet.endpoint == args.hid_endpoint and packet.payload:
            hid_rows.append(
                {
                    "packet_index": packet.packet_index,
                    "timestamp_utc": packet.timestamp_utc,
                    "relative_seconds": f"{packet.relative_seconds:.6f}",
                    "bus": packet.bus,
                    "device": packet.device,
                    "endpoint_hex": f"0x{packet.endpoint:02X}",
                    "payload_length": len(packet.payload),
                    "payload_hex": byte_hex(packet.payload),
                }
            )

    midi_fields = [
        "packet_index",
        "timestamp_utc",
        "relative_seconds",
        "bus",
        "device",
        "endpoint_hex",
        "payload_offset",
        "usb_midi_packet_hex",
        "cable",
        "cin_hex",
        "midi_hex",
        "status_hex",
        "statusName",
        "channel",
        "controlGroup",
        "controlName",
        "action",
        "data1_decimal",
        "data1_hex",
        "data2_decimal",
        "data2_hex",
    ]
    hid_fields = [
        "packet_index",
        "timestamp_utc",
        "relative_seconds",
        "bus",
        "device",
        "endpoint_hex",
        "payload_length",
        "payload_hex",
    ]
    write_csv(midi_csv, midi_rows, midi_fields)
    if args.include_hid:
        write_csv(hid_csv, hid_rows, hid_fields)

    known_midi_rows = [row for row in midi_rows if row.get("controlName")]
    summary = {
        "input": str(input_path),
        "pcap": metadata,
        "packetCount": packet_count,
        "parsedUsbPacketCount": parsed_usb_packets,
        "durationSeconds": None
        if first_relative is None or last_relative is None
        else round(last_relative - first_relative, 6),
        "filters": {
            "bus": args.bus,
            "device": args.device,
            "midiEndpoint": f"0x{args.midi_endpoint:02X}",
            "hidEndpoint": f"0x{args.hid_endpoint:02X}",
            "includeHid": bool(args.include_hid),
        },
        "endpointCounts": dict(endpoint_counts.most_common()),
        "deviceCounts": dict(device_counts.most_common()),
        "midiEventCount": len(midi_rows),
        "knownMidiEventCount": len(known_midi_rows),
        "knownControls": dict(Counter(str(row["controlName"]) for row in known_midi_rows).most_common()),
        "outputs": {
            "midiCsv": str(midi_csv),
            "hidCsv": str(hid_csv) if args.include_hid else None,
            "summaryJson": str(summary_json),
        },
        "note": "Reports are generated under reports/ and should stay out of Git.",
    }
    summary_json.parent.mkdir(parents=True, exist_ok=True)
    summary_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract DJ-Controller USB-MIDI event packets from a USBPcap .pcap capture."
    )
    parser.add_argument("--input", required=True, help="USBPcap .pcap file to read.")
    parser.add_argument("--output-dir", default="reports", help="Directory for CSV/JSON reports.")
    parser.add_argument("--bus", type=parse_int_auto, default=None, help="Optional USB bus filter.")
    parser.add_argument("--device", type=parse_int_auto, default=None, help="Optional USB device address filter.")
    parser.add_argument(
        "--midi-endpoint",
        type=parse_int_auto,
        default=0x85,
        help="USB endpoint containing USB-MIDI event packets. Default: 0x85.",
    )
    parser.add_argument(
        "--hid-endpoint",
        type=parse_int_auto,
        default=0x87,
        help="USB HID input endpoint for optional raw report export. Default: 0x87.",
    )
    parser.add_argument("--include-hid", action="store_true", help="Also export raw HID report rows.")
    parser.add_argument("--prefix", default="DJ-Controller-usbpcap", help="Output filename prefix.")
    args = parser.parse_args()

    summary = build_report(args)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
