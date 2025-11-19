import asyncio
import argparse
import logging
import sys
import os
import struct
import zipfile
import json
from typing import Optional, Tuple

from bleak import BleakScanner, BleakClient, BleakError
from bleak.backends.device import BLEDevice

# --- UUID Constants (Lowercase for cross-platform consistency) ---
DFU_SERVICE_UUID = "00001530-1212-efde-1523-785feabcd123"
DFU_CONTROL_POINT_UUID = "00001531-1212-efde-1523-785feabcd123"
DFU_PACKET_UUID = "00001532-1212-efde-1523-785feabcd123"
DFU_VERSION_UUID = "00001534-1212-efde-1523-785feabcd123"

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

# Buttonless specific
OP_CODE_ENTER_BOOTLOADER = 0x01
UPLOAD_MODE_APPLICATION = 0x04

# --- Logging Setup ---
logger = logging.getLogger("DFU")

class DfuException(Exception):
    pass

class NordicLegacyDFU:
    def __init__(self, zip_path: str, prn: int, packet_delay: float, adapter: str = None):
        self.zip_path = zip_path
        self.prn = prn
        self.packet_delay = packet_delay
        self.adapter = adapter  # Specific adapter (e.g., 'hci0')

        self.manifest = None
        self.bin_data = None
        self.dat_data = None
        self.client: Optional[BleakClient] = None

        self.response_queue = asyncio.Queue()
        self.pkg_receipt_event = asyncio.Event()
        self.bytes_sent = 0

    def parse_zip(self):
        if not os.path.exists(self.zip_path):
            raise FileNotFoundError(f"File not found: {self.zip_path}")

        with zipfile.ZipFile(self.zip_path, 'r') as z:
            if 'manifest.json' in z.namelist():
                with z.open('manifest.json') as f:
                    self.manifest = json.load(f)

                if 'manifest' in self.manifest and 'application' in self.manifest['manifest']:
                    app_info = self.manifest['manifest']['application']
                    bin_name = app_info['bin_file']
                    dat_name = app_info['dat_file']

                    logger.info(f"Found Application firmware: {bin_name}")
                    self.bin_data = z.read(bin_name)
                    self.dat_data = z.read(dat_name)
                else:
                    raise DfuException("Zip must contain an Application firmware manifest.")
            else:
                logger.warning("No manifest.json found. Attempting legacy compatibility mode.")
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
            logger.debug(f"<< RX Response: Request {request_op:#02x}, Status {status}")
            await self.response_queue.put((request_op, status))

        elif opcode == OP_CODE_PACKET_RECEIPT_NOTIF:
            if len(data) >= 5:
                bytes_received = struct.unpack('<I', data[1:5])[0]
                logger.debug(f"<< RX PRN: {bytes_received} bytes")
            self.pkg_receipt_event.set()

    async def _wait_for_response(self, expected_op_code, timeout=20.0):
        try:
            request_op, status = await asyncio.wait_for(self.response_queue.get(), timeout)
            if request_op != expected_op_code:
                logger.warning(f"Unexpected response op code: {request_op:#02x}, expected {expected_op_code:#02x}. Ignoring.")
                return -1

            if status != 1: # 1 = SUCCESS
                logger.error(f"Command {expected_op_code:#02x} failed with status {status}")
                return status

            logger.info(f"Command {expected_op_code:#02x} acknowledged (Status 1)")
            return 1
        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for response to Op Code {expected_op_code:#02x}")
            return -1

    async def jump_to_bootloader(self, device: BLEDevice):
        logger.info(f"Connecting to {device.name} ({device.address}) to trigger Bootloader Jump...")
        try:
            # Pass the adapter arg to the client
            async with BleakClient(device, adapter=self.adapter) as client:
                await client.start_notify(DFU_CONTROL_POINT_UUID, self._notification_handler)
                payload = bytearray([OP_CODE_ENTER_BOOTLOADER, UPLOAD_MODE_APPLICATION])
                logger.info(">> TX Enter Bootloader (0x01, 0x04)")
                try:
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, payload, response=True)
                except Exception as e:
                    logger.debug(f"Jump write exception (normal if device disconnects immediately): {e}")
                logger.info("Jump command sent.")
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"Jump connection error: {e}")

    async def perform_update(self, device: BLEDevice):
        logger.info(f"Target DFU Bootloader: {device.name} ({device.address})")

        max_retries = 3
        for attempt in range(max_retries):
            logger.info(f"Starting DFU connection attempt {attempt+1}/{max_retries}...")

            try:
                # Pass the adapter arg to the client
                async with BleakClient(device, timeout=20.0, adapter=self.adapter) as client:
                    self.client = client

                    logger.info("Connected.")

                    try:
                        version_data = await client.read_gatt_char(DFU_VERSION_UUID)
                        version = struct.unpack('<H', version_data)[0]
                        major = version >> 8
                        minor = version & 0xFF
                        logger.info(f"DFU Version: {major}.{minor}")
                    except Exception as e:
                        logger.warning(f"Could not read DFU Version: {e}")
                        logger.warning("Device might not be ready. Retrying...")
                        continue

                    logger.debug("Enabling notifications...")
                    await client.start_notify(DFU_CONTROL_POINT_UUID, self._notification_handler)

                    while not self.response_queue.empty(): self.response_queue.get_nowait()

                    logger.info("Initialize DFU parameters...")
                    start_payload = bytearray([OP_CODE_START_DFU, UPLOAD_MODE_APPLICATION])
                    logger.debug(f">> TX Start DFU: {start_payload.hex()}")
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, start_payload, response=True)

                    logger.debug(f"Waiting {self.packet_delay}s before sending size...")
                    await asyncio.sleep(self.packet_delay)

                    sd_size = 0
                    bl_size = 0
                    app_size = len(self.bin_data)
                    size_payload = struct.pack('<III', sd_size, bl_size, app_size)

                    logger.info(f"Sending firmware size ({app_size} bytes)...")
                    logger.debug(f">> TX Data (Size): {size_payload.hex()}")

                    await client.write_gatt_char(DFU_PACKET_UUID, size_payload, response=False)

                    status = await self._wait_for_response(OP_CODE_START_DFU)
                    if status != 1:
                        logger.warning(f"Start DFU failed (Status {status}). Attempting Reset...")
                        try:
                            await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_RESET]), response=True)
                        except Exception:
                            pass
                        raise DfuException("Start DFU sequence failed")

                    logger.info("Sending Init Packet...")
                    logger.debug(">> TX Init Params Start")
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_INIT_DFU_PARAMS, 0x00]), response=True)

                    logger.debug(f">> TX Data (Init): {self.dat_data.hex()}")
                    await client.write_gatt_char(DFU_PACKET_UUID, self.dat_data, response=False)

                    logger.debug(">> TX Init Params Complete")
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_INIT_DFU_PARAMS, 0x01]), response=True)

                    status = await self._wait_for_response(OP_CODE_INIT_DFU_PARAMS)
                    if status != 1: raise DfuException(f"Init Packet failed. Status: {status}")

                    if self.prn > 0:
                        logger.info(f"Enabling PRN every {self.prn} packets")
                        prn_payload = bytearray([OP_CODE_PACKET_RECEIPT_NOTIF_REQ]) + struct.pack('<H', self.prn)
                        logger.debug(f">> TX PRN Config: {prn_payload.hex()}")
                        await client.write_gatt_char(DFU_CONTROL_POINT_UUID, prn_payload, response=True)

                    logger.info("Requesting Firmware Upload...")
                    logger.debug(">> TX Receive Firmware Image")
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_RECEIVE_FIRMWARE_IMAGE]), response=True)

                    await self._stream_firmware()

                    logger.info("Checking upload status...")
                    status = await self._wait_for_response(OP_CODE_RECEIVE_FIRMWARE_IMAGE)
                    if status != 1: raise DfuException(f"Upload failed. Status: {status}")

                    logger.info("Validating...")
                    logger.debug(">> TX Validate")
                    await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_VALIDATE]), response=True)
                    status = await self._wait_for_response(OP_CODE_VALIDATE)
                    if status != 1: raise DfuException(f"Validation failed. Status: {status}")

                    logger.info("Activating and Resetting...")
                    try:
                        await client.write_gatt_char(DFU_CONTROL_POINT_UUID, bytearray([OP_CODE_ACTIVATE_AND_RESET]), response=True)
                    except Exception:
                        pass

                    logger.info("DFU Complete. Device should restart.")
                    return

            except Exception as e:
                logger.error(f"Error during attempt {attempt+1}: {e}")
                if attempt < max_retries - 1:
                    logger.info("Waiting 3 seconds before reconnecting...")
                    await asyncio.sleep(3.0)
                else:
                    logger.error("Max retries reached. Update failed.")
                    sys.exit(1)

    async def _stream_firmware(self):
        chunk_size = 20
        total_bytes = len(self.bin_data)
        packets_sent_since_prn = 0
        self.bytes_sent = 0

        logger.info(f"Starting upload: {total_bytes} bytes")

        for i in range(0, total_bytes, chunk_size):
            chunk = self.bin_data[i : i + chunk_size]

            await self.client.write_gatt_char(DFU_PACKET_UUID, chunk, response=False)
            self.bytes_sent += len(chunk)
            packets_sent_since_prn += 1

            if i % 4000 == 0:
                print(f"Progress: {int((self.bytes_sent / total_bytes) * 100)}%", end='\r')

            if self.prn > 0 and packets_sent_since_prn >= self.prn:
                self.pkg_receipt_event.clear()
                try:
                    await asyncio.wait_for(self.pkg_receipt_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("PRN Timeout, continuing...")

                packets_sent_since_prn = 0

        print(f"Progress: 100%")

async def find_device(name_or_address: str, force_scan: bool, adapter: str = None, service_uuid: str = None) -> BLEDevice:
    logger.info(f"Scanning for {name_or_address} (Adapter: {adapter or 'default'})...")

    # If using a specific adapter on Linux, force scanning logic,
    # because find_device_by_address doesn't always respect adapter arg reliably across versions
    if not force_scan and not adapter:
        try:
            # Basic check without adapter preference
            device = await BleakScanner.find_device_by_address(name_or_address, timeout=10.0)
            if device:
                return device
        except BleakError:
            pass

    # Explicit Scan with Adapter selection
    # Note: adapter arg is typically "hci0", "hci1" on Linux. Ignored on Windows/Mac.
    scanner = BleakScanner(adapter=adapter)
    scanned_devices = await scanner.discover(timeout=5.0, return_adv=True)

    target = None

    for key, (d, adv) in scanned_devices.items():
        # Address match
        if d.address.upper() == name_or_address.upper():
            target = d
            break

        # Name match
        adv_name = adv.local_name or d.name or ""
        if adv_name == name_or_address:
            target = d
            break

        # UUID match
        if not target and service_uuid:
            if service_uuid.lower() in [u.lower() for u in adv.service_uuids]:
                target = d
                break

    if not target:
        raise DfuException("Device not found.")

    return target

async def main():
    parser = argparse.ArgumentParser(description="Nordic Semi Buttonless Legacy DFU Utility")
    parser.add_argument("file", help="Path to the ZIP firmware file")
    parser.add_argument("device", help="Device Name or BLE Address")
    parser.add_argument("--scan", action="store_true", help="Force scan even if address is provided")
    parser.add_argument("--adapter", default=None, help="Bluetooth Adapter interface (Linux: hci0, hci1). Ignored on Win/Mac.")
    parser.add_argument("--prn", type=int, default=12, help="PRN interval (default 12, 0 disable)")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay between Start DFU and Size packet in seconds (default 0.2)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logs")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        logging.getLogger("bleak").setLevel(logging.WARNING)

    # Pass adapter to class for client connections
    dfu = NordicLegacyDFU(args.file, args.prn, args.delay, adapter=args.adapter)

    try:
        dfu.parse_zip()

        # Pass adapter to scanner
        app_device = await find_device(args.device, args.scan, adapter=args.adapter)

        await dfu.jump_to_bootloader(app_device)

        logger.info("Waiting for device to reboot into DFU mode (5s)...")
        await asyncio.sleep(5.0)

        bootloader_device = None

        # Method A: DFU Service UUID
        try:
            logger.info("Scanning for DFU Bootloader (Service UUID)...")
            bootloader_device = await find_device("DFU", force_scan=True, adapter=args.adapter, service_uuid=DFU_SERVICE_UUID)
        except DfuException:
            pass

        # Method B: Address guessing
        if not bootloader_device:
            original_mac = app_device.address
            if ":" in original_mac and len(original_mac) == 17:
                try:
                    prefix = original_mac[:-2]
                    last_byte = int(original_mac[-2:], 16)
                    last_byte = (last_byte + 1) & 0xFF
                    bootloader_mac_hint = f"{prefix}{last_byte:02X}"
                    logger.info(f"Scanning for DFU Bootloader (Address Hint: {bootloader_mac_hint})...")
                    bootloader_device = await find_device(bootloader_mac_hint, force_scan=True, adapter=args.adapter)
                except:
                    pass

        if not bootloader_device:
            raise DfuException("Could not locate DFU Bootloader device.")

        await dfu.perform_update(bootloader_device)

    except Exception as e:
        logger.error(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
