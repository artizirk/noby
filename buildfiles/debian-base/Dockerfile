FROM scratch

ENV DEBIAN_FRONTEND=noninteractive
ENV DEBCONF_NONINTERACTIVE_SEEN=true
ENV LC_ALL=C.UTF-8
ENV LANGUAGE=C.UTF-8
ENV LANG=C.UTF-8

HOST debootstrap --foreign --arch=armhf --variant=minbase \
        --include=dbus,debian-archive-keyring \
        jessie $TARGET

HOST cp -v /usr/bin/qemu-arm-static $TARGET/usr/bin && \
    echo force-unsafe-io > $TARGET/etc/dpkg/dpkg.cfg.d/force-unsafe-io
# debootstrap tries to mount /dev by itself and that doesnt work when
# running under systemd-nspawn
HOST chroot $TARGET /debootstrap/debootstrap --second-stage

RUN rm -f /var/cache/apt/archives/*.deb /var/cache/apt/archives/partial/*.deb /var/cache/apt/*.bin || true
