# Python Nordic Legacy DFU Tool

A utility to perform **Legacy Device Firmware Updates (DFU)** on Nordic Semiconductor nRF51/nRF52 devices using Python.

This project has been split into a modular library, a Command Line Interface (CLI), and a Graphical User Interface (GUI). It is designed to replicate the logic of the official [Nordic Android DFU Library](https://github.com/NordicSemiconductor/Android-DFU-Library), specifically handling the **Buttonless Jump** and **Legacy DFU** protocols via [Bleak](https://github.com/hbldh/bleak).

## Project Structure

*   `dfu_lib.py`: The core library containing all DFU logic and Bluetooth operations.
*   `dfu_cli.py`: The command-line interface (logic equivalent to the original script).
*   `dfu_gui.py`: A Tkinter-based GUI with device scanning and visual progress tracking.

## Features

*   **Dual Interface:** Choose between a scriptable CLI or a user-friendly GUI.
*   **Buttonless DFU:** Automatically switches the device from Application mode to Bootloader mode.
*   **Legacy DFU Protocol:** Supports the standard Nordic Legacy DFU process (SDK < 12 or Adafruit Bootloader).
*   **Zip Support:** Accepts standard firmware `.zip` packages (containing `manifest.json`, `.bin`, and `.dat`).
*   **Cross-Platform:** Works on Windows, macOS, and Linux.
*   **Tunable:** Configurable Packet Receipt Notification (PRN), scan timeouts, and transmission delays.

## Prerequisites

*   Python 3.9 or higher.
*   A Bluetooth Low Energy (BLE) adapter.

## Installation

1.  **Clone or download this repository.**
2.  **Install Python dependencies:**

```bash
pip install bleak
```

3.  **Linux Users Only:** You may need to install Tkinter explicitly for the GUI:
    ```bash
    sudo apt-get install python3-tk
    ```

## Usage

### 1. Graphical User Interface (GUI)

The GUI allows you to scan for devices, filter by signal strength (RSSI), and configure settings visually.

```bash
python dfu_gui.py
```

**Steps:**
1.  **Browse ZIP:** Select your firmware package.
2.  **Settings:**
    *   **Force Scan:** (Default: On) Forces a fresh discovery to find device services.
    *   **PRN:** (Default: 8) Packet Receipt Notification interval.
    *   **Scan Timeout:** (Default: 5s) How long to search for devices.
3.  **Scan Devices:** Click to populate the list. Devices are sorted by signal strength.
4.  **Select Device:** Click on the target device in the list.
5.  **Start Update:** Begins the DFU process. Check the "Log" window for details.

---

### 2. Command Line Interface (CLI)

The CLI is ideal for scripts or headless environments.

```bash
python dfu_cli.py <zip_file> <device_identifier> [options]
```

#### Arguments

| Argument | Description |
| :--- | :--- |
| `file` | Path to the `.zip` firmware file. |
| `device` | The BLE name (e.g., `MyDevice`) or MAC Address (e.g., `AA:BB:CC:11:22:33`) of the target. |
| `--scan` | Force a scan for the device even if a MAC address is provided (Recommended). |
| `--prn <N>` | Packet Receipt Notification interval. Default is `8`. |
| `--delay <S>` | **Critical:** Delay in seconds between "Start DFU" and "Firmware Size". Default is `0.4`. |
| `--verbose` | Enable debug logging to see detailed BLE traffic. |

#### Examples

**Basic Update (using Device Name):**
```bash
python dfu_cli.py  --scan firmware.zip MyDevice
```

**Update using MAC Address:**
```bash
python dfu_cli.py --scan firmware.zip AA:BB:CC:DD:EE:FF
```

**Update a slow device (Adafruit/Seeed Bootloaders):**
```bash
python dfu_cli.py --delay 0.5 --prn 4 --scan firmware.zip MyDevice
```

## Building Standalone Binaries

You can compile this tool into a standalone executable (`.exe`, `.app`, or Linux binary) using **PyInstaller**.

1.  **Install PyInstaller:**
    ```bash
    pip install pyinstaller
    ```
2.  **Build the GUI:**
    ```bash
    pyinstaller dfu_gui.py --onefile --windowed --name "NordicDFU"
    ```
    *   The output will be in the `dist/` folder.
    *   `--windowed` hides the console window.
    *   `--onefile` bundles everything into a single file.

## Troubleshooting

### "Device not found" / Scan issues
*   **GUI:** Ensure "Force Scan" is checked. Try increasing the "Scan Timeout".
*   **Linux:** Ensure your user has permissions to access the Bluetooth controller (add user to `bluetooth` group).
*   **macOS:** MAC addresses are hidden. Rely on the Device Name or UUID.

### "Timeout waiting for response to Op Code 0x1"
This occurs when the computer sends the firmware size packet before the device has finished processing the "Start" command.
*   **Fix:** Increase the delay using `--delay 0.6` or higher.

### "Upload failed" or Stalling
*   Try reducing the PRN value: `--prn 4` or `--prn 1`. This slows down the upload but ensures the device acknowledges packets more frequently.

## Compatibility

Tested with:
*   **Adafruit nRF52 Bootloader** (Used in Adafruit Feather, Seeed XIAO nRF52, RAK4631, etc.).
*   **Nordic SDK 11/12 Legacy Bootloaders**.

*Note: This tool does not support the "Secure DFU" protocol introduced in Nordic SDK 12+. It supports "Legacy DFU" only.*

## License

This utility is a Python implementation based on logic from the open-source Nordic Semiconductor Android DFU Library.

Use at your own risk. Ensure you have recovery mechanisms (e.g., a physical access to board USB, SWD interface) available when performing firmware updates.
