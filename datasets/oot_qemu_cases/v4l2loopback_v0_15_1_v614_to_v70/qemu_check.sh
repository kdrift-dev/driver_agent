#!/bin/sh
set -eu

echo DRIVER_AGENT_V4L2LOOPBACK_TEST_START
/bin/uname -a || true

if command -v driver_agent_load_module >/dev/null 2>&1; then
  driver_agent_load_module devices=1 video_nr=42 card_label=DriverAgent exclusive_caps=1 || {
    echo DRIVER_AGENT_MODULE_LOAD_FAIL
    /bin/dmesg | /bin/tail -80 || true
    /bin/poweroff -f
  }
elif [ -d /lib/modules/$(uname -r) ]; then
  /bin/modprobe "$DRIVER_AGENT_MODULE_NAME" devices=1 video_nr=42 card_label=DriverAgent exclusive_caps=1 || {
    echo DRIVER_AGENT_MODPROBE_FAIL
    /bin/dmesg | /bin/tail -80 || true
    /bin/poweroff -f
  }
else
  /bin/insmod "$DRIVER_AGENT_MODULE_KO" devices=1 video_nr=42 card_label=DriverAgent exclusive_caps=1 || {
    echo DRIVER_AGENT_INSMOD_FAIL
    /bin/dmesg | /bin/tail -80 || true
    /bin/poweroff -f
  }
fi

/bin/sleep 1
/bin/ls /sys/devices/virtual/video4linux || {
  echo DRIVER_AGENT_VIDEO4LINUX_SYSFS_MISSING
  /bin/dmesg | /bin/tail -80 || true
  /bin/poweroff -f
}

/bin/test -e /sys/devices/virtual/video4linux/video42/name || {
  echo DRIVER_AGENT_VIDEO42_SYSFS_MISSING
  /bin/ls /sys/devices/virtual/video4linux || true
  /bin/dmesg | /bin/tail -80 || true
  /bin/poweroff -f
}

/bin/cat /sys/devices/virtual/video4linux/video42/name | /bin/grep DriverAgent || {
  echo DRIVER_AGENT_VIDEO42_NAME_MISMATCH
  /bin/cat /sys/devices/virtual/video4linux/video42/name || true
  /bin/poweroff -f
}

/bin/test -e /dev/video42 || {
  echo DRIVER_AGENT_DEV_VIDEO42_MISSING
  /bin/ls /dev | /bin/head -80 || true
  /bin/poweroff -f
}

if command -v driver_agent_unload_module >/dev/null 2>&1; then
  driver_agent_unload_module || {
    echo DRIVER_AGENT_RMMOD_FAIL
    /bin/dmesg | /bin/tail -80 || true
    /bin/poweroff -f
  }
else
  /bin/rmmod "$DRIVER_AGENT_MODULE_NAME" || {
    echo DRIVER_AGENT_RMMOD_FAIL
    /bin/dmesg | /bin/tail -80 || true
    /bin/poweroff -f
  }
fi

echo DRIVER_AGENT_QEMU_SMOKE_PASS
/bin/poweroff -f
