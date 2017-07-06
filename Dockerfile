FROM scratch

HOST echo "hello world"
HOST pwd
HOST ls -lah
HOST env
HOST echo Target: $TARGET
HOST echo "im test" > $TARGET/test
#RUN ls
