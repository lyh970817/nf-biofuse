#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run a command under a real controlling terminal and cancel it the way a person does, so that Ctrl-C
handling is tested faithfully.

The command (Nextflow, in the lifecycle matrix) becomes the foreground process group of a fresh PTY.
Once ``--marker`` exists, the harness types ^C into the terminal and the line discipline delivers SIGINT
to that foreground group, exactly as at an interactive shell. The supervisor the plugin started has left
that session, so the keystroke must not reach it; the plugin has to stop it.

Usage: pty_signal_test.py --marker FILE [--cwd DIR] [--timeout SECONDS] -- COMMAND...
Exit status 0 when the command exited after the keystroke, 1 when it never reached the marker, 2 when it
did not exit within the timeout.
"""

import argparse
import fcntl
import os
import pty
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
    parser.add_argument("--marker", required=True, help="File whose appearance means the run is under way.")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--timeout", type=float, default=180, help="Seconds to wait for the marker, and for exit.")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command

    master, slave = pty.openpty()
    process = subprocess.Popen(
        command,
        cwd=args.cwd,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        preexec_fn=acquire_controlling_terminal,
    )
    os.close(slave)
    os.set_blocking(master, False)

    buffer = b""
    deadline = time.monotonic() + args.timeout
    while not os.path.exists(args.marker):
        if time.monotonic() > deadline or process.poll() is not None:
            print("the command never reached the marker")
            print(buffer.decode(errors="replace")[-4000:])
            process.kill()
            return 1
        buffer = read_available(master, buffer)
        time.sleep(0.05)

    time.sleep(0.5)
    os.write(master, b"\x03")
    print("--- typed ^C ---")

    deadline = time.monotonic() + args.timeout
    while process.poll() is None and time.monotonic() < deadline:
        buffer = read_available(master, buffer)
        time.sleep(0.05)
    buffer = read_available(master, buffer)

    print("--- terminal output ---")
    print(buffer.decode(errors="replace").strip()[-4000:])
    status = process.poll()
    print(f"--- exit status: {status} ---")
    if status is None:
        process.kill()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
