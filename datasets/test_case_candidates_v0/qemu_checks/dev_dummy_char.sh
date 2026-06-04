/bin/insmod "$DRIVER_AGENT_MODULE_KO" || { echo DRIVER_AGENT_INSMOD_FAIL; /bin/dmesg | /bin/tail -80; /bin/poweroff -f; }
if [ ! -e /dev/dummy_char ]; then
  echo DRIVER_AGENT_DEVICE_NODE_MISSING /dev/dummy_char
  /bin/ls -l /dev /sys/class/dummy_char_class 2>/dev/null || true
  /bin/poweroff -f
fi
/bin/rmmod "$DRIVER_AGENT_MODULE_NAME" || { echo DRIVER_AGENT_RMMOD_FAIL; /bin/poweroff -f; }
echo DRIVER_AGENT_QEMU_SMOKE_PASS
/bin/poweroff -f
