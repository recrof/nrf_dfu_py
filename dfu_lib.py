# --- START OF FILE dfu_lib.py ---
import asyncio
import logging
import struct
import zipfile
import json
import os
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
UPLOAD_MODE_APPLICATION = 0x04

logger = logging.getLogger("DFU_LIB")

class DfuException(Exception):
    pass

class NordicLegacyDFU:
    def __init__(self, zip_path: str, prn: int, packet_delay: float, adapter: str = None,
                 progress_callback: Callable[[int], None] = None,
                 log_callback: Callable[[str], None] = None):
        self.zip_path = zip_path
        self.prn = prn
        self.packet_delay = packet_delay
        self.adapter = adapter
        self.progress_callback = progress_callback
        self.log_callback = log_callback

        self.manifest = None
        self.bin_data = None
        self.dat_data = None
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

    def parse_zip(self):
        if not os.path.exists(self.zip_path):
            raise FileNotFoundError(f"File not found: {self.zip_path}")

        with zipfile.ZipFile(self.zip_path, 'r') as z:
            if 'manifest.json' in z.namelist():
                with z.open('manifest.json') as f:
                    self.manifest = json.load(f)

                if 'manifest' in self.manifest and 'application' in self.manifest['manifest']:
                    app_info = self.manifest['manifest']['application']
                    self.bin_data = z.read(app_info['bin_file'])
                    self.dat_data = z.read(app_info['dat_file'])
                else:
                    raise DfuException("Zip must contain an Application firmware manifest.")
            else:
                self._log("No manifest.json. Attempting legacy compatibility mode.")
                files = z.namelist()
                bin_file = next((f for f in files if f.endswith('.bin') and 'application' in f.lower()), None)
                dat_file = next((f for f in files if f.endswith('.dat') and 'application' in f.lower()), None)

                if bin_file and dat_file:
                    self.bin_data = z.read(bin_file)
                    self.dat_data = z.read(dat_file)
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
                await client.start_notify(DFU_CONTROL_POINT_UUID, self._notification_handler)
                payload = bytearray([OP_CODE_ENTER_BOOTLOADER, UPLOAD_MODE_APPLICATION])

                logger.debug(f">> TX Jump: {payload.hex()}")
                try:
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, payload, response=True)
                except Exception:
                    pass
                self._log("Jump command sent.")
        except Exception as e:
            self._log(f"Jump connection sequence ended: {e}")

    async def perform_update(self, device: BLEDevice):
        self._log(f"Target Bootloader: {device.address}")
        self.reset_in_progress = False

        max_retries = 3
        for attempt in range(max_retries):
            self._log(f"DFU connection attempt {attempt+1}/{max_retries}...")

            try:
                async with BleakClient(device, timeout=20.0, adapter=self.adapter) as client:
                    self.client = client
                    await client.start_notify(DFU_CONTROL_POINT_UUID, self._notification_handler)
                    while not self.response_queue.empty(): self.response_queue.get_nowait()

                    # Start DFU
                    start_payload = bytearray([OP_CODE_START_DFU, UPLOAD_MODE_APPLICATION])
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, start_payload, response=True)

                    if self.packet_delay > 0:
                        await asyncio.sleep(self.packet_delay)

                    sd_size = 0
                    bl_size = 0
                    app_size = len(self.bin_data)
                    size_payload = struct.pack('<III', sd_size, bl_size, app_size)

                    self._log(f"Sending Size: {app_size} bytes")
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
                    status = await self._wait_for_response(OP_CODE_RECEIVE_FIRMWARE_IMAGE)
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
        chunk_size = 20
        total_bytes = len(self.bin_data)
        packets_since_prn = 0
        self.bytes_sent = 0

        self._log(f"Uploading {total_bytes} bytes...")

        for i in range(0, total_bytes, chunk_size):
            chunk = self.bin_data[i : i + chunk_size]
            await self.client.write_gatt_char(DFU_PACKET_UUID, chunk, response=False)
            self.bytes_sent += len(chunk)
            packets_since_prn += 1

            if i % 2000 == 0 or i == 0:
                pct = int((self.bytes_sent / total_bytes) * 100)
                if self.progress_callback:
                    self.progress_callback(pct)

            if self.prn > 0 and packets_since_prn >= self.prn:
                self.pkg_receipt_event.clear()
                try:
                    await asyncio.wait_for(self.pkg_receipt_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._log("PRN Timeout, continuing anyway...", logging.WARNING)
                packets_since_prn = 0

        if self.progress_callback:
            self.progress_callback(100)

async def scan_for_devices(adapter: str = None) -> List[BLEDevice]:
    """Returns a list of all found devices (simple scan)."""
    scanner = BleakScanner(adapter=adapter)
    return await scanner.discover(timeout=5.0)

async def find_device_by_name_or_address(name_or_address: str, force_scan: bool, adapter: str = None, service_uuid: str = None) -> BLEDevice:
    """
    Helper to find a specific device.
    Uses return_adv=True to correctly inspect Service UUIDs.
    """
    if not force_scan and not adapter:
        try:
            device = await BleakScanner.find_device_by_address(name_or_address, timeout=10.0)
            if device: return device
        except BleakError:
            pass

    scanner = BleakScanner(adapter=adapter)
    # Important: return_adv=True returns a dict {address: (device, advertisement_data)}
    scanned_devices = await scanner.discover(timeout=5.0, return_adv=True)

    target = None

    for key, (d, adv) in scanned_devices.items():
        if d.address.upper() == name_or_address.upper():
            target = d; break

        # Check advertised name (AdvertisementData local_name or Device name)
        adv_name = adv.local_name or d.name or ""
        if adv_name == name_or_address:
            target = d; break

        # Check Service UUIDs in Advertisement Data
        if not target and service_uuid:
            if service_uuid.lower() in [u.lower() for u in adv.service_uuids]:
                target = d; break

    if not target:
        raise DfuException("Device not found.")

    return target