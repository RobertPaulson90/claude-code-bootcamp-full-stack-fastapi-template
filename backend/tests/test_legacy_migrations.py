import uuid

import pytest
from alembic import command
from pydantic import PostgresDsn
from sqlalchemy import inspect, text
from sqlmodel import create_engine

from app.core.config import settings
from app.core.db import engine
from tests.test_onboarding_migration import _alembic_config


def test_legacy_migrations_preserve_users_and_item_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exercise the entire migration chain without downgrading the suite's DB.
    database = f"migration_test_{uuid.uuid4().hex}"
    admin = create_engine(engine.url, isolation_level="AUTOCOMMIT")
    isolated = create_engine(engine.url.set(database=database))
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
        monkeypatch.setattr(
            settings,
            "DATABASE_URL",
            PostgresDsn(isolated.url.render_as_string(hide_password=False)),
        )
        config = _alembic_config()
        command.upgrade(config, "e2412789c190")
        with isolated.begin() as connection:
            connection.execute(
                text(
                    'INSERT INTO "user" '
                    "(id, email, is_active, is_superuser, full_name, hashed_password) "
                    "VALUES (7, 'legacy@example.com', true, false, 'Legacy', 'hash')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO item (id, owner_id, title, description) "
                    "VALUES (11, 7, 'Legacy item', 'Preserved description')"
                )
            )

        command.upgrade(config, "head")
        with isolated.connect() as connection:
            user_id, owner_id, item_id, name, title, description = connection.execute(
                text(
                    "SELECT u.id, i.owner_id, i.id, u.full_name, i.title, i.description "
                    'FROM "user" u JOIN item i ON i.owner_id = u.id'
                )
            ).one()
            assert isinstance(user_id, uuid.UUID)
            assert isinstance(item_id, uuid.UUID)
            assert owner_id == user_id
            assert (name, title, description) == (
                "Legacy",
                "Legacy item",
                "Preserved description",
            )
            assert (
                connection.execute(text("SELECT count(*) FROM onboarding")).scalar_one()
                == 0
            )
            columns = {c["name"]: c for c in inspect(connection).get_columns("user")}
            assert columns["email"]["type"].length == 255
            assert "created_at" in columns
            foreign_key = inspect(connection).get_foreign_keys("item")[0]
            assert foreign_key["options"]["ondelete"] == "CASCADE"

        command.downgrade(config, "e2412789c190")
        with isolated.connect() as connection:
            user_id, owner_id, item_id, email, title = connection.execute(
                text(
                    "SELECT u.id, i.owner_id, i.id, u.email, i.title "
                    'FROM "user" u JOIN item i ON i.owner_id = u.id'
                )
            ).one()
            assert isinstance(user_id, int)
            assert isinstance(item_id, int)
            assert owner_id == user_id
            assert (email, title) == ("legacy@example.com", "Legacy item")
            columns = {c["name"]: c for c in inspect(connection).get_columns("user")}
            assert columns["email"]["type"].length is None
            assert "created_at" not in columns
            assert not inspect(connection).has_table("onboarding")

        command.downgrade(config, "base")
        with isolated.connect() as connection:
            assert not inspect(connection).has_table("user")
            assert not inspect(connection).has_table("item")
    finally:
        isolated.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database}"')
        admin.dispose()
