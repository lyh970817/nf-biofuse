# Written by the nf-core/gwas team and released under the MIT license.
"""The mount daemon: biofuse's own ``mount-plink`` command, run with ``auto_unmount`` set.

Adapter for biofuse 0.1.0, whose ``fuse_adapter.mount`` builds its option set as
``pyfuse3.default_options`` plus ``fsname=`` and ``ro`` and exposes no CLI or API surface for further
mount options. Without it the launcher materially breaks: ``auto_unmount`` makes the kernel drop the
mount when the daemon exits, which is the only teardown that survives a SIGKILL, a cgroup kill or a
session teardown -- exactly where no userspace cleanup runs. A mount that outlives its daemon blocks
every process that touches it in uninterruptible sleep, and nothing short of a kernel connection abort
releases them.

Retire this module, and start ``biofuse mount-plink`` directly, once biofuse exposes mount options.

``SIGUSR1`` dumps every thread's Python stack to stderr (the daemon log) through ``faulthandler``; it
needs no ptrace, which matters because the daemon runs in its own session.

Usage is exactly biofuse's::

    python -m nf_core_gwas_biofuse.daemon mount-plink STORE.vcz MOUNTDIR --basename cohort --max-alleles 2
"""

from __future__ import annotations

import faulthandler
import signal
import sys

import pyfuse3

faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)

_pyfuse3_init = pyfuse3.init


def _init_with_auto_unmount(operations, mountpoint, options):
    mount_options = set(options)
    mount_options.add("auto_unmount")
    return _pyfuse3_init(operations, mountpoint, mount_options)


pyfuse3.init = _init_with_auto_unmount


def main(argv: list[str] | None = None) -> int:
    from biofuse.cli import biofuse_main  # imported after the patch so the adapter is in place

    return biofuse_main(args=argv, prog_name="biofuse")


if __name__ == "__main__":
    sys.exit(main())
