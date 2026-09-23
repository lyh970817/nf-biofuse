#!/usr/bin/env python3
# Written by the nf-core/gwas team and released under the MIT license.
"""Run nf-core-gwas-biofuse under a real controlling terminal and cancel it the way a person does, so
Ctrl-C cleanup is tested faithfully.

Backgrounding a launcher from a non-interactive shell makes the shell set SIGINT to SIG_IGN, and the
launcher honours an ignored signal, so terminal cancellation has to be tested under a terminal. In
``ctrl-c`` mode the harness types ^C into the terminal and the line discipline delivers SIGINT to the
foreground process group; in ``pgroup`` mode it signals that process group directly.

Usage: pty_signal_test.py LAUNCHER VCZ STATE_ROOT SIGNAL [ctrl-c|pgroup]
"""

import argparse
import fcntl
import os
import pty
import signal
import subprocess
import sys
import termios
import time


def acquire_controlling_terminal():
    # Runs in the child after setsid(): make the PTY on stdin this new session's controlling terminal.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def read_available(master, buffer):
    try:
        chunk = os.read(master, 65536)
    except (BlockingIOError, OSError):
        return buffer
    return buffer + chunk


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("launcher")
    parser.add_argument("vcz")
    parser.add_argument("state_root")
    parser.add_argument("signal_name")
    parser.add_argument("mode", nargs="?", default="ctrl-c", choices=("ctrl-c", "pgroup"))
    args = parser.parse_args()
    signum = getattr(signal, args.signal_name)

    master, slave = pty.openpty()
    process = subprocess.Popen(
        [
            args.launcher,
            "run",
            "--vcz",
            args.vcz,
            "--state-root",
            args.state_root,
            "--record-dir",
            args.state_root,
            "--cohort-id",
            "lifecycle",
            "--genome-build",
            "GRCh37",
            "--ancestry",
            "EUR",
            "--readahead-buffer-size",
            "16M",
            "-v",
            "--",
            "sleep",
            "300",
        ],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        preexec_fn=acquire_controlling_terminal,
    )
    os.close(slave)
    os.set_blocking(master, False)

    buffer = b""
    deadline = time.monotonic() + 120
    while b"mount ready" not in buffer:
        if time.monotonic() > deadline or process.poll() is not None:
            print("launcher never reported the mount ready")
            print(buffer.decode(errors="replace"))
            process.kill()
            return 1
        buffer = read_available(master, buffer)
        time.sleep(0.05)

    print("--- launcher output before signal ---")
    print(buffer.decode(errors="replace").strip())

    time.sleep(0.5)
    if args.mode == "ctrl-c":
        if signum != signal.SIGINT:
            parser.error("ctrl-c mode delivers SIGINT only")
        os.write(master, b"\x03")
    else:
        os.killpg(process.pid, signum)
    print(f"--- sent {args.signal_name} ({args.mode}) ---")

    deadline = time.monotonic() + 60
    while process.poll() is None and time.monotonic() < deadline:
        buffer = read_available(master, buffer)
        time.sleep(0.05)
    buffer = read_available(master, buffer)

    print("--- launcher output after signal ---")
    print(buffer.decode(errors="replace").strip()[-2000:])
    print(f"--- launcher exit status: {process.poll()} ---")
    # A launcher cancelled by a signal ends by that signal, as the shell that started it expects.
    return 0 if process.poll() == -signum else 2


if __name__ == "__main__":
    sys.exit(main())
