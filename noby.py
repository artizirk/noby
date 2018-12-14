#!/usr/bin/env python3
import os
import sys
import shlex
import shutil
import argparse
import subprocess
import distutils.util
from hashlib import sha256
from pathlib import Path
from pprint import pprint


__version__ = "0.3.2"


class DockerfileParser():

    def __init__(self, dockerfile):
        self.lines = []
        self.env = {}
        self.from_image = None
        self.build_commands = []
        self.build_hashes = []
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
        with dockerfile.open() as f:
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

    def calc_build_hashes(self, parent_hash=None):
        build_hash = sha256()
        if parent_hash:
            build_hash.update(parent_hash.encode())
        for cmd, args in self.build_commands:
            build_hash.update(cmd.encode())
            build_hash.update(args.encode())
            self.build_hashes.append(build_hash.hexdigest())

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

            if not image.exists():
                continue

            for attr in os.listxattr(str(image)):
                if not attr.startswith("user."):
                    continue
                val = os.getxattr(str(image), attr)
                val = val.decode()
                key = attr[5:]
                attrs[key] = val

    def find_children(self, parent_hash):
        for image, attrs in self.images.items():
            if attrs.get("parent_hash") == parent_hash:
                yield image, attrs

    def find_last_build_by_name(self, name):
        link = self.runtime / ("tag-" + str(name))

        if not link.exists():
            return  # Tag does not exist

        image = Path(os.readlink(str(link)))
        if not image.exists():
            return  # Tag points to non existing image

        if image.name.endswith('-init'):
            return  # Tag points to invalid image

        return image.name  # its a valid image


def btrfs_subvol_create(path):
    subprocess.run(("btrfs", "subvolume", "create", str(path)),
        check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

def btrfs_subvol_delete(path):
    subprocess.run(("btrfs", "subvolume", "delete", str(path)),
        check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

def btrfs_subvol_snapshot(src, dest, *, readonly=False):
    cmd = ("btrfs", "subvolume", "snapshot", str(src), str(dest))
    if readonly:
        cmd = ("btrfs", "subvolume", "snapshot", "-r", str(src), str(dest))
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

    #  Locate base image for this dockerfile
    parent_hash = ""
    if df.from_image != "scratch":
        parent_hash = r.find_last_build_by_name(df.from_image)
        if not parent_hash:
            raise FileNotFoundError("Image with name {} not found".format(df.from_image))
        print("Using parent image {}".format(parent_hash[:16]))

    #  Update build hashes based on base image
    df.calc_build_hashes(parent_hash=parent_hash)
    total_build_steps = len(df.build_commands)

    #  Early exit if image is already built
    if not args.no_cache and args.rm:
        if (runtime / df.build_hashes[-1]).exists():
            print("==> Already built {}".format(df.build_hashes[-1][:16]))
            return

    #  Do the build
    for current_build_step, (cmd, cmdargs) in enumerate(df.build_commands):
        build_step_hash = df.build_hashes[current_build_step]

        print("==> Building step {}/{} {}".format(current_build_step + 1, total_build_steps, build_step_hash[:16]))

        target = runtime / (build_step_hash + "-init")
        final_target = runtime / build_step_hash
        host_env = {
            "TARGET": str(target),
            "CONTEXT": str(context)
        }
        host_env.update(df.env)

        ## parent image checks
        if final_target.exists():
            if args.no_cache:
                btrfs_subvol_delete(final_target)
            else:
                previous_parent_hash = ""
                try:
                    previous_parent_hash = os.getxattr(str(final_target), b"user.parent_hash").decode()
                except:
                    pass
                if parent_hash and parent_hash != previous_parent_hash:
                    print("  -> parent image hash changed")
                    btrfs_subvol_delete(final_target)
                else:
                    print("  -> Using cached image")
                    parent_hash = build_step_hash
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
            print('  -> HOST {}'.format(cmdargs))
            subprocess.run(cmdargs, cwd=str(context), check=True, shell=True, env=host_env)

        elif cmd == "run":
            print('  -> RUN {}'.format(cmdargs))
            nspawn_cmd = ['systemd-nspawn', '--quiet']
            for key, val in df.env.items():
                nspawn_cmd.extend(('--setenv', '{}={}'.format(key, val)))
            nspawn_cmd.extend(('--register=no', '-D', str(target), '/bin/sh', '-c', cmdargs))
            subprocess.run(nspawn_cmd, cwd=str(target), check=True, shell=False, env=df.env)

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
            subprocess.run(cmd, cwd=str(context), check=True, shell=False, env=host_env)

        ## Seal build image
        os.setxattr(str(target), b"user.parent_hash", parent_hash.encode())
        for attr in ("user.cmd.host", "user.cmd.run"):
            try:
                os.removexattr(str(target), attr.encode())
            except:
                pass
        os.setxattr(str(target), "user.cmd.{}".format(cmd).encode(), cmdargs.encode())

        btrfs_subvol_snapshot(target, final_target, readonly=True)
        btrfs_subvol_delete(target)

        parent_hash = build_step_hash

    #  After build cleanup
    if args.rm:
        print("==> Cleanup")
        for build_hash in df.build_hashes[:-1]:
            target = runtime / build_hash
            if target.exists():
                print("  -> Remove intermediate image {}".format(build_hash[:16]))
                btrfs_subvol_delete(target)

    print("==> Successfully built {}".format(parent_hash[:16]))

    if args.tag:
        tmp_tag = runtime / ("tag-" + args.tag + "-tmp")
        if tmp_tag.exists():
            os.unlink(str(tmp_tag))
        os.symlink(str(runtime / parent_hash), str(tmp_tag))
        os.replace(str(tmp_tag), str(runtime / ("tag-" + args.tag)))
        print("==> Tagged image {} as {}".format(parent_hash[:16], args.tag))


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
        subprocess.run(('mksquashfs', str(runtime / image), args.output, '-no-xattrs', '-noappend'))
    else:
        raise NotImplementedError("Can't yet export container image with type {}".format(args.type))


def run(args):
    context = Path(args.container).resolve()
    dockerfile = Path(args.file)
    if not dockerfile.is_absolute():
        dockerfile = context / dockerfile
    dockerfile = dockerfile.resolve()
    if not dockerfile.is_file():
        raise FileNotFoundError("{} does not exist".format(dockerfile))

    runtime = Path(args.runtime).resolve()
    r = ImageStorage(runtime)

    df = DockerfileParser(dockerfile)

    #  Locate base image for this Dockerfile
    parent_hash = ""
    if df.from_image != "scratch":
        parent_hash = r.find_last_build_by_name(df.from_image)
        if not parent_hash:
            raise FileNotFoundError("Image with name {} not found".format(df.from_image))
        print("Using parent image {}".format(parent_hash[:16]))

    #  Update build hashes based on base image
    df.calc_build_hashes(parent_hash=parent_hash)

    target = runtime / df.build_hashes[-1]
    if not target.exists():
        raise FileNotFoundError("Image {} not found".format(df.build_hashes[-1]))

    print('  -> RUN {}'.format(args.command))
    nspawn_cmd = ['systemd-nspawn', '--quiet']
    for key, val in df.env.items():
        nspawn_cmd.extend(('--setenv', '{}={}'.format(key, val)))
    if args.rm:
        nspawn_cmd.append('-x')
    if args.volume:
        src_dest = args.volume.split(':')
        if len(src_dest) > 1:
            src = src_dest[0]
            dest = ":".join(src_dest[1:])
        else:
            src = src_dest[0]
            dest = ''
        src = Path(src)
        if not src.exists():
            raise FileNotFoundError("Volume {} does not exist".format(src))
        if not src.is_absolute():
            src = src.resolve()
        if not dest:
            dest = '/' + src.name
        if not dest.startswith('/'):
            dest = '/' + dest
        volume = "{}:{}".format(src, dest)
        nspawn_cmd.append('--bind=' + str(volume))
    nspawn_cmd.extend(('--register=no', '-D', str(target), '/bin/sh', '-c', args.command))
    subprocess.run(nspawn_cmd, cwd=str(target), check=True, shell=False, env=df.env)


def wipe(args):
    runtime = Path(args.runtime).resolve()
    r = ImageStorage(runtime)
    print("==> Removing {} images from runtime store".format(len(r.images)))
    for image in r.images.keys():
        print("  -> Removing {}".format(image[:16]))
        if image.startswith('tag'):
            os.unlink(str(runtime / image))
        else:
            btrfs_subvol_delete(str(runtime / image))


def strtobool(x):
    return bool(distutils.util.strtobool(x))


def parseargs():
    parser = argparse.ArgumentParser(description='Mini docker like image builder')
    parser.add_argument(
        '--runtime', action='store',
        help='Directory where runtime files are stored. (Default NOBY_RUNTIME env variable or /var/lib/noby)',
        default=os.environ.get('NOBY_RUNTIME', '/var/lib/noby'))
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
        default=False,
        type=strtobool,
        metavar='{true, false}',
        help="Remove intermediate images (Default False)")
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

    run_parser.add_argument('--file', '-f',
        action='store',
        help="Name of the dockerfile. (Default 'PATH/Dockerfile')",
        default="Dockerfile")

    run_parser.add_argument('--rm',
        action='store',
        default=True,
        type=strtobool,
        metavar='{true, false}',
        help="Remove the image after exit (Default True)")

    run_parser.add_argument('--volume',
        action='store',
        help="Bind mount a volume into the image (See systemd-nspawn --bind option)"
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
        default='/bin/sh',
        help='Command to run inside the container (Default /bin/sh)'
    )
    run_parser.set_defaults(func=run)

    wipe_parser = subparsers.add_parser(
        'wipe', help="Wipe runtime folder"
    )
    wipe_parser.set_defaults(func=wipe)

    return parser.parse_args()


def main():
    args = parseargs()

    if os.getuid() != 0:
        print("This script must be run as root")
        sys.exit(1)

    runtime = Path(args.runtime).resolve()
    runtime.mkdir(parents=True, exist_ok=True)

    if hasattr(args, "func"):
        args.func(args)
    else:
        print("No command defined in argparser")
        exit(1)


if __name__ == "__main__":
    main()
