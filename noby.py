#!/usr/bin/env python3
import os
import argparse
import subprocess
import distutils.util
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
            self.build_commands.append((cmd, args))

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

class ImageStorage():

    def __init__(self, runtime):
        self.runtime = Path(runtime)
        if not self.runtime.exists():
            raise FileNotFoundError(f"Runtime dir {self.runtime} does not exist")

        self.images = {}
        self._scan()

    def _scan(self):
        for image in self.runtime.iterdir():
            name = image.name
            self.images[name] = attrs = {}


            for attr in os.listxattr(image):
                if not attr.startswith("user."):
                    continue
                val = os.getxattr(image, attr)
                val = val.decode()
                key = attr[5:]
                attrs[key] = val

    def find_children(self, parent_hash):
        for image, attrs in self.images.items():
            if attrs.get("parent_hash") == parent_hash:
                yield image

    def find_by_name(self, name):
        for image, attrs in self.images.items():
            if attrs.get("name") == name:
                yield image

    def find_last_build_by_name(self, name):
        image_hash = list(self.find_by_name(name))
        if not image_hash:
            return None
        image_hash = image_hash[0]

        last_hash = image_hash
        hash_tree = []
        while True:
            childs = list(self.find_children(last_hash))
            if not childs:
                break
            for child in childs:
                hash_tree.append(child)
                last_hash = child

        if not hash_tree:
            return last_hash

        for image_hash in hash_tree[::-1]:
            if not image_hash.endswith("-init"):
                return image_hash


def btrfs_subvol_create(path):
    subprocess.run(("btrfs", "subvolume", "create", path),
        check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

def btrfs_subvol_delete(path):
    subprocess.run(("btrfs", "subvolume", "delete", path),
        check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

def btrfs_subvol_snapshot(src, dest, *, readonly=False):
    cmd = ("btrfs", "subvolume", "snapshot", src, dest)
    if readonly:
        cmd = ("btrfs", "subvolume", "snapshot", "-r", src, dest)
    subprocess.run(("btrfs", "subvolume", "snapshot", src, dest),
        check=True,
        #stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def build(args):
    context = Path(args.path).resolve()
    dockerfile = Path(args.file)
    if not dockerfile.is_absolute():
        dockerfile = context / dockerfile
    dockerfile = dockerfile.resolve()
    if not dockerfile.is_file():
        raise FileNotFoundError(f"{dockerfile} does not exist")

    runtime = Path(args.runtime).resolve()

    r = ImageStorage(runtime)

    df = DockerfileParser(dockerfile)
    if not df.build_commands:
        print("Nothing to do")
        return

    build_hash = sha256()
    if df.from_image == "scratch":
        parent_hash = ""
    else:
        parent_hash = r.find_last_build_by_name(df.from_image)
        if not parent_hash:
            raise FileNotFoundError(f"Image with name {df.from_image} not found")

    total_build_steps = len(df.build_commands)
    build_hashes = []

    if not args.no_cache:
        full_build_hash = sha256()
        for cmd, cmdargs in df.build_commands:
            full_build_hash.update(cmdargs.encode())

        if (runtime / full_build_hash.hexdigest()).exists():
            print(f"==> Already built {full_build_hash.hexdigest()[:16]}")
            return

    for current_build_step, (cmd, cmdargs) in enumerate(df.build_commands):
        current_build_step += 1
        build_hash.update(cmdargs.encode())
        args_hash = build_hash.hexdigest()
        build_hashes.append(args_hash)
        print(f"==> Building step {current_build_step}/{total_build_steps} {args_hash[:16]}")

        target = runtime / (args_hash+"-init")
        final_target = runtime / args_hash
        host_env = {
            "TARGET": target,
            "CONTEXT": context
        }
        host_env.update(df.env)

        ## parent image checks
        if final_target.exists():
            if args.no_cache:
                btrfs_subvol_delete(final_target)
            else:
                previous_parent_hash = ""
                try:
                    previous_parent_hash = os.getxattr(final_target, b"user.parent_hash").decode()
                except:
                    pass
                if parent_hash and parent_hash != previous_parent_hash:
                    print("  -> parent image hash changed")
                    btrfs_subvol_delete(final_target)
                else:
                    print("  -> Using cached image")
                    parent_hash = args_hash
                    continue

        if target.exists():
            print("  -> Deleting incomplete image")
            btrfs_subvol_delete(target)

        if parent_hash:
            btrfs_subvol_snapshot(runtime / parent_hash, target)
        else:
            btrfs_subvol_create(target)

        ## Run build step
        if cmd == "host":
            print(f'  -> HOST {cmdargs}')
            subprocess.run(cmdargs, cwd=context, check=True, shell=True, env=host_env)
        elif cmd == "run":
            print(f'  -> RUN {cmdargs}')
            nspawn_cmd = ['systemd-nspawn', '--quiet']
            for key, val in df.env.items():
                nspawn_cmd.extend(('--setenv',f'{key}={val}'))
            nspawn_cmd.extend(('-D', target, '/bin/sh', '-c', cmdargs))
            subprocess.run(nspawn_cmd, cwd=target, check=True, shell=False, env=df.env)

        ## Seal build image
        os.setxattr(target, b"user.parent_hash", parent_hash.encode())
        for attr in ("user.cmd.host", "user.cmd.run"):
            try:
                os.removexattr(target, attr.encode())
            except:
                pass
        os.setxattr(target, f"user.cmd.{cmd}".encode(), cmdargs.encode())

        if args.tag:
            os.setxattr(target, b"user.name", args.tag.encode())

        btrfs_subvol_snapshot(target, final_target, readonly=True)
        btrfs_subvol_delete(target)
        parent_hash = args_hash

    if args.rm:
        print("==> Cleanup")
        for build_hash in build_hashes[:-1]:
            target = runtime / build_hash
            if target.exists():
                print(f"  -> Remove intermediate image {build_hash[:16]}")
                btrfs_subvol_delete(target)

    print(f"==> Successfully built {parent_hash[:16]}")


def strtobool(x):
    return bool(distutils.util.strtobool(x))


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
    build_parser.add_argument('--no-cache',
        action='store',
        default=False,
        type=strtobool,
        metavar='{true, false}',
        help="Do not use cached images (Default false)")
    build_parser.add_argument('--rm',
        action='store',
        default=True,
        type=strtobool,
        metavar='{true, false}',
        help="Remove intermediate images (Default true)")
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
