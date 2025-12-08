#!/usr/bin/env python3
# --- START OF FILE dfu_gui.py ---

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import asyncio
import os
from datetime import datetime

# Import BleakScanner directly to handle real-time callbacks in the GUI
from bleak import BleakScanner

import dfu_lib
from dfu_lib import NordicLegacyDFU, DFU_SERVICE_UUID

class AsyncHelper:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run_task(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

class DfuApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Nordic DFU Tool")
        self.root.geometry("600x750")

        self.async_helper = AsyncHelper()
        self.selected_device = None

        # Dictionary to store unique found devices {address: device_obj}
        self.found_devices_map = {}

        # Scanning control
        self.scanner = None
        self.scan_cancel_event = None

        # Styles
        style = ttk.Style()
        style.configure("Bold.TLabel", font=("Helvetica", 10, "bold"))

        # --- Section 1: Settings (PRN, Timeout, Force Scan) ---
        settings_frame = ttk.LabelFrame(root, text="Settings", padding=10)
        settings_frame.pack(fill="x", padx=10, pady=5)

        # Force Scan
        self.force_scan_var = tk.BooleanVar(value=True)
        self.chk_force = ttk.Checkbutton(settings_frame, text="Force Scan", variable=self.force_scan_var)
        self.chk_force.grid(row=0, column=0, sticky="w", padx=5)

        # PRN
        ttk.Label(settings_frame, text="PRN:").grid(row=0, column=1, sticky="e", padx=5)
        self.prn_var = tk.StringVar(value="8")
        self.spin_prn = ttk.Spinbox(settings_frame, from_=0, to=100, textvariable=self.prn_var, width=5)
        self.spin_prn.grid(row=0, column=2, sticky="w")

        # Scan Timeout
        ttk.Label(settings_frame, text="Scan Timeout (s):").grid(row=0, column=3, sticky="e", padx=5)
        self.timeout_var = tk.StringVar(value="5")
        self.spin_timeout = ttk.Spinbox(settings_frame, from_=1, to=60, textvariable=self.timeout_var, width=5)
        self.spin_timeout.grid(row=0, column=4, sticky="w")

        # --- Section 2: Firmware File ---
        file_frame = ttk.LabelFrame(root, text="Firmware", padding=10)
        file_frame.pack(fill="x", padx=10, pady=5)

        self.file_path_var = tk.StringVar()
        ttk.Entry(file_frame, textvariable=self.file_path_var, state="readonly").pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(file_frame, text="Browse ZIP", command=self.browse_file).pack(side="right")

        # --- Section 3: Bluetooth Device ---
        device_frame = ttk.LabelFrame(root, text="Target Device", padding=10)
        device_frame.pack(fill="x", padx=10, pady=5)

        self.scan_btn = ttk.Button(device_frame, text="Scan Devices", command=self.start_scan)
        self.scan_btn.pack(fill="x", pady=(0, 5))

        # Listbox with Scrollbar
        list_frame = ttk.Frame(device_frame)
        list_frame.pack(fill="both", expand=True)

        self.dev_listbox = tk.Listbox(list_frame, height=8)
        self.dev_listbox.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.dev_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.dev_listbox.config(yscrollcommand=scrollbar.set)

        self.dev_listbox.bind('<<ListboxSelect>>', self.on_device_select)

        self.lbl_selected = ttk.Label(device_frame, text="No device selected", foreground="red")
        self.lbl_selected.pack(pady=5)

        # --- Section 4: Actions ---
        action_frame = ttk.Frame(root, padding=10)
        action_frame.pack(fill="x", padx=10)

        self.start_btn = ttk.Button(action_frame, text="START UPDATE", command=self.start_update, state="disabled")
        self.start_btn.pack(fill="x", ipady=5)

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(action_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", pady=10)

        # --- Section 5: Log ---
        log_frame = ttk.LabelFrame(root, text="Log", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.log_text = tk.Text(log_frame, state="disabled", font=("Courier", 9))
        self.log_text.pack(fill="both", expand=True)

    def log(self, msg):
        """Thread-safe logging to text widget."""
        def _update():
            self.log_text.configure(state="normal")
            time_str = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert("end", f"[{time_str}] {msg}\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(0, _update)

    def update_progress(self, pct):
        """Thread-safe progress update."""
        self.root.after(0, lambda: self.progress_var.set(pct))

    def browse_file(self):
        filename = filedialog.askopenfilename(filetypes=[("Zip Files", "*.zip")])
        if filename:
            self.file_path_var.set(filename)
            self.check_ready()

    # --- Scanning Logic ---
    def start_scan(self):
        if self.scanner is not None:
            self.log("Scan already in progress.")
            return

        try:
            timeout = int(self.timeout_var.get())
        except ValueError:
            timeout = 5
            self.timeout_var.set("5")

        self.scan_btn.config(state="disabled")

        # Clear previous results
        self.dev_listbox.delete(0, "end")
        self.found_devices_map = {}
        self.selected_device = None
        self.lbl_selected.config(text="No device selected", foreground="red")
        self.check_ready()

        self.log(f"Scanning for {timeout} seconds...")
        self.async_helper.run_task(self._async_scan(timeout))

    def _on_scan_detection(self, device, advertisement_data):
        """Callback from BleakScanner when a device is seen."""
        if device.address not in self.found_devices_map:
            # Filter devices with no name and no address
            if not device.name and not device.address:
                return

            self.found_devices_map[device.address] = device

            # Update UI safely
            self.root.after(0, lambda: self._add_device_to_list(device, advertisement_data))

    def _add_device_to_list(self, device, adv):
        name = device.name if device.name else "Unknown"
        rssi = getattr(device, "rssi", adv.rssi if adv else "N/A")
        if rssi is None: rssi = "N/A"

        display_text = f"{name} ({device.address}) RSSI: {rssi}"
        self.dev_listbox.insert("end", display_text)

    async def _async_scan(self, timeout):
        self.scan_cancel_event = asyncio.Event()
        try:
            self.scanner = BleakScanner(detection_callback=self._on_scan_detection)
            await self.scanner.start()

            # Wait for timeout OR cancellation
            try:
                await asyncio.wait_for(self.scan_cancel_event.wait(), timeout)
                # If we get here without timeout, event was set (manual stop)
                self.log("Scan interrupted.")
            except asyncio.TimeoutError:
                # Timeout reached naturally
                self.log(f"Scan complete. Found {len(self.found_devices_map)} devices.")

            await self.scanner.stop()
        except Exception as e:
            self.log(f"Scan Error: {e}")
        finally:
            self.scanner = None
            self.scan_cancel_event = None
            self.root.after(0, lambda: self.scan_btn.config(state="normal"))

    # --- Selection Logic ---
    def on_device_select(self, event):
        selection = self.dev_listbox.curselection()
        if selection:
            index = selection[0]
            # Match listbox index to map keys (insertion order preserved)
            all_addresses = list(self.found_devices_map.keys())
            if index < len(all_addresses):
                addr = all_addresses[index]
                self.selected_device = self.found_devices_map[addr]
                self.lbl_selected.config(text=f"Selected: {self.selected_device.address}", foreground="green")
                self.check_ready()

    def check_ready(self):
        if self.file_path_var.get() and self.selected_device:
            self.start_btn.config(state="normal")
        else:
            self.start_btn.config(state="disabled")

    # --- DFU Execution ---
    def start_update(self):
        zip_path = self.file_path_var.get()
        if not os.path.exists(zip_path):
            messagebox.showerror("Error", "File does not exist")
            return

        # Get Config
        try:
            prn_val = int(self.prn_var.get())
        except ValueError:
            prn_val = 8

        force_scan = self.force_scan_var.get()

        self.start_btn.config(state="disabled")
        self.scan_btn.config(state="disabled")

        # We start the update process, which includes ensuring scan is stopped
        self.async_helper.run_task(self._async_perform_dfu(zip_path, self.selected_device, prn_val, force_scan))

    async def _stop_scan_if_running(self):
        """Helper to stop the scanner if it is currently running."""
        if self.scanner and self.scan_cancel_event:
            self.log("Stopping active scan...")
            self.scan_cancel_event.set()
            # Wait for the scanner variable to be cleared by the scan loop
            while self.scanner is not None:
                await asyncio.sleep(0.1)

    async def _async_perform_dfu(self, zip_path, device, prn_val, force_scan):
        try:
            # 1. Stop any active scan before starting DFU
            await self._stop_scan_if_running()

            self.log(f"Starting DFU (PRN={prn_val}, ForceScan={force_scan})...")

            dfu = NordicLegacyDFU(
                zip_path,
                prn=prn_val,
                packet_delay=0.4,
                progress_callback=self.update_progress,
                log_callback=self.log
            )
            dfu.parse_zip()

            # 2. Jump to Bootloader
            await dfu.jump_to_bootloader(device)

            self.log("Waiting for reboot (5s)...")
            await asyncio.sleep(5.0)

            # 3. Find Bootloader
            self.log("Scanning for Bootloader...")
            bootloader_device = None

            # A. Try searching for DFU Service UUID
            try:
                bootloader_device = await dfu_lib.find_device_by_name_or_address(
                    "DFU",
                    force_scan=force_scan,
                    service_uuid=DFU_SERVICE_UUID
                )
            except Exception:
                pass

            # B. Try MAC Address Hint (Increment last byte)
            if not bootloader_device:
                original_mac = device.address
                if ":" in original_mac and len(original_mac) == 17:
                    try:
                        prefix = original_mac[:-2]
                        last_byte = int(original_mac[-2:], 16)
                        last_byte = (last_byte + 1) & 0xFF
                        bootloader_mac_hint = f"{prefix}{last_byte:02X}"
                        self.log(f"Trying Address Hint: {bootloader_mac_hint}")
                        bootloader_device = await dfu_lib.find_device_by_name_or_address(
                            bootloader_mac_hint,
                            force_scan=force_scan
                        )
                    except: pass

            if not bootloader_device:
                raise Exception("Could not locate Bootloader device. Try putting device in DFU mode manually.")

            # 4. Perform Update
            await dfu.perform_update(bootloader_device)
            self.log("SUCCESS! Firmware Updated.")
            messagebox.showinfo("Success", "Firmware updated successfully!")

        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
        finally:
            self.root.after(0, lambda: self.start_btn.config(state="normal"))
            self.root.after(0, lambda: self.scan_btn.config(state="normal"))

if __name__ == "__main__":
    root = tk.Tk()
    app = DfuApp(root)
    root.mainloop()
