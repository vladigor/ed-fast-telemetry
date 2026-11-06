# ED Fast Telemetry

A standalone Windows app that uses Elite Dangerous screenshots to publish faster planetary positions into `Status.json`.

In brief:
- Taking a screenshot results in a Journal entry that includes the players position.
- The app sends F10 presses to the game to trigger screenshots (eg. at a rate of 10 per second).
- The app watches the game's Journal file and copies screenshot event position data to the Status file.
- The app deletes the screenshot file.


## Use

1. Open Elite Dangerous and reach a planet surface.
2. Run `ED-fast-telemetry.exe`. Read the on-screen warnings.
3. Set **Updates per second** (whole numbers from 1 to 50, default 10). The setting is saved immediately.
4. Click **Start**, then return focus to the game. F10 presses only happen while `EliteDangerous64.exe` is foreground and Shift, Ctrl, Alt and Windows are not held.
5. Click **Stop** to stop F10 and status publishing. Screenshot cleanup continues for 15 seconds to catch delayed writes. Closing does a final cleanup pass and releases F10.

The status line shows F10 presses sent, actual presses and screenshots per second, key-send duration, screenshots read/deleted, status updates/skips/failures, pending deletes and errors. Actual updates can be slower than requested because of timers, frame rate, modifier keys or game-side limits.

## Screenshot management and safety

Every ordinary F10 screenshot taken while running, including manual ones, is deleted. Files named by live Screenshot journal events are deleted promptly on a background thread. Every two seconds the app also sweeps `Screenshot_<number>.bmp` files modified since starting (with a two-second timestamp tolerance), ignoring files modified within the last second. Older screenshots, hi-res `HighResScreenShot_*` files and other files are kept.

Locked files are retried for up to 30 seconds. Disable software that watches, converts or uploads screenshots before starting. Other applications with F10 hotkeys may react too. Pressing a modifier during a brief F10 hold can still cause a modified screenshot.

Windows Pictures and Saved Games locations are read from the registry, including relocated folders. Defaults:

- Journal and status: `Saved Games\Frontier Developments\Elite Dangerous`
- Screenshots: `Pictures\Frontier Developments\Elite Dangerous`

For custom locations, launch with `--journal-directory "C:\YourJournalFolder"` and/or `--screenshot-directory "C:\YourScreenshotFolder"`. The selected paths are displayed in the window. Missing/unreadable folders are reported in the status line.

### Optional: use a RAM disk for screenshots

If you experience game jitter while this app is running, you could try placing the game's screenshot folder on a RAM disk. This may reduce storage-related delays, but it is not guaranteed to fix jitter. RAM-disk contents are temporary and can be lost when the computer shuts down, so save any screenshots you want to keep elsewhere.

One Windows RAM-disk utility is [aim-toolkit](https://sourceforge.net/projects/aim-toolkit/). Install and configure a RAM disk with a stable drive letter (for example, `R:`) and enough space for screenshots; `32 MB` is only an example and may not be sufficient for every setup. Create a folder on it, such as `R:\EDScreenshots`.

Before changing the folder, close Elite Dangerous and this app, and keep a backup of your existing screenshots. Rename the game's existing screenshot folder (for example, `Elite Dangerous` to `Elite Dangerous - backup`), then open Command Prompt as Administrator and create a directory link from the game's usual screenshot path to the RAM disk:

```cmd
mklink /D "%USERPROFILE%\Pictures\Frontier Developments\Elite Dangerous" "R:\EDScreenshots"
```

If your Pictures folder is redirected (for example, into OneDrive) or you use a custom screenshot location, replace the first path with the actual screenshot-folder path. Start the RAM disk before the game so the linked destination exists. Copy any screenshots you want to keep off the RAM disk before shutdown.

## Publishing positions

The app watches the latest `Journal.*.log`, with fallback polling. Start reads recent history for system context but never publishes or deletes historical screenshot events. Partial journal lines are deferred until complete; new journals and truncation are handled.

For each read, the latest eligible planetary Screenshot event supplies `timestamp`, `Latitude`, `Longitude`, and any supplied `Heading` and `Altitude`. Missing altitude/heading and every other status field are preserved. The event must be at least as new as the status timestamp and match its body and the current journal system; the status Has Lat/Long flag must be set. Events invalidated by a subsequent body departure/jump are not published.

Status updates replace the file atomically. A reader watching only the original file handle may need to reattach its watcher after replacement. Detected concurrent game writes are skipped, but **Elite and this app do not share a lock protocol, so a small write-conflict risk remains**. Whole-second timestamps do not increase in value for every high-frequency update; apps should watch file changes and coordinates.

## Develop, test and build

Everything needed lives in this directory; it can be moved into a separate repository.
On Windows, from this directory:

```powershell
py -3.13 -m venv venv
.\venv\Scripts\python.exe -m pip install -e ".[dev]"
.\venv\Scripts\python.exe launcher.py
.\venv\Scripts\python.exe -m pytest -q
.\build.ps1
```

The build produces a single windowed executable under `dist`, using PyInstaller. It uses only this folder's `venv`, never a parent environment. `.\build.ps1 -Name "CustomName"` overrides the executable name.

Linux can run the offscreen tests (`QT_QPA_PLATFORM=offscreen python3 -m pytest -q`), but actual Win32 input and the Windows executable must be tested on Windows.

## Versions

Versions use `major.minor.patch`. This first development iteration is `0.1.0.dev0`, displayed as `0.1.0-dev`; a release becomes `0.1.0`. Update both [pyproject.toml](pyproject.toml) and [__init__.py](src/ed_fast_telemetry/__init__.py) together. The executable name is derived automatically from the runtime version.

This unofficial community app is not affiliated with or endorsed by Frontier Developments, Elite Dangerous or EDCoPilot. MIT licensed; retained attribution for reused code is in [LICENSE](LICENSE).
