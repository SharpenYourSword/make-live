#!/usr/bin/env python3

import os
import math
from glob import glob
from logging import getLogger
import logging
import shutil
import parted


def check_call(args):
    import sys
    from subprocess import check_call

    return check_call(args, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)


class _ConsoleHandler(logging.StreamHandler):
    def __init__(self):
        super().__init__()
        self.setFormatter(
            logging.Formatter('{levelname} - {message}', style='{')
        )


class DiskPath:
    def __init__(self, id_alias, dev_path):
        self.id_alias = id_alias
        self.dev_path = dev_path

    def __repr__(self):
        dname = '???'
        if self.id_alias:
            dname = os.path.basename(self.id_alias)
        return '{} ({})'.format(dname, self.dev_path)


class LibremDiskDevice(object):

    def __init__(self, disk):
        """
        Initialize the ExampleDevice object.
        """
        self.path = disk.id_alias
        self.logger = getLogger(__name__)

    @property
    def partition_names(self):
        """
        @return:    A list of partition device names on the block device.
        @rtype:     str
        """
        names = glob('{}[0-9]*'.format(self.path))
        self.logger.debug('has partitions %s', names)
        return names

    def _new_partition(self, device, disk, start, length, set_boot=False):
        geometry = parted.Geometry(device=device, start=start,
                                   length=length)

        self.logger.debug('created %s', geometry)
        filesystem = parted.FileSystem(type='ext4', geometry=geometry)
        self.logger.debug('created %s', filesystem)
        partition = parted.Partition(disk=disk, type=parted.PARTITION_NORMAL,
                                     fs=filesystem, geometry=geometry)
        self.logger.debug('created %s', partition)
        disk.addPartition(partition=partition,
                          constraint=device.optimalAlignedConstraint)
        if set_boot:
            partition.setFlag(parted.PARTITION_BOOT)

        return partition

    def partition_primary_disk(self):
        """
        Create a partition table on the block device for the installer
        image (rescue disk).
        """
        self.logger.info('Creating partitions')
        device = parted.getDevice(self.path)
        self.logger.debug('created %s', device)
        disk = parted.freshDisk(device, 'msdos')
        self.logger.debug('created %s', disk)

        # create the rescue disk partition - the size has been chosen to d-i's liking, so
        # it doesn't attempt to truncate or override the partition
        partition_size = round(2099249152 / (device.sectorSize)) # 2 GB
        self._new_partition(device, disk, 2048, partition_size, True) # 2048 padding is required to make d-i not override the rescue partition

        disk.commit()

        # wait for device nodes
        check_call(['udevadm', 'settle'])

        # create file system and labels
        check_call(['mkfs.ext4', '-F', self.path + '-part1'])
        check_call(['e2label', self.path + '-part1', 'rescue'])

    def partition_secondary_disk(self):
        """
        Format the whole disk for immediate use.
        TODO: We maybe want to tell d-i to encrypt additional partitions as well,
        if they are present.
        """
        self.logger.info('Creating partition on additional disk...')
        device = parted.getDevice(self.path)
        self.logger.debug('created %s', device)
        disk = parted.freshDisk(device, 'msdos')
        self.logger.debug('created %s', disk)

        self._new_partition(device, disk, start=2048, length=device.getLength() - 2048)
        disk.commit()

        # wait for device nodes
        check_call(['udevadm', 'settle'])

        # create file system and labels
        check_call(['mkfs.ext4', '-F', self.path + '-part1'])

    def wipe_dev(self, dev_path):
        """
        Wipe a device (partition or otherwise) of meta-data, be it file system,
        LVM, etc.

        @param dev_path:    Device path of the partition to be wiped.
        @type dev_path:     str
        """
        self.logger.debug('wiping %s', dev_path)
        with open(dev_path, 'wb') as p:
            p.write(bytearray(1024))

    def wipe(self):
        """
        Wipe the block device of meta-data, be it file system, LVM, etc.

        This is not intended to be secure, but rather to ensure that
        auto-discovery tools don't recognize anything here.
        """
        self.logger.info('Wiping partitions and other meta-data')
        for partition in self.partition_names:
            self.wipe_dev(partition)
        self.wipe_dev(self.path)


def configure_di_preseed(template_fname, dest_fname, target_disk):
    """
    Replace variables in a debian-installer preseed file and safe the result
    under a new name.
    """

    contents = None
    with open(template_fname, 'r') as f:
        contents = f.read()

    contents = contents.replace('%TARGET_DISK%', target_disk)

    with open(dest_fname, 'w') as f:
        f.write(contents)


def pureos_oem_setup(guess_fastest_system_disk=False):
    OEM_DATA_PATH = '/var/lib/pureos-oem/'
    logger = getLogger(__name__)

    # find our main hard disk
    all_disk_paths = glob('/dev/disk/by-id/*')

    # we use a dictionary for deduplication
    local_disks_map = {}
    for d in all_disk_paths:
        # exclude USB devices
        if d.startswith('/dev/disk/by-id/usb-'):
            continue
        # exclude partitions
        if '-part' in d:
            continue

        # resolve alias links to direct /dev nodes
        # we need the real path, as sometimes udev does create different names
        # when running in d-i
        dev_path = os.path.realpath(d)

        # exclude optical disks
        if dev_path.startswith('/dev/sr'):
            continue

        disk = DiskPath(id_alias=d, dev_path=dev_path)
        local_disks_map[dev_path] = disk

    if not local_disks_map:
        logger.error('No hard disk found on this system!')
        return 1

    # get a (deduplicated) list of disks
    local_disks = list(local_disks_map.values())

    # urgh... - there are better ways to detect whether a disk is an SSD,
    # but none of them worked reliably enough.
    # So we add this hack here (which we hopefully can remove at some point)
    primary_disk = local_disks[0]
    if guess_fastest_system_disk:
        for disk in local_disks:
            if '_ssd_' in disk.id_alias.lower():
                primary_disk = disk
            if 'nvme' in disk.id_alias.lower():
                primary_disk = disk
                break
    else:
        # try to determine which disk is first by BIOS boot order (this is also just a lucky guess)
        pdisks = sorted(glob('/dev/disk/by-path/*'))

        # pdisks also contains unsuitable disks, like the USB drive we are installing from, so
        # we need to go through the whole list.
        # a very inefficient and sometimes ineffective way to guess the first disk name...
        for pdisk in pdisks:
            first_disk = os.path.realpath(pdisk)
            disk_found = False
            for disk in local_disks:
                if disk.dev_path == first_disk:
                    primary_disk = disk
                    disk_found = True
                    break
            if disk_found:
                break

    logger.info('Found disks: {}'.format(str([d.id_alias for d in local_disks])))
    logger.info('Automatically determined primary disk: {}'.format(primary_disk))

    # allow manual disk selection if we have multiple disks installed
    manual_disk_selection = False
    if not guess_fastest_system_disk:
        if len(local_disks) > 1:
            manual_disk_selection_str = input('Select primary (install target) disk manually? [y/N]')
            manual_disk_selection = manual_disk_selection_str.strip().lower() == 'y'
    if manual_disk_selection:
        print('')
        print('Disks found:')
        for i, disk in enumerate(local_disks):
            print('[{}] {}'.format(i, disk))
        print('')

        while True:
            disk_index_str = input('Primary disk number [int]:')
            try:
                disk_index = int(disk_index_str)
            except (ValueError, TypeError):
                logger.error('Expected an integer')
                continue
            if disk_index >= len(local_disks):
                logger.error('Expected an integer between 0 and {}'.format(len(local_disks)-1))
                continue
            primary_disk = local_disks[disk_index]
            break

        # the final primary disk
        logger.info('User defined primary disk: {}'.format(primary_disk))

    # create the new partition and format it
    logger.info('Partitioning primary disk...')
    libremhdd = LibremDiskDevice(primary_disk)
    libremhdd.wipe()
    libremhdd.partition_primary_disk()

    if len(local_disks) > 1:
        for sdisk in local_disks:
            if sdisk == primary_disk:
                continue
            logger.info('Partitioning secondary disk "{}"...'.format(sdisk))
            extrahdd = LibremDiskDevice(sdisk)
            extrahdd.wipe()
            extrahdd.partition_secondary_disk()

    # mount the setup disk
    logger.info('Mounting main disk...')
    target = os.path.join(OEM_DATA_PATH, 'target')
    try:
        os.makedirs(target)
    except:
        pass
    check_call(['mount', primary_disk.id_alias + '-part1', target])

    # copy PureOS image files and d-i
    logger.info('Copying PureOS install files...')
    shutil.copy(os.path.join(OEM_DATA_PATH, 'pureos.iso'), target)
    shutil.copy(os.path.join(OEM_DATA_PATH, 'initrd.gz'), target)
    shutil.copy(os.path.join(OEM_DATA_PATH, 'vmlinuz'), target)

    # configure & install preseed
    configure_di_preseed(os.path.join(OEM_DATA_PATH, 'di-preseed.cfg.in'),
                         os.path.join(target, 'di-preseed.cfg'),
                         target_disk=primary_disk.dev_path)

    # set up GRUB
    logger.info('Creating GRUB configuration...')
    boot_dir = os.path.join(target, 'boot')
    grub_dir = os.path.join(boot_dir, 'grub')
    try:
        os.makedirs(grub_dir)
    except:
        pass
    shutil.copy(os.path.join(OEM_DATA_PATH, 'grub', 'grub.cfg'), grub_dir)
    shutil.copy(os.path.join(OEM_DATA_PATH, 'grub', 'loopback.cfg'), grub_dir)

    logger.info('Installing GRUB...')
    check_call(['grub-install', primary_disk.dev_path, '--boot-directory=%s' % (boot_dir)])

    check_call(['umount', target])
    logger.info('Done.')

    shutdown = input('Shutdown now? [Y/n]')
    if not shutdown.strip() or shutdown.lower() == 'y':
        check_call(['systemctl', 'poweroff'])

    return 0


if __name__ == '__main__':
    import sys

    print('')
    print('Do you really want to continue installing the OEM image?')
    run_oem = input('/!\ THIS WILL ERASE THE CONTENTS OF ALL DISKS FOUND IN THIS DEVICE [Y/n]')
    if run_oem.strip().lower() != 'y' and run_oem.strip():
        print('Installation cancelled. Rebooting.')
        check_call(['systemctl', 'reboot'])
        sys.exit(0)

    # Set up a logger for nice visibility.
    logger = getLogger(__name__)

    if os.environ.get('DEBUG'):
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)
    logger.addHandler(_ConsoleHandler())

    print('')
    guess_system_disk_str = input('Use heuristics to determine the fastest disk as system disk? [y/N]')
    guess_system_disk = guess_system_disk_str.strip().lower() == 'y'

    r = pureos_oem_setup(guess_fastest_system_disk=guess_system_disk)
    sys.exit(r)
