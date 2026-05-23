# OOT QEMU Case: class_create char driver

This controlled out-of-tree case uses old Linux `v6.1` driver API usage:

```c
class_create(THIS_MODULE, OOT_CLASS_DEMO_NAME)
```

The target Linux `v7.0-rc1` API is:

```c
struct class *class_create(const char *name);
```

The old source should build against `v6.1`, fail against `v7.0-rc1`, and pass after adapting the call to `class_create(OOT_CLASS_DEMO_NAME)` while preserving cleanup paths.
