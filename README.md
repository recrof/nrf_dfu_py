# Python Nordic Legacy DFU Tool

A utility to perform **Legacy Device Firmware Updates (DFU)** on Nordic Semiconductor nRF51/nRF52 devices using Python.

This project has been split into a modular library, a Command Line Interface (CLI), and a Graphical User Interface (GUI). It is designed to replicate the logic of the official [Nordic Android DFU Library](https://github.com/NordicSemiconductor/Android-DFU-Library), specifically handling the **Buttonless Jump** and **Legacy DFU** protocols via [Bleak](https://github.com/hbldh/bleak).

## Project Structure

*   `dfu_lib.py`: The core library containing all DFU logic and Bluetooth operations.
*   `dfu_cli.py`: The command-line interface.
*   `dfu_gui.py`: A Tkinter-based GUI with real-time device scanning.

## Features

*   **Dual Interface:** Choose between a scriptable CLI or a user-friendly GUI.
*   **Multi-Device Targeting (CLI):** Specify multiple target names or addresses; the tool will connect to the first one found.
*   **Persistent Scanning:** The `--wait` flag allows the CLI to loop indefinitely until a target device appears.
*   **Buttonless DFU:** Automatically switches the device from Application mode to Bootloader mode.
*   **Legacy DFU Protocol:** Supports the standard Nordic Legacy DFU process (SDK < 12 or Adafruit Bootloader).
*   **Tunable:** Configurable Packet Receipt Notification (PRN), timeouts, retries, and transmission delays.

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

The CLI is ideal for scripts, headless environments, or mass deployment.

```bash
python dfu_cli.py <zip_file> <device_1> [device_2 ...] [options]
```

#### Arguments

| Argument | Description |
| :--- | :--- |
| `file` | Path to the `.zip` firmware file. |
| `device` | **One or more** BLE names (e.g., `MyDevice`) or MAC Addresses. The tool will scan for all provided identifiers. |
| `--wait` | Loop indefinitely scanning for the provided device(s) until one is found. |
| `--retry <N>` | Number of connection/update attempts if failures occur (Default: `3`). |
| `--scan` | Force a scan for the device even if a MAC address is provided (Recommended). |
| `--prn <N>` | Packet Receipt Notification interval. Default is `8`. |
| `--delay <S>` | **Critical:** Delay in seconds between "Start DFU" and "Firmware Size". Default is `0.4`. |
| `--verbose` | Enable debug logging to see detailed BLE traffic. |

#### Examples

**1. Basic Update (Single Device):**
```bash
python dfu_cli.py --scan firmware.zip MyDevice
```

**2. Target Multiple Devices (First Found):**
This is useful if your devices might have different names or if you want to update whichever device appears first.
```bash
python dfu_cli.py firmware.zip DeviceA DeviceB AA:BB:CC:11:22:33
```

**3. Wait for a device to appear (Persistent Mode):**
This will keep scanning in a loop until `MyDevice` starts advertising.
```bash
python dfu_cli.py --wait --scan firmware.zip MyDevice
```

**4. Update a slow device with custom retries:**
```bash
python dfu_cli.py --delay 0.6 --prn 4 --retry 5 firmware.zip MyDevice
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

### "Device not found"
*   **GUI:** Ensure "Force Scan" is checked.
*   **CLI:** Use the `--scan` flag. If the device is not currently advertising, add `--wait` to keep searching.
*   **Linux:** Ensure your user has permissions to access the Bluetooth controller (add user to `bluetooth` group).

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