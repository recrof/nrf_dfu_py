## Flash update MeshCore from drone + rpi zero 2w
1. flash raspbian
2. `sudo git clone https://github.com/recrof/nrf_dfu_py/ /opt/nrf_dfu_py`
3. copy `dfu_service.sh` to `/opt/nrf_dfu_py`, `sudo chmod +x dfu_service.sh`
4. copy `nrf_dfu.service` to `/etc/systemd/system/`
5. rename newest RAK4631 repeater/room server firmware to`firmware.zip` and copy it to `/boot/firmware/` - this is the same fat32 drive `BOOTFS` when you put microsd into windows or mac
6. `sudo systemctl enable nrf_dfu`
7. `sudo systemctl start nrf_dfu`
8. deploy the rpi with power source on drone, and it will auto-update any MeshCore RAK4631 in OTA mode