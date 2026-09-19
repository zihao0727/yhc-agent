"""Provision an isolated application database without touching other databases."""
import os
import secrets
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL


def main():
    config = Path(__file__).with_name(".env")
    if config.exists():
        raise SystemExit("backend/.env already exists; provisioning refused.")
    admin_password = os.environ["PGPASSWORD"]
    app_password = secrets.token_urlsafe(32)
    token = secrets.token_urlsafe(32)
    role, database = "firefly_pricing_app", "firefly_pricing"
    with psycopg.connect(host="127.0.0.1", port=5432, user="postgres",
                        password=admin_password, dbname="postgres", autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            raise SystemExit("Application role exists; refusing to change credentials.")
        if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
            raise SystemExit("Application database exists; refusing to overwrite it.")
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
            sql.Identifier(role), sql.Literal(app_password)))
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0").format(
            sql.Identifier(database), sql.Identifier(role)))
    url = URL.create("postgresql+psycopg", username=role, password=app_password,
                     host="127.0.0.1", port=5432, database=database)
    config.write_text(f'DATABASE_URL="{url.render_as_string(hide_password=False)}"\n'
                      f'ADMIN_TOKEN="{token}"\n', encoding="utf-8")
    print(f"Created isolated database {database}; application credentials saved to backend/.env.")


if __name__ == "__main__":
    main()
