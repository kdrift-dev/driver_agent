/bin/insmod "$DRIVER_AGENT_MODULE_KO" || { echo DRIVER_AGENT_INSMOD_FAIL; /bin/poweroff -f; }

if [ ! -d /sys/bus/ldd ]; then
  echo DRIVER_AGENT_LDDBUS_SYSFS_MISSING
  /bin/poweroff -f
fi

if ! cat /sys/bus/ldd/bus_version 2>/dev/null | grep 'Revision'; then
  echo DRIVER_AGENT_BUS_VERSION_MISSING
  /bin/poweroff -f
fi

found_dev_info=0
for f in /sys/devices/*/dev_info /sys/devices/*/*/dev_info /sys/devices/*/*/*/dev_info; do
  if [ -e "$f" ] && cat "$f" | grep 'ldd0'; then
    found_dev_info=1
  fi
done
if [ "$found_dev_info" != 1 ]; then
  echo DRIVER_AGENT_DEV_INFO_MISSING
  /bin/find /sys/devices -name dev_info -print 2>/dev/null
  /bin/poweroff -f
fi

/bin/rmmod "$DRIVER_AGENT_MODULE_NAME" || { echo DRIVER_AGENT_RMMOD_FAIL; /bin/poweroff -f; }
echo DRIVER_AGENT_QEMU_SMOKE_PASS
/bin/poweroff -f
