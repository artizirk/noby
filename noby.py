#!/usr/bin/env python3
import os
import argparse
from hashlib import sha256
from pathlib import Path
from pprint import pprint
from subprocess import run

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
        host_env = {"TARGET": target}
        host_env.update(df.env)

        if final_target.exists():
            print("  -> Using cached image")
            parrent_hash = args_hash
            continue

        if target.exists():
            print("  -> Deleting incomplete image")
            run(("btrfs", "subvolume", "delete", target), check=True)

        if parrent_hash:
            run(("btrfs", "subvolume", "snapshot", runtime / parrent_hash, target), check=True)
        else:
            run(("btrfs", "subvolume", "create", target), check=True)

        if cmd == "host":
            print(f"  -> Running on host \"{cmdargs}\"")
            run(cmdargs, cwd=target, check=True, shell=True, env=host_env)
        elif cmd == "run":
            print(f"  -> Running in container \"{cmdargs}\"")
            run(['systemd-nspawn', '-D', target, '/bin/sh', '-c', cmdargs], cwd=target, check=True, shell=False, env=df.env)

        os.setxattr(target, b"user.parrent_hash", parrent_hash.encode())
        for attr in ("user.cmd.host", "user.cmd.run"):
            try:
                os.removexattr(target, attr.encode())
            except:
                pass
        os.setxattr(target, f"user.cmd.{cmd}".encode(), cmdargs.encode())
        run(("btrfs", "subvolume", "snapshot", "-r", target, final_target), check=True)
        run(("btrfs", "subvolume", "delete", target), check=True)
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
