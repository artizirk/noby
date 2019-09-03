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
sudo ./noby.py run busybox "echo Hello Noby"


#Test Environment variables.
sudo ./noby.py wipe

#Should not add any env variables
sudo ./noby.py build -f Dockerfile-env .

sudo ./noby.py wipe
#Should override BAR value and add DEV value
sudo ./noby.py build -e BAR=modified --env DEV=devvalue -f Dockerfile-env .
