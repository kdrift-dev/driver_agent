/bin/insmod "$DRIVER_AGENT_MODULE_KO" || { echo DRIVER_AGENT_INSMOD_FAIL; /bin/dmesg | /bin/tail -80; /bin/poweroff -f; }
if [ ! -e /dev/lkmc_character_device_create_dev ]; then
  echo DRIVER_AGENT_DEVICE_NODE_MISSING /dev/lkmc_character_device_create_dev
  /bin/ls -l /dev /sys/class/lkmc_character_device_create_sys 2>/dev/null || true
  /bin/poweroff -f
fi
/bin/cat /dev/lkmc_character_device_create_dev >/tmp/driver_agent_read.out 2>/tmp/driver_agent_read.err || true
/bin/rmmod "$DRIVER_AGENT_MODULE_NAME" || { echo DRIVER_AGENT_RMMOD_FAIL; /bin/poweroff -f; }
echo DRIVER_AGENT_QEMU_SMOKE_PASS
/bin/poweroff -f
