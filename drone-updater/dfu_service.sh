while true; do
    /usr/bin/python3 dfu_cli.py --scan --wait --retry 10 /boot/firmware/firmware.zip RAK4631_OTA AdaDFU
    sleep 5
done
