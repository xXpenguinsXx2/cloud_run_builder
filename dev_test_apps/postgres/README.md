# PostgreSQL with Parquet Support

This directory contains the Docker configuration for PostgreSQL 14 with the local-file Parquet Foreign Data Wrapper (FDW).

## Features

- **PostgreSQL 14** with `parquet_fdw` installed from source
- Automatically mounts parquet data files from `data-engine/core/utils/events/data`
- Pre-configured with necessary extensions and schemas
- Health checks enabled for container orchestration

## Credentials

- **User**: `testuser`
- **Password**: `testpass`
- **Database**: `testdb`
- **Port**: `5432`

## Quick Start

### Build and Start All Services

```bash
make up-detached
```

This builds and starts:
- Data engine
- Streamlit tester app (port 8501)
- PostgreSQL (port 5432)

### Connect to PostgreSQL

```bash
make psql
```

Or manually:

```bash
psql -h localhost -U testuser -d testdb -W
```

## Example: Query Parquet Files

Once connected to PostgreSQL, you can create foreign tables to query parquet files:

```sql
-- List available extensions
\dx

-- Show parquet data schema
\dn

-- Create a foreign table pointing to a specific parquet file
CREATE FOREIGN TABLE IF NOT EXISTS events_data (
    sk_id text,
    weapon_id int32,
    weapon_type text,
    weapon_attribute text,
    primary_skill text,
    group_skill text,
    event_date text
)
SERVER parquet_server
OPTIONS (
    filename '/parquet_data/comp-abc123.parquet'
);

-- Query the foreign table
SELECT * FROM events_data LIMIT 10;

-- Get file statistics
SELECT COUNT(*) FROM events_data;
```

## Volume Mounts

- **Data**: `/parquet_data` (read-only) → points to `data-engine/core/utils/events/data`
- **PostgreSQL data**: `postgres_data` (named volume) → persistent storage

## Logs

View PostgreSQL logs:

```bash
make postgres-logs
```

Or:

```bash
docker compose logs -f postgres
```

## Troubleshooting

### Parquet FDW not found

If you get `extension "parquet_fdw" does not exist`, the extension may not have compiled. Check build logs:

```bash
docker compose build --no-cache postgres
```

### Can't connect to PostgreSQL

Ensure the container is healthy:

```bash
docker compose ps
```

Wait for health check to pass (may take ~30 seconds on first start).

### Data files not visible

Check mount permissions:

```bash
docker compose exec postgres ls -la /parquet_data/
```

## Advanced Usage

### Load SQL Functions

Create a file `postgres/init-functions.sql` and add to `init-db.sh` to load custom functions.

### Configure Performance

Modify PostgreSQL settings by adding environment variables or config files:

```yaml
postgres:
  environment:
    POSTGRES_INITDB_ARGS: "-c shared_buffers=256MB -c max_connections=100"
```

## Cleanup

Remove all PostgreSQL data and containers:

```bash
make clean
```
