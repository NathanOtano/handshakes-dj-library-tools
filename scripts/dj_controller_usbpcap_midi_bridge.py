#!/usr/bin/env python3
"""Forward DJ-Controller USBPcap USB-MIDI packets to a WinMM MIDI output port."""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import struct
import time
from collections import Counter
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable

from dj_controller_usbpcap_extract import (
    CIN_LENGTHS,
    PCAP_MICRO_MAGIC,
    PcapRecord,
    USBPCAP_LINKTYPE,
    byte_hex,
    iter_usb_midi_events,
    parse_int_auto,
    parse_usbp_packet,
    read_pcap_records,
)


class MidiOutCapsW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("wTechnology", wintypes.WORD),
        ("wVoices", wintypes.WORD),
        ("wNotes", wintypes.WORD),
        ("wChannelMask", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


class WinMmMidiOutput:
    def __init__(self, name: str):
        self.name = name
        self.handle = wintypes.HANDLE()
        self.winmm = ctypes.WinDLL("winmm.dll")
        self.winmm.midiOutGetNumDevs.restype = wintypes.UINT
        self.winmm.midiOutGetDevCapsW.argtypes = [
            ctypes.c_size_t,
            ctypes.POINTER(MidiOutCapsW),
            wintypes.UINT,
        ]
        self.winmm.midiOutOpen.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_size_t,
            wintypes.DWORD,
        ]
        self.winmm.midiOutShortMsg.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.winmm.midiOutClose.argtypes = [wintypes.HANDLE]

    def list_outputs(self) -> list[dict[str, object]]:
        outputs: list[dict[str, object]] = []
        count = self.winmm.midiOutGetNumDevs()
        size = ctypes.sizeof(MidiOutCapsW)
        for index in range(count):
            caps = MidiOutCapsW()
            result = self.winmm.midiOutGetDevCapsW(index, ctypes.byref(caps), size)
            outputs.append({"id": index, "name": caps.szPname, "capsResult": result})
        return outputs

    def open(self) -> None:
        outputs = self.list_outputs()
        matches = [item for item in outputs if str(item["name"]).lower() == self.name.lower()]
        if not matches:
            available = ", ".join(str(item["name"]) for item in outputs)
            raise RuntimeError(f"MIDI output '{self.name}' not found. Available outputs: {available}")
        output_id = int(matches[0]["id"])
        result = self.winmm.midiOutOpen(ctypes.byref(self.handle), output_id, 0, 0, 0)
        if result != 0:
            raise RuntimeError(f"midiOutOpen failed for '{self.name}' with code {result}")

    def close(self) -> None:
        if self.handle:
            self.winmm.midiOutClose(self.handle)
            self.handle = wintypes.HANDLE()

    def send_short(self, midi_bytes: bytes) -> None:
        if len(midi_bytes) < 1 or len(midi_bytes) > 3:
            return
        padded = midi_bytes + b"\x00" * (3 - len(midi_bytes))
        raw = padded[0] | (padded[1] << 8) | (padded[2] << 16)
        result = self.winmm.midiOutShortMsg(self.handle, raw)
        if result != 0:
            raise RuntimeError(f"midiOutShortMsg failed with code {result}")

    def __enter__(self) -> "WinMmMidiOutput":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def read_exact_or_wait(handle: BinaryIO, size: int, follow: bool, poll_seconds: float) -> bytes:
    data = handle.read(size)
    while follow and len(data) < size:
        time.sleep(poll_seconds)
        more = handle.read(size - len(data))
        if more:
            data += more
        else:
            position = handle.tell()
            handle.seek(0, 2)
            end = handle.tell()
            handle.seek(position)
            if end < position:
                return data
    return data


def follow_pcap_records(path: Path, poll_seconds: float) -> Iterable[tuple[dict[str, object], object]]:
    while not path.exists() or path.stat().st_size < 24:
        time.sleep(poll_seconds)

    handle = path.open("rb")
    magic = handle.read(4)
    from dj_controller_usbpcap_extract import PCAP_MICRO_MAGIC, PcapRecord

    if magic not in PCAP_MICRO_MAGIC:
        handle.close()
        raise ValueError(f"Unsupported pcap magic: {byte_hex(magic)}")
    endian, nanoseconds = PCAP_MICRO_MAGIC[magic]
    header_rest = read_exact_or_wait(handle, 20, True, poll_seconds)
    if len(header_rest) != 20:
        handle.close()
        raise ValueError("Truncated pcap global header")
    version_major, version_minor, thiszone, sigfigs, snaplen, network = struct.unpack(
        f"{endian}HHIIII", header_rest
    )
    metadata: dict[str, object] = {
        "versionMajor": version_major,
        "versionMinor": version_minor,
        "thiszone": thiszone,
        "sigfigs": sigfigs,
        "snaplen": snaplen,
        "network": network,
        "endian": endian,
        "nanoseconds": nanoseconds,
    }
    if network != USBPCAP_LINKTYPE:
        handle.close()
        raise ValueError(f"Expected USBPcap linktype 249, got {network}")

    yield metadata, None

    packet_index = 0
    first_ts = None
    divisor = 1_000_000_000 if nanoseconds else 1_000_000
    while True:
        packet_header = read_exact_or_wait(handle, 16, True, poll_seconds)
        if len(packet_header) == 0:
            time.sleep(poll_seconds)
            continue
        if len(packet_header) != 16:
            time.sleep(poll_seconds)
            continue
        ts_sec, ts_frac, included_len, _original_len = struct.unpack(f"{endian}IIII", packet_header)
        payload = read_exact_or_wait(handle, included_len, True, poll_seconds)
        if len(payload) != included_len:
            continue
        timestamp = ts_sec + (ts_frac / divisor)
        if first_ts is None:
            first_ts = timestamp
        yield metadata, PcapRecord(
            packet_index=packet_index,
            ts_seconds=ts_sec,
            ts_fraction=ts_frac,
            timestamp_utc=datetime.fromtimestamp(timestamp).isoformat(),
            relative_seconds=timestamp - first_ts,
            payload=payload,
        )
        packet_index += 1


def iter_stream_pcap_records(handle: BinaryIO) -> Iterable[tuple[dict[str, object], object]]:
    magic = handle.read(4)
    if magic not in PCAP_MICRO_MAGIC:
        raise ValueError(f"Unsupported pcap magic: {byte_hex(magic)}")
    endian, nanoseconds = PCAP_MICRO_MAGIC[magic]
    header_rest = handle.read(20)
    if len(header_rest) != 20:
        raise ValueError("Truncated pcap global header")
    version_major, version_minor, thiszone, sigfigs, snaplen, network = struct.unpack(
        f"{endian}HHIIII", header_rest
    )
    metadata: dict[str, object] = {
        "versionMajor": version_major,
        "versionMinor": version_minor,
        "thiszone": thiszone,
        "sigfigs": sigfigs,
        "snaplen": snaplen,
        "network": network,
        "endian": endian,
        "nanoseconds": nanoseconds,
    }
    if network != USBPCAP_LINKTYPE:
        raise ValueError(f"Expected USBPcap linktype 249, got {network}")

    yield metadata, None

    packet_index = 0
    first_ts = None
    divisor = 1_000_000_000 if nanoseconds else 1_000_000
    while True:
        packet_header = handle.read(16)
        if not packet_header:
            return
        if len(packet_header) != 16:
            return
        ts_sec, ts_frac, included_len, _original_len = struct.unpack(f"{endian}IIII", packet_header)
        payload = handle.read(included_len)
        if len(payload) != included_len:
            return
        timestamp = ts_sec + (ts_frac / divisor)
        if first_ts is None:
            first_ts = timestamp
        yield metadata, PcapRecord(
            packet_index=packet_index,
            ts_seconds=ts_sec,
            ts_fraction=ts_frac,
            timestamp_utc=datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            relative_seconds=timestamp - first_ts,
            payload=payload,
        )
        packet_index += 1


def iter_usbpcap_process_records(args: argparse.Namespace):
    command = [
        args.usbpcap_cmd,
        "-d",
        args.capture_device,
        "-o",
        "-",
        "--inject-descriptors",
    ]
    if args.capture_devices:
        command += ["--devices", args.capture_devices]
    if args.capture_from_all_devices:
        command.append("--capture-from-all-devices")

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        if process.stdout is None:
            raise RuntimeError("USBPcap stdout pipe did not open")
        seen_metadata = False
        for metadata, record in iter_stream_pcap_records(process.stdout):
            if not seen_metadata:
                seen_metadata = True
                continue
            yield record
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


def iter_records(path: Path, follow: bool, poll_seconds: float):
    if follow:
        seen_metadata = False
        for metadata, record in follow_pcap_records(path, poll_seconds):
            if not seen_metadata:
                seen_metadata = True
                continue
            yield record
    else:
        metadata, records = read_pcap_records(path)
        if metadata["network"] != USBPCAP_LINKTYPE:
            raise ValueError(f"Expected USBPcap linktype 249, got {metadata['network']}")
        yield from records


def run_bridge(args: argparse.Namespace) -> dict[str, object]:
    input_path = Path(args.input).resolve() if args.input else None
    start_time = time.monotonic()
    sent_count = 0
    packet_count = 0
    skipped_count = 0
    endpoint_counts: Counter[str] = Counter()
    device_counts: Counter[str] = Counter()
    first_midi_hex = None
    last_midi_hex = None
    last_relative = None
    last_sent_monotonic = start_time

    record_iter = (
        iter_usbpcap_process_records(args)
        if args.live_usbpcap
        else iter_records(input_path, args.follow, args.poll_seconds)
    )

    try:
        with WinMmMidiOutput(args.output_name) as midi_out:
            for record in record_iter:
                if record is None:
                    continue
                packet_count += 1
                if args.duration_seconds and time.monotonic() - start_time >= args.duration_seconds:
                    break
                packet = parse_usbp_packet(record)
                if packet is None:
                    continue
                endpoint_counts[f"bus={packet.bus} device={packet.device} endpoint=0x{packet.endpoint:02X}"] += 1
                device_counts[f"bus={packet.bus} device={packet.device}"] += 1
                if args.bus is not None and packet.bus != args.bus:
                    continue
                if args.device is not None and packet.device != args.device:
                    continue
                if packet.endpoint != args.midi_endpoint:
                    continue

                for _offset, usb_midi_packet, _cable, cin in iter_usb_midi_events(packet.payload):
                    length = CIN_LENGTHS.get(cin, 0)
                    midi_bytes = usb_midi_packet[1 : 1 + length]
                    if len(midi_bytes) not in (1, 2, 3):
                        skipped_count += 1
                        continue

                    if args.replay_timing and last_relative is not None:
                        delta = max(0.0, packet.relative_seconds - last_relative)
                        if delta > 0:
                            time.sleep(min(delta, args.max_replay_sleep))

                    midi_out.send_short(midi_bytes)
                    sent_count += 1
                    first_midi_hex = first_midi_hex or byte_hex(midi_bytes)
                    last_midi_hex = byte_hex(midi_bytes)
                    last_relative = packet.relative_seconds
                    last_sent_monotonic = time.monotonic()

                    if args.max_events and sent_count >= args.max_events:
                        return {
                            "input": str(input_path) if input_path else "USBPcap live stdout",
                            "outputName": args.output_name,
                            "sentCount": sent_count,
                            "skippedCount": skipped_count,
                            "packetCount": packet_count,
                            "topEndpoints": dict(endpoint_counts.most_common(12)),
                            "topDevices": dict(device_counts.most_common(8)),
                            "firstMidiHex": first_midi_hex,
                            "lastMidiHex": last_midi_hex,
                            "stoppedBy": "maxEvents",
                        }

                if args.follow and args.idle_timeout_seconds:
                    if time.monotonic() - last_sent_monotonic >= args.idle_timeout_seconds:
                        break
    finally:
        close = getattr(record_iter, "close", None)
        if close:
            close()

    return {
        "input": str(input_path) if input_path else "USBPcap live stdout",
        "outputName": args.output_name,
        "sentCount": sent_count,
        "skippedCount": skipped_count,
        "packetCount": packet_count,
        "topEndpoints": dict(endpoint_counts.most_common(12)),
        "topDevices": dict(device_counts.most_common(8)),
        "firstMidiHex": first_midi_hex,
        "lastMidiHex": last_midi_hex,
        "stoppedBy": "durationOrEof",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Forward DJ-Controller USB-MIDI packets from USBPcap to a WinMM MIDI output."
    )
    parser.add_argument("--input", help="USBPcap .pcap file to read or follow.")
    parser.add_argument("--output-name", default="DJ-Controller Bridge (B)", help="WinMM MIDI output name.")
    parser.add_argument("--bus", type=parse_int_auto, default=None, help="Optional USB bus filter.")
    parser.add_argument("--device", type=parse_int_auto, default=None, help="Optional USB device address filter.")
    parser.add_argument("--midi-endpoint", type=parse_int_auto, default=0x85, help="USB-MIDI endpoint.")
    parser.add_argument("--live-usbpcap", action="store_true", help="Launch USBPcapCMD and read pcap bytes from stdout.")
    parser.add_argument("--usbpcap-cmd", default=r"C:\Program Files\USBPcap\USBPcapCMD.exe", help="USBPcapCMD.exe path.")
    parser.add_argument("--capture-device", default=r"\\.\USBPcap3", help="USBPcap root hub device.")
    parser.add_argument("--capture-devices", default="", help="USB device address list for USBPcapCMD, for example 16 or 16,17.")
    parser.add_argument("--capture-from-all-devices", action="store_true", help="Capture all devices on the selected root hub.")
    parser.add_argument("--follow", action="store_true", help="Follow a growing pcap file.")
    parser.add_argument("--poll-seconds", type=float, default=0.02, help="Poll interval for follow mode.")
    parser.add_argument("--duration-seconds", type=float, default=0, help="Stop after this duration.")
    parser.add_argument("--idle-timeout-seconds", type=float, default=0, help="Stop after no MIDI events in follow mode.")
    parser.add_argument("--max-events", type=int, default=0, help="Stop after sending this many MIDI events.")
    parser.add_argument("--replay-timing", action="store_true", help="Preserve original timing in replay mode.")
    parser.add_argument("--max-replay-sleep", type=float, default=0.25, help="Cap replay sleeps.")
    parser.add_argument("--list-outputs", action="store_true", help="List MIDI output ports and exit.")
    args = parser.parse_args()

    if args.list_outputs:
        print(json.dumps(WinMmMidiOutput(args.output_name).list_outputs(), indent=2, ensure_ascii=False))
        return 0
    if not args.input and not args.live_usbpcap:
        raise SystemExit("--input is required unless --live-usbpcap is used")

    print(json.dumps(run_bridge(args), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
