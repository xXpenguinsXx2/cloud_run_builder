-- Sample queries for exploring Parquet files via PostgreSQL
-- Connect and run: psql -U testuser -d testdb -f sample-queries.sql

\echo '=== Parquet FDW Extension Info ==='
\dx+ parquet_fdw

\echo ''
\echo '=== Available Servers ==='
SELECT * FROM pg_foreign_server;

\echo ''
\echo '=== User Mappings ==='
SELECT * FROM pg_user_mappings;

\echo ''
\echo '=== Create Sample Foreign Table ==='
\echo 'Example: Create a foreign table for a specific parquet file'
\echo ''
\echo 'CREATE FOREIGN TABLE events ('
\echo '    sk_id text,'
\echo '    weapon_id int,'
\echo '    weapon_type text,'
\echo '    weapon_attribute text,'
\echo '    primary_skill text,'
\echo '    group_skill text,'
\echo '    event_date text'
\echo ') SERVER parquet_server'
\echo 'OPTIONS ('
\echo '    filename '\''/parquet_data/<filename>.parquet'\'
\echo ');'
\echo ''
\echo 'SELECT * FROM events LIMIT 5;'
\echo ''

\echo '=== List Parquet Files ==='
-- You can use this to see what parquet files are available
\! ls -lh /parquet_data/*.parquet 2>/dev/null | head -20

\echo ''
\echo 'Setup complete! You can now:'
\echo '1. Use the CREATE FOREIGN TABLE statement above to create tables'
\echo '2. Query them with standard SQL: SELECT * FROM events;'
\echo '3. Join with other tables or do aggregations'
