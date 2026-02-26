# --- START OF FILE dfu_lib.py ---
import asyncio
import logging
import struct
import zipfile
import json
import os
import warnings
from typing import Optional, Callable, List

from bleak import BleakScanner, BleakClient, BleakError
from bleak.backends.device import BLEDevice

# --- UUID Constants ---
DFU_SERVICE_UUID = "00001530-1212-efde-1523-785feabcd123"
DFU_CONTROL_POINT_UUID = "00001531-1212-efde-1523-785feabcd123"
DFU_PACKET_UUID = "00001532-1212-efde-1523-785feabcd123"

# --- Op Codes ---
OP_CODE_START_DFU = 0x01
OP_CODE_INIT_DFU_PARAMS = 0x02
OP_CODE_RECEIVE_FIRMWARE_IMAGE = 0x03
OP_CODE_VALIDATE = 0x04
OP_CODE_ACTIVATE_AND_RESET = 0x05
OP_CODE_RESET = 0x06
OP_CODE_PACKET_RECEIPT_NOTIF_REQ = 0x08
OP_CODE_RESPONSE_CODE = 0x10
OP_CODE_PACKET_RECEIPT_NOTIF = 0x11
OP_CODE_ENTER_BOOTLOADER = 0x01
UPLOAD_MODE_SOFTDEVICE  = 0x01
UPLOAD_MODE_BOOTLOADER  = 0x02
UPLOAD_MODE_SD_BL       = 0x03  # SoftDevice + Bootloader combined
UPLOAD_MODE_APPLICATION = 0x04

_UPLOAD_MODE_NAMES = {
    UPLOAD_MODE_SOFTDEVICE:  "SoftDevice",
    UPLOAD_MODE_BOOTLOADER:  "Bootloader",
    UPLOAD_MODE_SD_BL:       "SoftDevice+Bootloader",
    UPLOAD_MODE_APPLICATION: "Application",
}

logger = logging.getLogger("DFU_LIB")

class DfuException(Exception):
    pass

class NordicLegacyDFU:
    def __init__(self, zip_path: str, prn: int, packet_delay: float, adapter: str = None,
                 high_mtu: bool = False,
                 progress_callback: Callable[[int], None] = None,
                 log_callback: Callable[[str], None] = None):
        self.zip_path = zip_path
        self.prn = prn
        self.packet_delay = packet_delay
        self.adapter = adapter
        self.high_mtu = high_mtu
        self.progress_callback = progress_callback
        self.log_callback = log_callback

        self.manifest = None
        self.bin_data = None
        self.dat_data = None
        self.upload_mode = UPLOAD_MODE_APPLICATION
        self.sd_size = 0
        self.bl_size = 0
        self.app_size = 0
        self.client: Optional[BleakClient] = None

        self.response_queue = asyncio.Queue()
        self.pkg_receipt_event = asyncio.Event()
        self.bytes_sent = 0
        self.reset_in_progress = False

    def _log(self, msg: str, level=logging.INFO):
        """Internal helper to route logs to both logger and callback."""
        if level == logging.ERROR:
            logger.error(msg)
        elif level == logging.DEBUG:
            logger.debug(msg)
        else:
            logger.info(msg)

        if self.log_callback:
            self.log_callback(msg)

    async def _setup_mtu(self):
        if not self.client:
            return 23

        if not self.high_mtu:
            return 23

        if hasattr(self.client, "_backend"):
            if hasattr(self.client._backend, "_acquire_mtu"):
                try:
                    await self.client._backend._acquire_mtu()
                except Exception:
                    pass

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            try:
                mtu = self.client.mtu_size
            except:
                mtu = 23
        return mtu

    def parse_zip(self):
        if not os.path.exists(self.zip_path):
            raise FileNotFoundError(f"File not found: {self.zip_path}")

        with zipfile.ZipFile(self.zip_path, 'r') as z:
            if 'manifest.json' in z.namelist():
                with z.open('manifest.json') as f:
                    self.manifest = json.load(f)

                m = self.manifest.get('manifest', {})

                if 'softdevice_bootloader' in m:
                    info = m['softdevice_bootloader']
                    self.bin_data = z.read(info['bin_file'])
                    self.dat_data = z.read(info['dat_file'])
                    self.sd_size = info.get('sd_size', 0)
                    self.bl_size = info.get('bl_size', 0)
                    if self.sd_size == 0 and self.bl_size == 0:
                        raise DfuException(
                            "softdevice_bootloader manifest entry must include 'sd_size' and 'bl_size'.")
                    self.app_size = 0
                    self.upload_mode = UPLOAD_MODE_SD_BL

                elif 'bootloader' in m:
                    info = m['bootloader']
                    self.bin_data = z.read(info['bin_file'])
                    self.dat_data = z.read(info['dat_file'])
                    self.sd_size = 0
                    self.bl_size = len(self.bin_data)
                    self.app_size = 0
                    self.upload_mode = UPLOAD_MODE_BOOTLOADER

                elif 'softdevice' in m:
                    info = m['softdevice']
                    self.bin_data = z.read(info['bin_file'])
                    self.dat_data = z.read(info['dat_file'])
                    self.sd_size = len(self.bin_data)
                    self.bl_size = 0
                    self.app_size = 0
                    self.upload_mode = UPLOAD_MODE_SOFTDEVICE

                elif 'application' in m:
                    info = m['application']
                    self.bin_data = z.read(info['bin_file'])
                    self.dat_data = z.read(info['dat_file'])
                    self.sd_size = 0
                    self.bl_size = 0
                    self.app_size = len(self.bin_data)
                    self.upload_mode = UPLOAD_MODE_APPLICATION

                else:
                    raise DfuException(
                        "Unrecognized manifest. Expected one of: application, bootloader, "
                        "softdevice, or softdevice_bootloader.")

            else:
                self._log("No manifest.json. Attempting legacy compatibility mode.")
                files = z.namelist()

                bl_bin  = next((f for f in files if f.endswith('.bin') and 'bootloader'  in f.lower()), None)
                sd_bin  = next((f for f in files if f.endswith('.bin') and 'softdevice'  in f.lower()), None)
                app_bin = next((f for f in files if f.endswith('.bin') and 'application' in f.lower()), None)
                bl_dat  = next((f for f in files if f.endswith('.dat') and 'bootloader'  in f.lower()), None)
                sd_dat  = next((f for f in files if f.endswith('.dat') and 'softdevice'  in f.lower()), None)
                app_dat = next((f for f in files if f.endswith('.dat') and 'application' in f.lower()), None)

                if bl_bin and sd_bin:
                    raise DfuException(
                        "Found both softdevice and bootloader BINs without a manifest.json. "
                        "Cannot determine individual sizes. Please add a manifest.json with "
                        "'sd_size' and 'bl_size'.")
                elif bl_bin:
                    self.bin_data = z.read(bl_bin)
                    self.dat_data = z.read(bl_dat) if bl_dat else b''
                    self.sd_size = 0
                    self.bl_size = len(self.bin_data)
                    self.app_size = 0
                    self.upload_mode = UPLOAD_MODE_BOOTLOADER
                elif sd_bin:
                    self.bin_data = z.read(sd_bin)
                    self.dat_data = z.read(sd_dat) if sd_dat else b''
                    self.sd_size = len(self.bin_data)
                    self.bl_size = 0
                    self.app_size = 0
                    self.upload_mode = UPLOAD_MODE_SOFTDEVICE
                elif app_bin and app_dat:
                    self.bin_data = z.read(app_bin)
                    self.dat_data = z.read(app_dat)
                    self.sd_size = 0
                    self.bl_size = 0
                    self.app_size = len(self.bin_data)
                    self.upload_mode = UPLOAD_MODE_APPLICATION
                else:
                    raise DfuException("Could not auto-detect firmware files in ZIP.")

    async def _notification_handler(self, sender, data):
        data = bytearray(data)
        opcode = data[0]

        if opcode == OP_CODE_RESPONSE_CODE:
            request_op = data[1]
            status = data[2]
            logger.debug(f"<< RX Resp: Op={request_op:#02x} Status={status}")
            await self.response_queue.put((request_op, status))

        elif opcode == OP_CODE_PACKET_RECEIPT_NOTIF:
            if len(data) >= 5:
                bytes_received = struct.unpack('<I', data[1:5])[0]
                logger.debug(f"<< RX PRN: {bytes_received}")
            self.pkg_receipt_event.set()

    async def _wait_for_response(self, expected_op_code, timeout=30.0):
        try:
            request_op, status = await asyncio.wait_for(self.response_queue.get(), timeout)
            if request_op != expected_op_code:
                return -1

            if status != 1: # 1 = SUCCESS
                self._log(f"<< RX Error: Command {expected_op_code:#02x} failed with status {status}", logging.ERROR)
                return status
            return 1
        except asyncio.TimeoutError:
            self._log(f"Timeout ({timeout}s) waiting for response", logging.ERROR)
            return -1

    async def jump_to_bootloader(self, device: BLEDevice):
        self._log(f"Connecting to {device.name} ({device.address}) for Jump...")
        try:
            async with BleakClient(device, adapter=self.adapter) as client:
                self.client = client
                await client.start_notify(DFU_CONTROL_POINT_UUID, self._notification_handler)
                mtu = await self._setup_mtu()
                self._log(f"Connected. MTU: {mtu}")

                payload = bytearray([OP_CODE_ENTER_BOOTLOADER, UPLOAD_MODE_APPLICATION])

                logger.debug(f">> TX Jump: {payload.hex()}")
                try:
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, payload, response=True)
                except Exception:
                    pass
                self._log("Jump command sent.")
        except Exception as e:
            self._log(f"Jump connection sequence ended: {e}")

    async def perform_update(self, device: BLEDevice, max_retries: int = 3):
        self._log(f"Target Bootloader: {device.address}")
        self.reset_in_progress = False

        for attempt in range(max_retries):
            self._log(f"DFU connection attempt {attempt+1}/{max_retries}...")

            try:
                async with BleakClient(device, timeout=20.0, adapter=self.adapter) as client:
                    self.client = client

                    await client.start_notify(DFU_CONTROL_POINT_UUID, self._notification_handler)

                    mtu = await self._setup_mtu()
                    self._log(f"Connected to Bootloader. MTU: {mtu}")

                    while not self.response_queue.empty(): self.response_queue.get_nowait()

                    # Start DFU
                    mode_name = _UPLOAD_MODE_NAMES.get(self.upload_mode, f"0x{self.upload_mode:02x}")
                    self._log(f"Firmware type: {mode_name} (mode=0x{self.upload_mode:02x})")
                    start_payload = bytearray([OP_CODE_START_DFU, self.upload_mode])
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, start_payload, response=True)

                    if self.packet_delay > 0:
                        await asyncio.sleep(self.packet_delay)

                    size_payload = struct.pack('<III', self.sd_size, self.bl_size, self.app_size)

                    self._log(f"Sending sizes: SD={self.sd_size} BL={self.bl_size} App={self.app_size} bytes")
                    await client.write_gatt_char(DFU_PACKET_UUID, size_payload, response=False)

                    status = await self._wait_for_response(OP_CODE_START_DFU, timeout=60.0)
                    if status != 1:
                        await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_RESET]), response=True)
                        raise DfuException("Start DFU sequence failed")

                    # Init Packet
                    self._log("Sending Init Packet...")
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_INIT_DFU_PARAMS, 0x00]), response=True)
                    await client.write_gatt_char(DFU_PACKET_UUID, self.dat_data, response=False)
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_INIT_DFU_PARAMS, 0x01]), response=True)

                    status = await self._wait_for_response(OP_CODE_INIT_DFU_PARAMS)
                    if status != 1: raise DfuException(f"Init Packet failed. Status: {status}")

                    # PRN
                    if self.prn > 0:
                        self._log(f"Configuring PRN: {self.prn}")
                        prn_payload = bytearray([OP_CODE_PACKET_RECEIPT_NOTIF_REQ]) + struct.pack('<H', self.prn)
                        await client.write_gatt_char(DFU_CONTROL_POINT_UUID, prn_payload, response=True)

                    # Stream
                    self._log("Requesting Upload...")
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_RECEIVE_FIRMWARE_IMAGE]), response=True)
                    await self._stream_firmware()

                    # Validate
                    self._log("Verifying Upload...")
                    flash_write_timeout = max(60.0, len(self.bin_data) / 50000) # Longer timeout for flash write completion - ~1s per 50KB
                    status = await self._wait_for_response(OP_CODE_RECEIVE_FIRMWARE_IMAGE, timeout=flash_write_timeout)
                    if status != 1: raise DfuException(f"Upload failed. Status: {status}")

                    self._log("Validating...")
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_VALIDATE]), response=True)
                    status = await self._wait_for_response(OP_CODE_VALIDATE)
                    if status != 1: raise DfuException(f"Validation failed. Status: {status}")

                    # Reset
                    self._log("Activating & Resetting...")
                    self.reset_in_progress = True
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_ACTIVATE_AND_RESET]), response=True)
                    self._log("DFU Complete.")
                    return # SUCCESS

            except Exception as e:
                if self.reset_in_progress:
                    self._log(f"Device disconnected during reset. Update Successful.")
                    return
                self._log(f"Attempt {attempt+1} failed: {e}", logging.ERROR)
                if attempt < max_retries - 1:
                    await asyncio.sleep(3.0)
                else:
                    raise e

    async def _stream_firmware(self):
        if not self.high_mtu:
            mtu = 23
        else:
            # Suppress warning when reading MTU for chunk calculation
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                mtu = self.client.mtu_size if self.client else 23
        chunk_size = min(mtu - 3, 244)  # ATT overhead, cap at 244
        if chunk_size < 20: chunk_size = 20
        self._log(f"Using chunk_size = {chunk_size}")
        self._last_progress_block = -1
        total_bytes = len(self.bin_data)
        packets_since_prn = 0
        self.bytes_sent = 0
        prn_timeout = max(0.8, self.prn * 0.08) # Estimate timeout based on PRN
        self._log(f"PRN Timeout set to {prn_timeout:.2f} seconds")

        self._log(f"Uploading {total_bytes} bytes...")

        for i in range(0, total_bytes, chunk_size):
            chunk = self.bin_data[i : i + chunk_size]
            await self.client.write_gatt_char(DFU_PACKET_UUID, chunk, response=False)
            self.bytes_sent += len(chunk)
            packets_since_prn += 1

            pct = int((self.bytes_sent * 100) / total_bytes)

            last_pct = getattr(self, "_last_progress_pct", -1)
            if pct > last_pct:
                if self.progress_callback:
                    self.progress_callback(pct)
                self._last_progress_pct = pct

            if self.prn > 0 and packets_since_prn >= self.prn:
                self.pkg_receipt_event.clear()
                try:
                    await asyncio.wait_for(self.pkg_receipt_event.wait(), timeout=prn_timeout)
                except asyncio.TimeoutError:
                    self._log("PRN Timeout, continuing anyway...", logging.WARNING)
                packets_since_prn = 0

        if self.progress_callback:
            if getattr(self, "_last_progress_pct", -1) < 100:
                self.progress_callback(100)

async def scan_for_devices(adapter: str = None) -> List[BLEDevice]:
    """Returns a list of all found devices (simple scan)."""
    scanner = BleakScanner(adapter=adapter)
    return await scanner.discover(timeout=5.0)

async def find_device_by_name_or_address(name_or_address: str, force_scan: bool, adapter: str = None, service_uuid: str = None) -> BLEDevice:
    """
    Helper to find a specific device.
    """
    if not force_scan and not adapter:
        try:
            device = await BleakScanner.find_device_by_address(name_or_address, timeout=10.0)
            if device: return device
        except BleakError:
            pass

    scanner = BleakScanner(adapter=adapter)
    scanned_devices = await scanner.discover(timeout=5.0, return_adv=True)

    target = None

    for key, (d, adv) in scanned_devices.items():
        if d.address.upper() == name_or_address.upper():
            target = d; break

        adv_name = adv.local_name or d.name or ""
        if adv_name == name_or_address:
            target = d; break

        if not target and service_uuid:
            if service_uuid.lower() in [u.lower() for u in adv.service_uuids]:
                target = d; break

    if not target:
        raise DfuException("Device not found.")

    return target

async def find_any_device(identifiers: List[str], adapter: str = None, service_uuid: str = None) -> BLEDevice:
    """
    Scans once and checks if ANY of the provided identifiers match found devices.
    Returns the first device that matches.
    """
    scanner = BleakScanner(adapter=adapter)
    # Perform a single broadcast scan
    scanned_devices = await scanner.discover(timeout=5.0, return_adv=True)

    for identifier in identifiers:
        identifier_upper = identifier.upper()

        for key, (d, adv) in scanned_devices.items():
            # 1. Check Address Match
            if d.address.upper() == identifier_upper:
                return d

            # 2. Check Name Match
            adv_name = adv.local_name or d.name or ""
            if adv_name == identifier:
                return d

            # 3. Check Service UUID (only if identifier matches special UUID string if applicable)
            # (Logic handled separately usually, but here checking generally)
            if service_uuid and service_uuid.lower() in [u.lower() for u in adv.service_uuids]:
                # This is a bit ambiguous if multiple devices have the UUID,
                # but this function targets specific identifiers.
                # If identifier was "DFU_SERVICE", it would catch here.
                pass

    raise DfuException(f"No devices found matching: {identifiers}")
