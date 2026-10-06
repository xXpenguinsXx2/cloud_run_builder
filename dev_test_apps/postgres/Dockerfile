# PostgreSQL with Parquet support for testing
FROM postgres:14-bookworm

# Install build dependencies and Arrow libraries from Apache Arrow apt repository
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    build-essential \
    make \
    postgresql-server-dev-14 \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
        curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
            -o /usr/share/keyrings/cloud.google.asc; \
        echo "deb [signed-by=/usr/share/keyrings/cloud.google.asc] https://packages.cloud.google.com/apt gcsfuse-bookworm main" \
            > /etc/apt/sources.list.d/gcsfuse.list; \
        apt-get update; \
        apt-get install -y --no-install-recommends gcsfuse fuse3; \
        rm -rf /var/lib/apt/lists/*; \
        grep -q '^user_allow_other$' /etc/fuse.conf || echo user_allow_other >> /etc/fuse.conf

RUN curl -sSL https://apache.jfrog.io/artifactory/arrow/$(lsb_release --id --short | tr 'A-Z' 'a-z')/apache-arrow-apt-source-latest-$(lsb_release --codename --short).deb -o /tmp/apache-arrow-apt-source.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/apache-arrow-apt-source.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libarrow-dev \
        libparquet-dev \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /tmp/apache-arrow-apt-source.deb

# Build the local-file Parquet FDW used by this PostgreSQL image.
RUN set -eux; \
    curl -sSL https://github.com/adjust/parquet_fdw/archive/d15664ebdcb1cbb759a7bb39fd26fb2fa2fff3ea.tar.gz -o /tmp/parquet_fdw.tar.gz; \
    mkdir -p /tmp/parquet_fdw; \
    tar -xzf /tmp/parquet_fdw.tar.gz --strip-components=1 -C /tmp/parquet_fdw; \
    cd /tmp/parquet_fdw; \
    sed -i 's/-std=c++17/-std=c++20/' Makefile; \
    make; \
    make install; \
    rm -rf /tmp/parquet_fdw.tar.gz /tmp/parquet_fdw

# Copy initialization scripts
COPY init-db.sh /docker-entrypoint-initdb.d/
COPY start-postgres-with-gcsfuse.sh /usr/local/bin/
RUN chmod +x /docker-entrypoint-initdb.d/init-db.sh /usr/local/bin/start-postgres-with-gcsfuse.sh

EXPOSE 5432
