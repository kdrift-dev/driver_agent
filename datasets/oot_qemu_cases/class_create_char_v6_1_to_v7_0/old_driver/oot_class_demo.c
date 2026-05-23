// SPDX-License-Identifier: GPL-2.0
#include <linux/cdev.h>
#include <linux/device.h>
#include <linux/fs.h>
#include <linux/init.h>
#include <linux/module.h>
#include <linux/uaccess.h>

#define OOT_CLASS_DEMO_NAME "oot_class_demo"

static dev_t demo_devt;
static struct cdev demo_cdev;
static struct class *demo_class;
static char demo_buf[64] = "oot-class-demo\n";

static ssize_t demo_read(struct file *file, char __user *buf, size_t count, loff_t *ppos)
{
	return simple_read_from_buffer(buf, count, ppos, demo_buf, strlen(demo_buf));
}

static ssize_t demo_write(struct file *file, const char __user *buf, size_t count, loff_t *ppos)
{
	size_t len = min(count, sizeof(demo_buf) - 1);

	if (copy_from_user(demo_buf, buf, len))
		return -EFAULT;
	demo_buf[len] = '\0';
	return count;
}

static const struct file_operations demo_fops = {
	.owner = THIS_MODULE,
	.read = demo_read,
	.write = demo_write,
};

static int __init demo_init(void)
{
	int ret;

	ret = alloc_chrdev_region(&demo_devt, 0, 1, OOT_CLASS_DEMO_NAME);
	if (ret)
		return ret;

	cdev_init(&demo_cdev, &demo_fops);
	demo_cdev.owner = THIS_MODULE;
	ret = cdev_add(&demo_cdev, demo_devt, 1);
	if (ret)
		goto err_unregister;

	/* Old kernel API: v6.1 class_create(owner, name). */
	demo_class = class_create(THIS_MODULE, OOT_CLASS_DEMO_NAME);
	if (IS_ERR(demo_class)) {
		ret = PTR_ERR(demo_class);
		goto err_cdev;
	}

	if (IS_ERR(device_create(demo_class, NULL, demo_devt, NULL, OOT_CLASS_DEMO_NAME))) {
		ret = -ENODEV;
		goto err_class;
	}

	pr_info("oot_class_demo loaded\n");
	return 0;

err_class:
	class_destroy(demo_class);
err_cdev:
	cdev_del(&demo_cdev);
err_unregister:
	unregister_chrdev_region(demo_devt, 1);
	return ret;
}

static void __exit demo_exit(void)
{
	device_destroy(demo_class, demo_devt);
	class_destroy(demo_class);
	cdev_del(&demo_cdev);
	unregister_chrdev_region(demo_devt, 1);
	pr_info("oot_class_demo unloaded\n");
}

module_init(demo_init);
module_exit(demo_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Driver-Agent OOT case");
MODULE_DESCRIPTION("Small OOT char driver for class_create API migration");
