#!/usr/bin/env python3
import os
import shlex
import shutil
import argparse
import subprocess
import distutils.util
from hashlib import sha256
from pathlib import Path
from pprint import pprint


__version__ = "0.2.1"

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

        elif cmd in ("host", "run", "copy"):
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
            raise FileNotFoundError("Runtime dir {} does not exist".format(self.runtime))

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
                yield image, attrs

    def find_by_name(self, name):
        for image, attrs in self.images.items():
            if attrs.get("name") == name:
                yield image, attrs

    def find_last_build_by_name(self, name):
        image_hashes = list(self.find_by_name(name))
        if not image_hashes:
            return None

        image_hash = None
        for h, a in image_hashes:
            if h.endswith("-init"):
                continue
            image_hash = h

        if not image_hash:
            return None

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
    subprocess.run(cmd,
        check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

def newest_file(context, cmdargs):
    *srcs, dest = shlex.split(cmdargs)
    mtime = 0
    for src in srcs:
        src = context / src
        for file in src.glob('**/*'):
            fmtime = file.stat().st_mtime
            if fmtime > mtime:
                mtime = fmtime
    return mtime


def build(args):
    context = Path(args.path).resolve()
    dockerfile = Path(args.file)
    if not dockerfile.is_absolute():
        dockerfile = context / dockerfile
    dockerfile = dockerfile.resolve()
    if not dockerfile.is_file():
        raise FileNotFoundError("{} does not exist".format(dockerfile))

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
            raise FileNotFoundError("Image with name {} not found".format(df.from_image))
        print("Using parent image {}".format(parent_hash[:16]))

    total_build_steps = len(df.build_commands)
    build_hashes = []

    if not args.no_cache:
        full_build_hash = sha256()
        for cmd, cmdargs in df.build_commands:
            full_build_hash.update(cmd.encode())
            full_build_hash.update(cmdargs.encode())

        if (runtime / full_build_hash.hexdigest()).exists():
            print("==> Already built {}".format(full_build_hash.hexdigest()[:16]))
            return

    for current_build_step, (cmd, cmdargs) in enumerate(df.build_commands):
        current_build_step += 1
        build_hash.update(cmd.encode())
        build_hash.update(cmdargs.encode())
        if cmd == "copy":
            mtime = newest_file(context, cmdargs)
            build_hash.update(int(mtime).to_bytes(4, 'big'))
        args_hash = build_hash.hexdigest()
        build_hashes.append(args_hash)
        print("==> Building step {}/{} {}".format(current_build_step, total_build_steps, args_hash[:16]))

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
            try:
                os.removexattr(target, b"user.name")
            except:
                pass
        else:
            btrfs_subvol_create(target)

        ## Run build step
        if cmd == "host":
            print('  -> HOST {}'.format(cmdargs))
            subprocess.run(cmdargs, cwd=context, check=True, shell=True, env=host_env)

        elif cmd == "run":
            print('  -> RUN {}'.format(cmdargs))
            nspawn_cmd = ['systemd-nspawn', '--quiet']
            for key, val in df.env.items():
                nspawn_cmd.extend(('--setenv','{}={}'.format(key, val)))
            nspawn_cmd.extend(('-D', target, '/bin/sh', '-c', cmdargs))
            subprocess.run(nspawn_cmd, cwd=target, check=True, shell=False, env=df.env)

        elif cmd == "copy":
            print("  -> COPY {}".format(cmdargs))
            *srcs, dest = shlex.split(cmdargs)
            if Path(dest).is_absolute():
                dest = target / dest[1:]
            else:
                dest = target / dest
            if len(srcs) > 1 and not dest.is_dir():
                raise NotADirectoryError("Destination must be a directory")
            cmd = ['cp', '-rv']
            cmd.extend(srcs)
            cmd.append(str(dest))
            subprocess.run(cmd, cwd=context, check=True, shell=False, env=host_env)

        ## Seal build image
        os.setxattr(target, b"user.parent_hash", parent_hash.encode())
        for attr in ("user.cmd.host", "user.cmd.run"):
            try:
                os.removexattr(target, attr.encode())
            except:
                pass
        os.setxattr(target, "user.cmd.{}".format(cmd).encode(), cmdargs.encode())

        if args.tag:
            os.setxattr(target, b"user.name", args.tag.encode())
        else:
            try:
                os.removexattr(target, b"user.name")
            except:
                pass

        btrfs_subvol_snapshot(target, final_target, readonly=True)
        btrfs_subvol_delete(target)
        parent_hash = args_hash

    if args.rm:
        print("==> Cleanup")
        for build_hash in build_hashes[:-1]:
            target = runtime / build_hash
            if target.exists():
                print("  -> Remove intermediate image {}".format(build_hash[:16]))
                btrfs_subvol_delete(target)

    print("==> Successfully built {}".format(parent_hash[:16]))


def export(args):
    runtime = Path(args.runtime).resolve()
    r = ImageStorage(runtime)
    image = r.find_last_build_by_name(args.container)

    if not image:
        raise Exception('Can\'t find container image with name "{}"'.format(args.container))
    print('==> Exporting image "{}" with hash {}'.format(args.container, image[:16]))

    if args.type == "squashfs":
        if not args.output:
            raise Exception("--output argument missing. Squashfs can't be written to STDOUT")
        print("  -> Building squashfs image")
        subprocess.run(('mksquashfs', runtime / image, args.output, '-no-xattrs', '-noappend'))
    else:
        raise NotImplementedError("Can't yet export container image with type {}".format(args.type))


def run(args):
    raise NotImplementedError("Can't yet run commands inside the container")

def strtobool(x):
    return bool(distutils.util.strtobool(x))


def parseargs():
    parser = argparse.ArgumentParser(description='Mini docker like image builder')
    parser.add_argument(
        '--runtime', action='store',
        help='Directory where runtime files are stored. (Default /var/lib/noby)',
        default=os.environ.get('NOBY_RUNTIME','/var/lib/noby'))
    parser.add_argument('--version', action='version', version=__version__)
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

    # Export parser
    export_parser = subparsers.add_parser(
        'export', help="Export image"
    )
    export_parser.add_argument('--output', '-o',
        action='store',
        help="Write to a file, instead of STDOUT")
    export_parser.add_argument('--type',
        action='store',
        choices=('tar.gz', 'squashfs'),
        default='squashfs',
        help="Export image type (Default squashfs)"
    )
    export_parser.add_argument('container',
        action='store',
        metavar='CONTAINER',
        help='Name of the conainer image to export'
    )
    export_parser.set_defaults(func=export)

    # Run parser
    run_parser = subparsers.add_parser(
        'run', help="Run image"
    )
    run_parser.add_argument('container',
        action='store',
        metavar='CONTAINER',
        help='Name or hash of the container image to run'
    )
    run_parser.add_argument('command',
        action='store',
        nargs='?',
        metavar='COMMAND',
        default='/bin/bash',
        help='Command to run inside the container (Default /bin/bash)'
    )
    run_parser.set_defaults(func=run)

    return parser.parse_args()

def main():
    args = parseargs()

    runtime = Path(args.runtime).resolve()
    runtime.mkdir(parents=True, exist_ok=True)

    if hasattr(args, "func"):
        args.func(args)
    else:
        print("No command defined in argparser")
        exit(1)

if __name__ == "__main__":
    main()
