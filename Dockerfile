FROM debian:bookworm-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential libssl-dev libexpat1-dev bison flex ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# src.tar is `git archive` of the branch under test, see run.sh
COPY src.tar /src.tar
RUN mkdir /src && tar -xf /src.tar -C /src && cd /src \
 && ./configure --prefix=/usr/local --sysconfdir=/etc --disable-flto \
 && make -j"$(nproc)" && make install

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libssl3 libexpat1 python3 tcpdump bind9-dnsutils ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/local /usr/local
RUN ldconfig
COPY fakedns.py runner.py /opt/e2e/
