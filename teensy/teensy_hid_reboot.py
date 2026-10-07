#!/usr/bin/env python3
"""teensy/teensy_hid_reboot.py — put a Teensy that is NOT in Serial USB mode into its HalfKay
bootloader, from the Pi it is plugged into (system python3, stdlib only).

teensy_loader_cli's soft reboot (-s) only speaks to the Serial USB identity (16c0:0483). A fresh
board enumerates as RawHID (16c0:0486), where the reboot request is a 4-byte HID feature report
(A9 45 C2 6B) to the Teensyduino serial-emulation interface — what the Teensy Loader GUI does.
Exit 0 once lsusb shows the bootloader (16c0:0478), 1 if no Teensyduino HID interface took it.
"""
import fcntl, glob, os, subprocess, sys, time

HIDIOCSFEATURE = lambda n: (3 << 30) | (n << 16) | (ord("H") << 8) | 0x06      # noqa: E731
REBOOT_REQUEST = bytearray([0x00, 0xA9, 0x45, 0xC2, 0x6B])                      # report id 0 + payload


def usb_ids() -> str:
    return subprocess.run(["lsusb", "-d", "16c0:"], capture_output=True, text=True).stdout


def main() -> int:
    if "0478" in usb_ids():
        print("Teensy already in HalfKay bootloader")
        return 0
    devs = []
    for h in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            if "Teensyduino" in open(h + "/device/uevent").read():
                devs.append("/dev/" + os.path.basename(h))
        except OSError:
            pass
    for dev in reversed(devs):            # the serial-emulation interface is the higher-numbered one
        try:
            fd = os.open(dev, os.O_RDWR)
            fcntl.ioctl(fd, HIDIOCSFEATURE(len(REBOOT_REQUEST)), REBOOT_REQUEST)
            os.close(fd)
        except OSError as e:
            print(f"{dev}: {e}")
            continue
        for _ in range(25):
            time.sleep(0.1)
            if "0478" in usb_ids():
                print(f"Teensy rebooted into HalfKay via {dev}")
                return 0
    print("no HID reboot: " + (", ".join(devs) if devs else "no Teensyduino HID interface on USB"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
