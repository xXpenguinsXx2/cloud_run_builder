#!/bin/bash
# PostgreSQL initialization script for Parquet support

set -e

echo "Initializing PostgreSQL with parquet_fdw support..."

# Create the extension and a simple server wrapper for local parquet files
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS parquet_fdw;

    CREATE SERVER IF NOT EXISTS parquet_server
    FOREIGN DATA WRAPPER parquet_fdw;

    CREATE SCHEMA IF NOT EXISTS parquet_data;

    GRANT USAGE ON SCHEMA parquet_data TO $POSTGRES_USER;

    \echo 'PostgreSQL initialized with Parquet support'
EOSQL

echo "Parquet FDW setup complete!"
