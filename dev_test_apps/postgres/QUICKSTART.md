# PostgreSQL Parquet Support - Quick Start

## What You Get

A Docker image running **PostgreSQL 14** with the **parquet_fdw** extension, allowing you to:
- Query parquet files created by your Streamlit data engine
- Use standard SQL to analyze snapshot data
- Join parquet data with PostgreSQL tables

## Usage

### Start All Services (Recommended)

```bash
make up-detached
```

This starts:
- Data engine container
- Streamlit UI (http://localhost:8501)
- PostgreSQL database (localhost:5432)

### Connect to PostgreSQL

```bash
make psql
```

### Example: Query Your Parquet Data

```bash
# Connect to PostgreSQL
make psql

# In psql, create a foreign table for your parquet file:
CREATE FOREIGN TABLE events (
    sk_id text,
    weapon_id int,
    weapon_type text,
    weapon_attribute text,
    primary_skill text,
    group_skill text,
    event_date text
)
SERVER parquet_server
OPTIONS (filename '/parquet_data/comp-abc123.parquet');

# Query it!
SELECT COUNT(*) FROM events;
SELECT weapon_type, COUNT(*) FROM events GROUP BY weapon_type;
```

## Credentials

- **Host**: `localhost`
- **Port**: `5432`
- **Username**: `testuser`
- **Password**: `testpass`
- **Database**: `testdb`

## Files

- `Dockerfile` - PostgreSQL 14 with build tools and parquet_fdw
- `init-db.sh` - Automatic setup of FDW extension and schemas
- `sample-queries.sql` - Example SQL queries

## Troubleshooting

**Build fails with "parquet_fdw not found"?**
```bash
docker compose build --no-cache postgres
```

**Can't connect?**
```bash
docker compose logs postgres
docker compose ps
```

**View PostgreSQL logs:**
```bash
make postgres-logs
```

## Clean Up

```bash
make clean
```

See `postgres/README.md` for detailed documentation.
