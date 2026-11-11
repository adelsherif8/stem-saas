"""Programmatic Alembic runner used on app startup and by the test suite.

Handles the one awkward case: a database that was created by the old
`Base.metadata.create_all()` (tables exist, but no `alembic_version`). That
schema is identical to revision 0001, so we stamp it there and upgrade.
"""
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from .database import DATABASE_URL

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = "0001_initial"


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.attributes["url"] = url or DATABASE_URL
    cfg.attributes["configure_logging"] = False
    return cfg


def upgrade_to_head(url: str | None = None) -> None:
    url = url or DATABASE_URL
    cfg = alembic_config(url)
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    if "users" in tables and "alembic_version" not in tables:
        command.stamp(cfg, BASELINE)
    command.upgrade(cfg, "head")
