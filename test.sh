#!/bin/bash
# run some simple tests

set -e  # exit on any errors

#  wipe backing storage
sudo ./noby.py wipe

#  First pass without any cache
sudo ./noby.py build -f Dockerfile . -t busybox
sudo ./noby.py build -f Dockerfile-from .

#  Should do nothing
sudo ./noby.py build -f Dockerfile . -t busybox
sudo ./noby.py build -f Dockerfile-from .

#  Test run
sudo ./noby.py run -f Dockerfile . "echo Hello Noby"
