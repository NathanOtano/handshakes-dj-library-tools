[CmdletBinding()]
param(
    [string] $DeviceNamePattern = 'DJ-Controller',
    [ValidateRange(1, 300)]
    [int] $Seconds = 20,
    [switch] $CloseRekordbox,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not ('WinMmMidiInputMonitor' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class WinMmMidiInputMonitor
{
    private const int CALLBACK_FUNCTION = 0x00030000;
    private const int MIM_DATA = 0x3C3;

    private delegate void MidiInProc(IntPtr hMidiIn, int wMsg, IntPtr dwInstance, IntPtr dwParam1, IntPtr dwParam2);
    private static MidiInProc CallbackDelegate = MidiCallback;
    private static IntPtr Handle = IntPtr.Zero;
    private static readonly object EventLock = new object();
    private static readonly List<MidiEvent> EventList = new List<MidiEvent>();

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Auto)]
    private struct MIDIINCAPS
    {
        public ushort wMid;
        public ushort wPid;
        public uint vDriverVersion;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
        public string szPname;
        public uint dwSupport;
    }

    public sealed class MidiDevice
    {
        public int Id { get; set; }
        public string Name { get; set; }
        public int CapsResult { get; set; }
    }

    public sealed class MidiEvent
    {
        public string Time { get; set; }
        public int Status { get; set; }
        public int Data1 { get; set; }
        public int Data2 { get; set; }
        public int Raw { get; set; }
        public string Hex { get; set; }
    }

    [DllImport("winmm.dll")]
    private static extern uint midiInGetNumDevs();

    [DllImport("winmm.dll", CharSet = CharSet.Auto)]
    private static extern int midiInGetDevCaps(UIntPtr uDeviceID, out MIDIINCAPS lpMidiInCaps, uint cbMidiInCaps);

    [DllImport("winmm.dll")]
    private static extern int midiInOpen(out IntPtr lphMidiIn, uint uDeviceID, MidiInProc dwCallback, IntPtr dwInstance, uint dwFlags);

    [DllImport("winmm.dll")]
    private static extern int midiInStart(IntPtr hMidiIn);

    [DllImport("winmm.dll")]
    private static extern int midiInStop(IntPtr hMidiIn);

    [DllImport("winmm.dll")]
    private static extern int midiInReset(IntPtr hMidiIn);

    [DllImport("winmm.dll")]
    private static extern int midiInClose(IntPtr hMidiIn);

    public static MidiDevice[] GetInputDevices()
    {
        uint count = midiInGetNumDevs();
        List<MidiDevice> devices = new List<MidiDevice>();
        uint size = (uint)Marshal.SizeOf(typeof(MIDIINCAPS));

        for (uint i = 0; i < count; i++)
        {
            MIDIINCAPS caps;
            int result = midiInGetDevCaps((UIntPtr)i, out caps, size);
            devices.Add(new MidiDevice {
                Id = (int)i,
                Name = result == 0 ? caps.szPname : "",
                CapsResult = result
            });
        }

        return devices.ToArray();
    }

    public static void ClearEvents()
    {
        lock (EventLock)
        {
            EventList.Clear();
        }
    }

    public static int Open(int deviceId)
    {
        ClearEvents();
        return midiInOpen(out Handle, (uint)deviceId, CallbackDelegate, IntPtr.Zero, CALLBACK_FUNCTION);
    }

    public static int Start()
    {
        return midiInStart(Handle);
    }

    public static int StopAndClose()
    {
        int result = 0;
        if (Handle != IntPtr.Zero)
        {
            midiInStop(Handle);
            midiInReset(Handle);
            result = midiInClose(Handle);
            Handle = IntPtr.Zero;
        }
        return result;
    }

    public static MidiEvent[] GetEvents()
    {
        lock (EventLock)
        {
            return EventList.ToArray();
        }
    }

    private static void MidiCallback(IntPtr hMidiIn, int wMsg, IntPtr dwInstance, IntPtr dwParam1, IntPtr dwParam2)
    {
        if (wMsg != MIM_DATA)
        {
            return;
        }

        int raw = unchecked((int)dwParam1.ToInt64());
        int status = raw & 0xFF;
        int data1 = (raw >> 8) & 0xFF;
        int data2 = (raw >> 16) & 0xFF;

        MidiEvent item = new MidiEvent {
            Time = DateTimeOffset.Now.ToString("o"),
            Status = status,
            Data1 = data1,
            Data2 = data2,
            Raw = raw,
            Hex = String.Format("{0:X2} {1:X2} {2:X2}", status, data1, data2)
        };

        lock (EventLock)
        {
            EventList.Add(item);
        }
    }
}
'@
}

if ($CloseRekordbox) {
    $rekordboxProcesses = @(Get-Process rekordbox -ErrorAction SilentlyContinue)
    foreach ($process in $rekordboxProcesses) {
        if ($process.MainWindowHandle -ne 0) {
            [void] $process.CloseMainWindow()
        }
    }
    Start-Sleep -Seconds 4
}

$devices = @([WinMmMidiInputMonitor]::GetInputDevices())
$device = $devices | Where-Object { $_.Name -match $DeviceNamePattern } | Select-Object -First 1

if ($null -eq $device) {
    throw "No MIDI input matched '$DeviceNamePattern'. Devices: $($devices.Name -join ', ')"
}

$rekordboxRunning = [bool](Get-Process rekordbox -ErrorAction SilentlyContinue)
$openResult = [WinMmMidiInputMonitor]::Open($device.Id)
if ($openResult -ne 0) {
    $result = [pscustomobject][ordered]@{
        deviceId = $device.Id
        deviceName = $device.Name
        seconds = $Seconds
        closeRekordbox = [bool]$CloseRekordbox
        rekordboxRunning = $rekordboxRunning
        openResult = $openResult
        startResult = $null
        closeResult = $null
        eventCount = 0
        events = @()
        hint = 'midiInOpen failed. Close rekordbox or any app using this MIDI input, then retry.'
    }

    if ($Json) {
        $result | ConvertTo-Json -Depth 6
        return
    }

    $result
    return
}

$startResult = [WinMmMidiInputMonitor]::Start()
if ($startResult -ne 0) {
    [void][WinMmMidiInputMonitor]::StopAndClose()
    throw "midiInStart failed with code $startResult"
}

Start-Sleep -Seconds $Seconds
$events = @([WinMmMidiInputMonitor]::GetEvents())
$closeResult = [WinMmMidiInputMonitor]::StopAndClose()

$result = [pscustomobject][ordered]@{
    deviceId = $device.Id
    deviceName = $device.Name
    seconds = $Seconds
    closeRekordbox = [bool]$CloseRekordbox
    rekordboxRunning = $rekordboxRunning
    openResult = $openResult
    startResult = $startResult
    closeResult = $closeResult
    eventCount = $events.Count
    events = $events
}

if ($Json) {
    $result | ConvertTo-Json -Depth 6
    return
}

$result
