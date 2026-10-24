<#
MicMix installer: one command, and MicMix itself needs no admin. Press Win+R and paste:

    powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/benjaminhoffman-bit/micmix/main/install.ps1 | iex"

or double-click Install.cmd in the extracted download. Run it again to update: your settings and sounds stay.

It gets Python if this PC has none (winget, else python.org), puts MicMix in %LOCALAPPDATA%\MicMix, opens VB-Audio's
page for VB-CABLE and waits while you install it (MicMix doesn't bundle it; you get it from VB-Audio), then puts your
speakers and mic back as Windows' defaults, adds Start menu and desktop shortcuts, and opens MicMix.
Uninstall: the "Uninstall MicMix" shortcut, or Install.cmd -Uninstall.
#>
param(
    [switch]$Uninstall,
    [string]$Dest = "$env:LOCALAPPDATA\MicMix",
    [string]$Programs = [Environment]::GetFolderPath('Programs'),
    [string]$Desktop = [Environment]::GetFolderPath('Desktop'),
    [switch]$NoStart  # for tests: no launch, no questions, no pause at the end
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Windows PowerShell downloads crawl while drawing a progress bar
$Files = 'micmix.pyw', 'audio.py', 'test_micmix.py', 'README.md', 'LICENSE.txt', 'install.ps1', 'Install.cmd'
$Links = "$Programs\MicMix.lnk", "$Desktop\MicMix.lnk", "$Programs\Uninstall MicMix.lnk"
$Saved = "$Dest\defaults-before-vbcable.json"  # your default devices, kept until they are back

function Say([string]$text) { Write-Host "  $text" }
function Step([string]$text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }

function Find-Python {
    # a Python 3.10 or newer that has Tkinter; returns python.exe's full path
    $tries = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $tries += , @('py', '-3') }
    if (Get-Command python -ErrorAction SilentlyContinue) { $tries += , @('python') }
    Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" `
        -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { $tries += , @($_.FullName) }
    foreach ($try in $tries) {
        try {
            $exe = & $try[0] $try[1..9] -c 'import sys, tkinter; assert sys.version_info >= (3, 10); print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { return ([string]($exe | Select-Object -Last 1)).Trim() }
        } catch { }
    }
    return $null
}

function Install-Python {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Say 'Installing Python 3.12 with winget (a minute or two)...'
        try {
            winget install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements `
                --accept-source-agreements --disable-interactivity | Out-Null
        } catch { }
    }
    if (Find-Python) { return }
    Say 'Downloading Python 3.12 from python.org...'
    $setup = Join-Path $env:TEMP 'python-3.12.10-amd64.exe'
    Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile $setup -UseBasicParsing
    $sig = Get-AuthenticodeSignature $setup
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        throw 'That Python download is not signed by the Python Software Foundation, so it was not run.'
    }
    Start-Process $setup -Wait -ArgumentList '/quiet', 'InstallAllUsers=0', 'Include_launcher=1', 'InstallLauncherAllUsers=0',
        'Include_tcltk=1', 'Include_test=0', 'Shortcuts=0'
    Remove-Item $setup -ErrorAction SilentlyContinue
}

function Add-Helpers {
    # C# for what PowerShell can't do alone: stamp a shortcut with MicMix's AppUserModelID (so Windows shows MicMix
    # as its own app in the taskbar and a pin starts MicMix), and ask a running MicMix to quit.
    if ('MicMixAppId' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMicMixPropertyStore {
  [PreserveSig] int GetCount(out uint count);
  [PreserveSig] int GetAt(uint index, out MicMixPropertyKey key);
  [PreserveSig] int GetValue(ref MicMixPropertyKey key, out MicMixPropVariant value);
  [PreserveSig] int SetValue(ref MicMixPropertyKey key, ref MicMixPropVariant value);
  [PreserveSig] int Commit();
}
[StructLayout(LayoutKind.Sequential, Pack = 4)] public struct MicMixPropertyKey { public Guid Fmtid; public uint Pid; }
[StructLayout(LayoutKind.Explicit)] public struct MicMixPropVariant { [FieldOffset(0)] public ushort Vt; [FieldOffset(8)] public IntPtr Ptr; }
public static class MicMixAppId {
  [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
  static extern int SHGetPropertyStoreFromParsingName(string path, IntPtr bindCtx, int flags, ref Guid iid, out IMicMixPropertyStore store);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr FindWindowW(string cls, string title);
  [DllImport("user32.dll")] static extern bool PostMessageW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);
  public static void Set(string lnk, string id) {
    Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
    IMicMixPropertyStore store;
    Marshal.ThrowExceptionForHR(SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2 /* GPS_READWRITE */, ref iid, out store));
    MicMixPropertyKey key = new MicMixPropertyKey { Fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), Pid = 5 };  // System.AppUserModel.ID
    MicMixPropVariant value = new MicMixPropVariant { Vt = 31 /* VT_LPWSTR */, Ptr = Marshal.StringToCoTaskMemUni(id) };
    try { Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref value)); Marshal.ThrowExceptionForHR(store.Commit()); }
    finally { Marshal.FreeCoTaskMem(value.Ptr); Marshal.ReleaseComObject(store); }
  }
  public static bool Quit() {  // WM_CLOSE to MicMix's tray window: MicMix quits for real and saves its settings
    IntPtr tray = FindWindowW("MicMixTray", null);
    return tray != IntPtr.Zero && PostMessageW(tray, 0x10, IntPtr.Zero, IntPtr.Zero);
  }
}
'@
}

function Stop-MicMix {
    # the copy installed here only
    $running = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*$Dest\micmix.pyw*" } | ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
    if (-not $running) { return }
    try { [void][MicMixAppId]::Quit() } catch { }
    foreach ($p in $running) {
        if (-not $p.WaitForExit(5000)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }  # an old MicMix
    }
}

function Test-Cable {
    # its endpoints keep "(VB-Audio Virtual Cable)" while Windows may call the input end "CABLE Input" or "Speakers"
    [bool](Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue |
        Where-Object { $_.FriendlyName -like '*(VB-Audio Virtual Cable)' -and $_.Status -eq 'OK' })
}

function Restore-Defaults([string]$python) {
    & $python "$Dest\micmix.pyw" --restore-defaults $Saved
    if ($LASTEXITCODE -eq 0) {
        Remove-Item $Saved -ErrorAction SilentlyContinue
        Say 'Your own speakers and mic are Windows'' defaults again.'
    } else {
        Say 'Windows would not take all of your speakers and mic back as defaults (one may be unplugged).'
        Say 'Press Win+R, type mmsys.cpl, press Enter, and set them as default there.'
    }
}

function Add-Link([string]$path, [string]$target, [string]$arguments, [string]$about, [string]$appId = '') {
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
    $link.TargetPath, $link.Arguments, $link.WorkingDirectory = $target, $arguments, $Dest
    $link.IconLocation, $link.Description = "$Dest\assets\micmix.ico,0", $about
    $link.Save()
    if ($appId) { try { [MicMixAppId]::Set($path, $appId) } catch { } }  # only the taskbar grouping is lost
}

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    try { Add-Helpers } catch { }
    if ($Uninstall) {
        Step 'Removing MicMix'
        Set-Location $env:TEMP  # not inside the folder being removed (the uninstall shortcut starts there)
        [Environment]::CurrentDirectory = $env:TEMP
        Stop-MicMix
        Remove-Item $Links -ErrorAction SilentlyContinue
        $keep = if ($NoStart) { 'y' } else { Read-Host '  Keep your sounds and settings, in case you install it again? (Y/n)' }
        if ($keep -match '^\s*n') { Remove-Item $Dest -Recurse -Force -ErrorAction SilentlyContinue }
        else { Get-ChildItem $Dest -Exclude 'sounds', 'micmix.json' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force }
        Say 'MicMix is removed. If you pinned it to the taskbar, right-click it there and choose Unpin from taskbar.'
        Say 'VB-CABLE stays: to remove it too, run its setup again and choose Remove Driver.'
        Say 'In Discord, set Input Device back to your real mic.'
    } else {
        Step 'Python'
        $python = Find-Python
        if (-not $python) { Install-Python; $python = Find-Python }
        if (-not $python) { throw 'Python could not be installed. Get it from python.org, then run this again.' }
        Say "Using $python"
        $pythonw = Join-Path (Split-Path $python) 'pythonw.exe'

        Step 'MicMix'
        New-Item -ItemType Directory -Force $Dest, "$Dest\sounds" | Out-Null
        $here = if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'micmix.pyw'))) { (Resolve-Path $PSScriptRoot).Path } else { '' }
        $from = if ($here -and $here -ne (Resolve-Path $Dest).Path) { $here } else { '' }  # the installed copy updates
        if (-not $from) {
            Say 'Downloading the latest MicMix from GitHub...'
            $zip, $unzipped = (Join-Path $env:TEMP 'micmix-main.zip'), (Join-Path $env:TEMP 'micmix-download')
            Invoke-WebRequest 'https://github.com/benjaminhoffman-bit/micmix/archive/refs/heads/main.zip' -OutFile $zip -UseBasicParsing
            Remove-Item $unzipped -Recurse -Force -ErrorAction SilentlyContinue
            Expand-Archive $zip $unzipped -Force
            $from = Join-Path $unzipped 'micmix-main'
        }
        Stop-MicMix
        foreach ($file in $Files) { Copy-Item (Join-Path $from $file) $Dest -Force }
        Copy-Item (Join-Path $from 'assets') $Dest -Recurse -Force  # the SILVADES fonts (with their licenses) and icon
        Say "Installed in $Dest"

        Step 'VB-CABLE, the virtual mic'
        if (Test-Cable) {
            if (Test-Path $Saved) { Restore-Defaults $python }  # an earlier run stopped waiting before the cable came
            else { Say 'Already installed.' }
        } else {
            & $python "$Dest\micmix.pyw" --save-defaults $Saved
            Say 'VB-CABLE is a free driver from VB-Audio, and its own setup has to install it. Opening its page:'
            Say '  1. Download the driver pack (the big Download button) and extract the zip.'
            Say '  2. Right-click VBCABLE_Setup_x64.exe, choose Run as administrator, click Install Driver.'
            Say 'This window waits and carries on by itself once the cable appears.'
            Start-Process 'https://vb-audio.com/Cable/'
            $deadline = (Get-Date).AddMinutes(30)
            while (-not (Test-Cable) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 3 }
            if (Test-Cable) {
                Start-Sleep -Seconds 5  # let VB-CABLE's setup finish making itself the default, then undo that
                Restore-Defaults $python
                Say 'VB-Audio asks for a restart to finish; MicMix usually works before it.'
            } else {
                Say 'No VB-CABLE yet. Install it, then run this installer again: it puts your devices back then.'
            }
        }

        Step 'Shortcuts'
        New-Item -ItemType Directory -Force $Programs, $Desktop | Out-Null
        Add-Link $Links[0] $pythonw "`"$Dest\micmix.pyw`"" 'MicMix: your mic, your music and your sounds in one mic' 'SilvaDes.MicMix'
        Add-Link $Links[1] $pythonw "`"$Dest\micmix.pyw`"" 'MicMix: your mic, your music and your sounds in one mic' 'SilvaDes.MicMix'
        Add-Link $Links[2] "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" `
            "-NoProfile -ExecutionPolicy Bypass -File `"$Dest\install.ps1`" -Uninstall" 'Remove MicMix'
        Say 'MicMix is in the Start menu and on the desktop. To keep it on the taskbar, right-click its taskbar'
        Say 'button once it opens and choose Pin to taskbar.'

        Step 'Last step, in Discord: User Settings > Voice & Video'
        Say 'Input Device: CABLE Output (VB-Audio Virtual Cable)'
        Say 'Input Profile: Custom. Noise Suppression: None. Echo Cancellation and Automatic Gain Control: off.'
        Say 'Input Sensitivity: turn off "Automatically determine" and drag the slider all the way left.'
        if (-not $NoStart) { Start-Process $pythonw -ArgumentList "`"$Dest\micmix.pyw`"" -WorkingDirectory $Dest }
    }
} catch {
    Write-Host ''
    Write-Host "  Something went wrong: $($_.Exception.Message)" -ForegroundColor Red
}
if (-not $NoStart) { Write-Host ''; [void](Read-Host '  Press Enter to close this window') }
