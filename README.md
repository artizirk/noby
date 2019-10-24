# noby

Not moby minimal dockerfile like image builder based on btrfs subvolumes

# Install

    sudo pip3 install git+https://github.com/artizirk/noby.git

For upgrade run this command

    sudo pip3 install --upgrade git+https://github.com/artizirk/noby.git


# Examples

    sudo ./noby.py build -f Dockerfile . -t busybox
    sudo ./noby.py build -f Dockerfile-from .


# Tests

Unittests

    python3 -m unittest

package tests

    python3 setup.py test

Integration tests

    ./test.sh
