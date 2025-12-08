#!/usr/bin/env python3
import asyncio
import argparse
import logging
import sys
import time

from dfu_lib import NordicLegacyDFU, find_device_by_name_or_address, DfuException, DFU_SERVICE_UUID

# --- Custom Logger for CLI ---
class MsFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        t = time.strftime("%H:%M:%S", ct)
        return f"{t}.{int(record.msecs):03d}"

    def format(self, record):
        timestamp = self.formatTime(record)
        msg = record.getMessage()
        return f"{timestamp}  {msg}"

logger = logging.getLogger("DFU_CLI")

def cli_progress_handler(pct):
    sys.stdout.write(f"\rUploading: {pct}%")
    sys.stdout.flush()
    if pct == 100:
        sys.stdout.write("\n")

async def main():
    parser = argparse.ArgumentParser(description="Nordic Semi Buttonless Legacy DFU Utility (CLI)")
    parser.add_argument("file", help="Path to the ZIP firmware file")
    parser.add_argument("device", help="Device Name or BLE Address")
    parser.add_argument("--scan", action="store_true", help="Force scan even if address is provided")
    parser.add_argument("--adapter", default=None, help="Bluetooth Adapter interface (Linux: hci0)")
    parser.add_argument("--prn", type=int, default=8, help="PRN interval (default 8)")
    parser.add_argument("--delay", type=float, default=0.4, help="Start/Size Delay (default 0.4s)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logs")

    # New Arguments
    parser.add_argument("--wait", action="store_true", help="Loop indefinitely until the target device is found")
    parser.add_argument("--retry", type=int, default=3, help="Number of DFU connection retries (default 3)")

    args = parser.parse_args()

    handler = logging.StreamHandler()
    if args.verbose:
        handler.setFormatter(MsFormatter())
        logger.setLevel(logging.DEBUG)
        logging.getLogger("bleak").setLevel(logging.WARNING)
        logging.getLogger("DFU_LIB").setLevel(logging.DEBUG)
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        logger.setLevel(logging.INFO)
        logging.getLogger("bleak").setLevel(logging.ERROR)
        logging.getLogger("DFU_LIB").setLevel(logging.INFO)

    logger.addHandler(handler)
    logging.getLogger("DFU_LIB").addHandler(handler) # Attach handler to lib logger

    try:
        # Pass None for log_callback so the library uses the standard logger configured above
        dfu = NordicLegacyDFU(args.file, args.prn, args.delay, adapter=args.adapter, progress_callback=cli_progress_handler)
        dfu.parse_zip()

        logger.info(f"Scanning for {args.device}...")

        # --- WAIT / SCAN Loop ---
        app_device = None
        while True:
            try:
                app_device = await find_device_by_name_or_address(args.device, args.scan, adapter=args.adapter)
                break # Found!
            except DfuException:
                if args.wait:
                    logger.info(f"Device {args.device} not found. Waiting...")
                    await asyncio.sleep(2.0)
                    continue
                else:
                    raise # Rethrow if not waiting

        await dfu.jump_to_bootloader(app_device)

        logger.info("Waiting for reboot (5s)...")
        await asyncio.sleep(5.0)

        bootloader_device = None
        try:
            logger.info("Scanning for Bootloader (UUID)...")
            bootloader_device = await find_device_by_name_or_address("DFU", force_scan=True, adapter=args.adapter, service_uuid=DFU_SERVICE_UUID)
        except DfuException:
            pass

        if not bootloader_device:
            original_mac = app_device.address
            if ":" in original_mac and len(original_mac) == 17:
                try:
                    prefix = original_mac[:-2]
                    last_byte = int(original_mac[-2:], 16)
                    last_byte = (last_byte + 1) & 0xFF
                    bootloader_mac_hint = f"{prefix}{last_byte:02X}"
                    logger.info(f"Scanning for Bootloader (Hint: {bootloader_mac_hint})...")
                    bootloader_device = await find_device_by_name_or_address(bootloader_mac_hint, force_scan=True, adapter=args.adapter)
                except: pass

        if not bootloader_device:
            raise DfuException("Could not locate DFU Bootloader device.")

        # Pass the custom retry count here
        await dfu.perform_update(bootloader_device, max_retries=args.retry)

    except KeyboardInterrupt:
        logger.info("\nOperation Cancelled by User.")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())