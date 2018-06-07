FROM scratch

HOST echo "hello world"
HOST pwd
HOST ls -lah
HOST env
HOST echo Target: $TARGET
HOST echo "im test" > $TARGET/test
HOST mkdir -p $TARGET/bin $TARGET/usr/bin
HOST wget https://busybox.net/downloads/binaries/1.28.1-defconfig-multiarch/busybox-x86_64 -O $TARGET/bin/busybox
HOST chmod +x $TARGET/bin/busybox
HOST ln -s /bin/busybox $TARGET/bin/sh
HOST mkdir $TARGET/etc
HOST echo "NAME=busybox" > $TARGET/etc/os-release
RUN /bin/busybox --install -s /bin
RUN echo "Hello World"
RUN echo 4 >> test
