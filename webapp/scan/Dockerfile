FROM debian:bookworm-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        libdvdread8 \
        lsdvd \
        mplayer \
    && rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["lsdvd"]
