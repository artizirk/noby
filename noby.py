#!/usr/bin/env python3
import os
import argparse
import subprocess
from hashlib import sha256
from pathlib import Path
from pprint import pprint


class DockerfileParser():

    def __init__(self, dockerfile):
        self.lines = []
        self.env = {}
        self.from_image = None
        self.build_commands = []
        self._parse_file(dockerfile)

    def _populate_env(self, rawenv):
        env_name, *value = rawenv.split("=")  # replace with shlex maybe
        self.env[env_name] = "=".join(value)

    def _populate_vars(self, cmd, args, raw):
        if not cmd:
            return
        cmd = cmd.lower()
        if cmd == "env":
            self._populate_env(args)

        elif cmd in ("host", "run"):
            self.build_commands.append((cmd, sha256(args.encode()).hexdigest(), args))

        elif cmd == "from":
            self.from_image = args

    def _parse_file(self, dockerfile):
        with open(dockerfile) as f:
            for rawline in self._yield_lines(f.readlines()):
                line = self._line_parser(rawline)
                self.lines.append(line)
                self._populate_vars(*line)

    def _line_parser(self, string):
        trimmed_string = string.strip()
        if trimmed_string.startswith("#"):
            # We have a comment
            return "#", trimmed_string.lstrip("#").strip(), string
        elif not trimmed_string:
            # Blank line
            return "", "", string
        else:
            # First instance of line continuation, so we have a cmd
            cmd, args = trimmed_string.split(" ", 1)
            return cmd, args, string

    def _yield_lines(self, iterable):
        current_line = []
        # Inspired by https://github.com/mpapierski/dockerfile-parser
        for raw_line in iterable:
            string = raw_line.strip()
            if string.startswith("#") or not string:
                yield raw_line.rstrip()
                continue
            current_line.append(string)
            if not string.endswith("\\"):
                yield "\n".join(current_line)
                current_line = []


def btrfs_subvol_create(path):
    subprocess.run(("btrfs", "subvolume", "create", path), check=True)

def btrfs_subvol_delete(path):
    subprocess.run(("btrfs", "subvolume", "delete", path), check=True)

def btrfs_subvol_snapshot(src, dest, *, readonly=False):
    if readonly:
        subprocess.run(("btrfs", "subvolume", "snapshot", "-r", src, dest), check=True)
    else:
        subprocess.run(("btrfs", "subvolume", "snapshot", src, dest), check=True)


def build(args):
    context = Path(args.path).resolve()
    dockerfile = Path(args.file)
    if not dockerfile.is_absolute():
        dockerfile = context / dockerfile
    dockerfile = dockerfile.resolve()
    if not dockerfile.is_file():
        raise FileNotFoundError(f"{dockerfile} does not exist")

    runtime = Path(args.runtime).resolve()

    df = DockerfileParser(dockerfile)
    if not df.build_commands:
        print("Nothing to do")
        return

    if df.from_image == "scratch":
        parrent_hash = ""
    else:
        raise NotImplemented("Can't yet locate parrent_hash")

    for cmd, args_hash, cmdargs in df.build_commands:
        print(f"==> Building {args_hash[:16]}")

        target = runtime / (args_hash+"-init")
        final_target = runtime / args_hash
        host_env = {
            "TARGET": target,
            "CONTEXT": context
        }
        host_env.update(df.env)

        ## Parrent image checks
        if final_target.exists():
            previous_parrent_hash = ""
            try:
                previous_parrent_hash = os.getxattr(final_target, b"user.parrent_hash").decode()
            except:
                pass
            if parrent_hash and parrent_hash != previous_parrent_hash:
                print("  -> Parrent image hash changed")
                btrfs_subvol_delete(final_target)
            else:
                print("  -> Using cached image")
                parrent_hash = args_hash
                continue

        if target.exists():
            print("  -> Deleting incomplete image")
            btrfs_subvol_delete(target)

        if parrent_hash:
            btrfs_subvol_snapshot(runtime / parrent_hash, target)
        else:
            btrfs_subvol_create(target)

        ## Run build step
        if cmd == "host":
            print(f"  -> Running on host \"{cmdargs}\"")
            subprocess.run(cmdargs, cwd=context, check=True, shell=True, env=host_env)
        elif cmd == "run":
            print(f"  -> Running in container \"{cmdargs}\"")
            subprocess.run(['systemd-nspawn', '-D', target, '/bin/sh', '-c', cmdargs], cwd=target, check=True, shell=False, env=df.env)

        ## Seal build image
        os.setxattr(target, b"user.parrent_hash", parrent_hash.encode())
        for attr in ("user.cmd.host", "user.cmd.run"):
            try:
                os.removexattr(target, attr.encode())
            except:
                pass
        os.setxattr(target, f"user.cmd.{cmd}".encode(), cmdargs.encode())
        btrfs_subvol_snapshot(target, final_target, readonly=True)
        btrfs_subvol_delete(targtet)
        parrent_hash = args_hash


def parseargs():
    parser = argparse.ArgumentParser(description='Mini docker like image builder')
    parser.add_argument(
        '--runtime', action='store',
        help='Directory where runtime files are stored. (Default /var/lib/noby)',
        default='/var/lib/noby')
    subparsers = parser.add_subparsers(dest='command', metavar='COMMAND', help='commands')
    subparsers.required = True

    # Image builder argument parser
    build_parser = subparsers.add_parser(
        'build', help='Build dockerfile')
    build_parser.add_argument('--file', '-f',
        action='store',
        help="Name of the dockerfile. (Default 'PATH/Dockerfile')",
        default="Dockerfile")
    build_parser.add_argument('--tag', '-t',
        action='store',
        help="Name and optionally a tag in the 'name:tag' format")
    build_parser.add_argument('path',
        action='store',
        metavar='PATH',
        help='context for the build')
    build_parser.set_defaults(func=build)

    export_parser = subparsers.add_parser(
        'export', help="Export image"
    )

    return parser.parse_args()

def main():
    args = parseargs()

    runtime = Path(args.runtime).resolve()
    runtime.mkdir(parents=True, exist_ok=True)

    if hasattr(args, "func"):
        args.func(args)
    else:
        exit(1)

if __name__ == "__main__":
    main()
