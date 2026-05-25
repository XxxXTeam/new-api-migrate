#!/usr/bin/env python3
"""
Migrate a running site's SQLite database to PostgreSQL.

Recommended flow:
  1. Stop new-api cleanly so in-memory/batch counters are flushed.
  2. Create an empty PostgreSQL database.
  3. Let new-api start once with SQL_DSN=postgresql://... so GORM creates the
     PostgreSQL schema, then stop it.
  4. Run this script with --truncate-target to replace the bootstrap rows.
  5. Start new-api with SQL_DSN and REDIS_CONN_STRING.

Python dependency:
  pip install "psycopg[binary]"

The script can also create PostgreSQL tables from SQLite metadata when
--create-schema is passed, but the existing-schema path is safer for GORM apps.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    import psycopg  # type: ignore

    PG_DRIVER = "psycopg"
except ImportError:  # pragma: no cover - depends on user's environment.
    try:
        import psycopg2 as psycopg  # type: ignore

        PG_DRIVER = "psycopg2"
    except ImportError:  # pragma: no cover
        psycopg = None
        PG_DRIVER = ""


SQLITE_INTERNAL_PREFIX = "sqlite_"

KNOWN_BOOLEAN_COLUMNS = {
    ("abilities", "enabled"),
    ("custom_oauth_providers", "enabled"),
    ("logs", "is_stream"),
    ("passkey_credentials", "clone_warning"),
    ("passkey_credentials", "user_present"),
    ("passkey_credentials", "user_verified"),
    ("passkey_credentials", "backup_eligible"),
    ("passkey_credentials", "backup_state"),
    ("subscription_plans", "enabled"),
    ("tasks", "per_call_billing"),
    ("tokens", "cross_group_retry"),
    ("tokens", "model_limits_enabled"),
    ("tokens", "unlimited_quota"),
    ("two_fa_backup_codes", "is_used"),
    ("two_fas", "is_enabled"),
}

KNOWN_JSON_COLUMNS = {
    ("channels", "channel_info"),
}


class MigrationError(RuntimeError):
    pass


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def placeholders(count: int) -> str:
    return ", ".join(["%s"] * count)


def strip_sqlite_query(path: str) -> str:
    if path.startswith("file:"):
        return path
    if "?" not in path:
        return path
    head, _query = path.split("?", 1)
    if head and Path(head).exists():
        return head
    return path


def connect_sqlite(path: str, readonly: bool = False) -> sqlite3.Connection:
    clean_path = strip_sqlite_query(path)
    if clean_path.startswith("file:"):
        conn = sqlite3.connect(clean_path, uri=True, timeout=30)
    elif readonly:
        abs_path = Path(clean_path).resolve()
        uri_path = abs_path.as_posix()
        conn = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(clean_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def backup_sqlite(source: str, backup_dir: Path | None) -> Path:
    source_path = Path(strip_sqlite_query(source)).resolve()
    if not source_path.exists():
        raise MigrationError(f"SQLite database not found: {source_path}")

    if backup_dir is None:
        backup_dir = source_path.parent
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{source_path.stem}.pg-migration-{stamp}{source_path.suffix}"

    src = connect_sqlite(str(source_path), readonly=True)
    try:
        dst = sqlite3.connect(str(backup_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    wal = source_path.with_suffix(source_path.suffix + "-wal")
    shm = source_path.with_suffix(source_path.suffix + "-shm")
    for sidecar in (wal, shm):
        if sidecar.exists():
            shutil.copy2(sidecar, backup_dir / f"{backup_path.name}.{sidecar.name.rsplit('-', 1)[-1]}")

    return backup_path


def sqlite_integrity_check(conn: sqlite3.Connection) -> None:
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise MigrationError(f"SQLite integrity_check failed: {result[0] if result else 'no result'}")


def sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE ?
        ORDER BY name
        """,
        (f"{SQLITE_INTERNAL_PREFIX}%",),
    ).fetchall()
    return [row["name"] for row in rows]


def sqlite_table_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"PRAGMA table_info({quote_sqlite_ident(table)})").fetchall()


def quote_sqlite_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def pg_connect(dsn: str):
    if psycopg is None:
        raise MigrationError('Missing PostgreSQL driver. Install with: pip install "psycopg[binary]"')
    return psycopg.connect(dsn)


def pg_current_schema(cur) -> str:
    cur.execute("SELECT current_schema()")
    return cur.fetchone()[0]


def pg_table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (table,))
    return cur.fetchone()[0] is not None


def pg_table_count(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}")
    return int(cur.fetchone()[0])


def pg_column_types(cur) -> dict[tuple[str, str], str]:
    cur.execute(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = current_schema()
        """
    )
    return {(r[0], r[1]): r[2] for r in cur.fetchall()}


def sqlite_type_to_pg(table: str, column: str, raw_type: str, is_single_integer_pk: bool) -> str:
    if (table, column) in KNOWN_BOOLEAN_COLUMNS:
        return "boolean"
    if (table, column) in KNOWN_JSON_COLUMNS:
        return "json"
    if is_single_integer_pk:
        return "bigserial"

    typ = (raw_type or "").strip().lower()
    if not typ:
        return "text"
    if "varchar" in typ:
        match = re.search(r"varchar\s*\(\s*\d+\s*\)", typ)
        return match.group(0) if match else "varchar"
    if "char" in typ:
        match = re.search(r"char\s*\(\s*\d+\s*\)", typ)
        return match.group(0) if match else "varchar"
    if "bigint" in typ:
        return "bigint"
    if "int" in typ:
        return "bigint"
    if "decimal" in typ:
        match = re.search(r"decimal\s*\(\s*\d+\s*,\s*\d+\s*\)", typ)
        return match.group(0) if match else "numeric"
    if "numeric" in typ:
        return "numeric"
    if "double" in typ or "real" in typ or "float" in typ:
        return "double precision"
    if "bool" in typ:
        return "boolean"
    if "json" in typ:
        return "json"
    if "datetime" in typ or "timestamp" in typ:
        return "timestamp with time zone"
    if "date" in typ:
        return "timestamp with time zone"
    if "blob" in typ or "binary" in typ:
        return "bytea"
    return "text"


def convert_default(default: Any, pg_type: str) -> str | None:
    if default is None:
        return None
    text = str(default).strip()
    if not text:
        return None
    if text.upper() == "NULL":
        return None
    if pg_type == "boolean":
        if text in {"1", "'1'", '"1"'}:
            return "true"
        if text in {"0", "'0'", '"0"'}:
            return "false"
    return text


def create_tables_from_sqlite(cur, sqlite_conn: sqlite3.Connection, tables: list[str]) -> None:
    for table in tables:
        cols = sqlite_table_columns(sqlite_conn, table)
        if not cols:
            continue
        pk_cols = [c["name"] for c in cols if int(c["pk"] or 0) > 0]
        single_pk = len(pk_cols) == 1
        column_defs: list[str] = []
        table_constraints: list[str] = []

        for col in cols:
            name = col["name"]
            is_single_integer_pk = (
                single_pk
                and pk_cols[0] == name
                and "int" in (col["type"] or "").lower()
            )
            pg_type = sqlite_type_to_pg(table, name, col["type"], is_single_integer_pk)
            parts = [quote_ident(name), pg_type]

            if is_single_integer_pk:
                parts.append("PRIMARY KEY")
            else:
                if int(col["notnull"] or 0):
                    parts.append("NOT NULL")
                default = convert_default(col["dflt_value"], pg_type)
                if default is not None:
                    parts.append("DEFAULT " + default)

            column_defs.append(" ".join(parts))

        if len(pk_cols) > 1:
            table_constraints.append(
                "PRIMARY KEY (" + ", ".join(quote_ident(c) for c in pk_cols) + ")"
            )

        all_defs = column_defs + table_constraints
        sql = f"CREATE TABLE IF NOT EXISTS {quote_ident(table)} (\n  "
        sql += ",\n  ".join(all_defs)
        sql += "\n)"
        cur.execute(sql)


def sqlite_index_sql(conn: sqlite3.Connection, tables: Iterable[str]) -> list[str]:
    table_set = set(tables)
    rows = conn.execute(
        """
        SELECT name, tbl_name, sql
        FROM sqlite_master
        WHERE type = 'index'
          AND sql IS NOT NULL
        ORDER BY name
        """
    ).fetchall()
    out: list[str] = []
    for row in rows:
        if row["tbl_name"] not in table_set:
            continue
        sql = convert_sqlite_index_to_pg(row["sql"])
        if sql:
            out.append(sql)
    return out


def convert_sqlite_index_to_pg(sql: str) -> str | None:
    sql = sql.strip().rstrip(";")
    if not sql:
        return None
    sql = sql.replace("`", '"')
    sql = re.sub(r"\[(.*?)\]", r'"\1"', sql)
    sql = re.sub(r"CREATE\s+UNIQUE\s+INDEX\s+", "CREATE UNIQUE INDEX IF NOT EXISTS ", sql, count=1, flags=re.I)
    sql = re.sub(r"CREATE\s+INDEX\s+", "CREATE INDEX IF NOT EXISTS ", sql, count=1, flags=re.I)
    return sql


def truncate_target(cur, tables: list[str]) -> None:
    existing = [t for t in tables if pg_table_exists(cur, t)]
    if not existing:
        return
    quoted = ", ".join(quote_ident(t) for t in existing)
    cur.execute(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")


def assert_target_empty(cur, tables: list[str]) -> None:
    nonempty: list[tuple[str, int]] = []
    for table in tables:
        if not pg_table_exists(cur, table):
            continue
        count = pg_table_count(cur, table)
        if count > 0:
            nonempty.append((table, count))
    if nonempty:
        detail = ", ".join(f"{t}={c}" for t, c in nonempty[:20])
        raise MigrationError(
            "Target PostgreSQL already has rows. Use --truncate-target if this is the migration database. "
            f"Non-empty tables: {detail}"
        )


def convert_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    text = str(value).strip().lower()
    if text in {"1", "t", "true", "y", "yes", "on"}:
        return True
    if text in {"0", "f", "false", "n", "no", "off", ""}:
        return False
    raise MigrationError(f"Cannot convert value to boolean: {value!r}")


def convert_json(value: Any, table: str, column: str, lenient: bool) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    text = str(value)
    if text == "":
        return None
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        if lenient:
            return None
        raise MigrationError(f"Invalid JSON in {table}.{column}: {exc}") from exc
    return text


def convert_value(
    value: Any,
    table: str,
    column: str,
    pg_type: str | None,
    lenient_json: bool,
) -> Any:
    if value is None:
        return None
    effective_type = (pg_type or "").lower()
    if effective_type == "boolean" or (table, column) in KNOWN_BOOLEAN_COLUMNS:
        return convert_bool(value)
    if effective_type in {"json", "jsonb"} or (table, column) in KNOWN_JSON_COLUMNS:
        return convert_json(value, table, column, lenient_json)
    if isinstance(value, bytes) and effective_type != "bytea":
        return value.decode("utf-8", errors="strict")
    return value


def copy_table(
    sqlite_conn: sqlite3.Connection,
    pg_cur,
    table: str,
    column_types: dict[tuple[str, str], str],
    batch_size: int,
    lenient_json: bool,
) -> int:
    source_cols = [c["name"] for c in sqlite_table_columns(sqlite_conn, table)]
    if not source_cols:
        return 0
    if not pg_table_exists(pg_cur, table):
        raise MigrationError(f"Target table does not exist in PostgreSQL: {table}")

    target_cols = {column for (target_table, column), _typ in column_types.items() if target_table == table}
    cols = [c for c in source_cols if c in target_cols]
    skipped = [c for c in source_cols if c not in target_cols]
    if skipped:
        print(f"Skipping columns absent from PostgreSQL {table}: {', '.join(skipped)}")
    if not cols:
        raise MigrationError(f"No common columns between SQLite and PostgreSQL for table: {table}")

    quoted_source_cols = ", ".join(quote_sqlite_ident(c) for c in cols)
    select_sql = f"SELECT {quoted_source_cols} FROM {quote_sqlite_ident(table)}"
    insert_sql = (
        f"INSERT INTO {quote_ident(table)} "
        f"({', '.join(quote_ident(c) for c in cols)}) "
        f"VALUES ({placeholders(len(cols))})"
    )

    total = 0
    rows: list[tuple[Any, ...]] = []
    src_cur = sqlite_conn.execute(select_sql)
    for row in src_cur:
        converted = tuple(
            convert_value(row[c], table, c, column_types.get((table, c)), lenient_json)
            for c in cols
        )
        rows.append(converted)
        if len(rows) >= batch_size:
            pg_cur.executemany(insert_sql, rows)
            total += len(rows)
            rows.clear()
    if rows:
        pg_cur.executemany(insert_sql, rows)
        total += len(rows)
    return total


def reset_sequences(cur, tables: list[str]) -> None:
    for table in tables:
        if not pg_table_exists(cur, table):
            continue
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %s
              AND column_default LIKE 'nextval(%%'
            ORDER BY ordinal_position
            """,
            (table,),
        )
        for (column,) in cur.fetchall():
            cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (table, column))
            seq_row = cur.fetchone()
            if not seq_row or not seq_row[0]:
                continue
            seq_name = seq_row[0]
            cur.execute(f"SELECT COALESCE(MAX({quote_ident(column)}), 0) FROM {quote_ident(table)}")
            max_id = int(cur.fetchone()[0] or 0)
            if max_id <= 0:
                cur.execute("SELECT setval(%s, 1, false)", (seq_name,))
            else:
                cur.execute("SELECT setval(%s, %s, true)", (seq_name, max_id))


def verify_counts(sqlite_conn: sqlite3.Connection, pg_cur, tables: list[str]) -> list[tuple[str, int, int]]:
    mismatches: list[tuple[str, int, int]] = []
    for table in tables:
        src_count = int(sqlite_conn.execute(f"SELECT COUNT(*) FROM {quote_sqlite_ident(table)}").fetchone()[0])
        dst_count = pg_table_count(pg_cur, table)
        if src_count != dst_count:
            mismatches.append((table, src_count, dst_count))
    return mismatches


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate new-api SQLite data to PostgreSQL.")
    parser.add_argument("--sqlite", default=os.environ.get("SQLITE_PATH", "one-api.db"), help="SQLite path or SQLITE_PATH value.")
    parser.add_argument("--postgres", default=os.environ.get("SQL_DSN"), help="PostgreSQL DSN, e.g. postgresql://user:pass@host:5432/new-api")
    parser.add_argument("--backup-dir", default=None, help="Directory for the consistent SQLite backup copy.")
    parser.add_argument("--no-backup", action="store_true", help="Read SQLite directly. Not recommended for live sites.")
    parser.add_argument("--create-schema", action="store_true", help="Create PostgreSQL tables/indexes from SQLite metadata.")
    parser.add_argument("--drop-target", action="store_true", help="Drop target tables before creating schema. Requires --create-schema.")
    parser.add_argument("--truncate-target", action="store_true", help="TRUNCATE target tables before loading data.")
    parser.add_argument("--allow-nonempty", action="store_true", help="Allow inserting into non-empty target tables.")
    parser.add_argument("--skip-indexes", action="store_true", help="Skip creating indexes when --create-schema is used.")
    parser.add_argument("--batch-size", type=int, default=1000, help="Rows per executemany batch.")
    parser.add_argument("--lenient-json", action="store_true", help="Convert invalid/empty JSON values to NULL instead of failing.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect source/target and print actions without writing PostgreSQL.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.postgres:
        raise MigrationError("PostgreSQL DSN is required. Pass --postgres or set SQL_DSN.")
    if args.batch_size <= 0:
        raise MigrationError("--batch-size must be greater than zero.")
    if args.drop_target and not args.create_schema:
        raise MigrationError("--drop-target requires --create-schema.")

    if args.no_backup:
        sqlite_path = strip_sqlite_query(args.sqlite)
        print(f"Reading SQLite directly: {sqlite_path}")
    else:
        backup_dir = Path(args.backup_dir).resolve() if args.backup_dir else None
        sqlite_path = str(backup_sqlite(args.sqlite, backup_dir))
        print(f"SQLite backup created: {sqlite_path}")

    sqlite_conn = connect_sqlite(sqlite_path, readonly=True)
    pg_conn = None
    try:
        sqlite_integrity_check(sqlite_conn)
        tables = sqlite_tables(sqlite_conn)
        if not tables:
            raise MigrationError("No user tables found in SQLite database.")
        print(f"Discovered {len(tables)} SQLite tables: {', '.join(tables)}")

        pg_conn = pg_connect(args.postgres)
        pg_cur = pg_conn.cursor()
        schema = pg_current_schema(pg_cur)
        print(f"PostgreSQL current_schema(): {schema}")

        if args.dry_run:
            for table in tables:
                count = int(sqlite_conn.execute(f"SELECT COUNT(*) FROM {quote_sqlite_ident(table)}").fetchone()[0])
                exists = pg_table_exists(pg_cur, table)
                print(f"DRY RUN table={table} sqlite_rows={count} pg_exists={exists}")
            return 0

        try:
            if args.drop_target:
                for table in reversed(tables):
                    pg_cur.execute(f"DROP TABLE IF EXISTS {quote_ident(table)} CASCADE")

            if args.create_schema:
                create_tables_from_sqlite(pg_cur, sqlite_conn, tables)
                pg_conn.commit()

            if args.truncate_target:
                truncate_target(pg_cur, tables)
            elif not args.allow_nonempty:
                assert_target_empty(pg_cur, tables)

            column_types = pg_column_types(pg_cur)
            copied: list[tuple[str, int]] = []
            for table in tables:
                count = copy_table(sqlite_conn, pg_cur, table, column_types, args.batch_size, args.lenient_json)
                copied.append((table, count))
                print(f"Copied {table}: {count} rows")

            if args.create_schema and not args.skip_indexes:
                for sql in sqlite_index_sql(sqlite_conn, tables):
                    pg_cur.execute(sql)

            reset_sequences(pg_cur, tables)
            mismatches = verify_counts(sqlite_conn, pg_cur, tables)
            if mismatches:
                details = ", ".join(f"{t}: sqlite={s}, pg={p}" for t, s, p in mismatches)
                raise MigrationError(f"Row count verification failed: {details}")

            pg_conn.commit()
        except Exception:
            pg_conn.rollback()
            raise

        total = sum(count for _table, count in copied)
        print(f"Migration completed: {len(copied)} tables, {total} rows.")
        return 0
    finally:
        sqlite_conn.close()
        if pg_conn is not None:
            pg_conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
