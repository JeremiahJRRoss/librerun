"""A value the database writes on UPDATE is fetched by the UPDATE (H17's rule).

A column whose ``onupdate`` is a SQL expression (``func.now()``), or which
declares a ``server_onupdate``, gets its new value from the database.
SQLAlchemy 2.0's default ``eager_defaults="auto"`` fetches such values on
INSERT, by RETURNING, and not on UPDATE: the flush expires the attribute
instead, and the next read of it is a lazy load. Async code cannot lazy
load — it raises ``MissingGreenlet`` — so a handler that reads the column
after writing a row it already had answers 500. That was H17: the
settings PUT, on every write of a key after its first
(``tests/test_app_settings_put_twice.py``).

The fix is one line on the mapper, ``eager_defaults=True``, and this
module is what keeps the next table from needing its own H17: every
mapped class under ``app/models`` with such a column carries it. A
Python-side ``onupdate`` (a callable or a constant) is computed in the
process and never expires, so it is not held to the rule.
"""
from __future__ import annotations

import importlib
import pkgutil
from datetime import datetime

from sqlalchemy import DateTime, Integer, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Every column the rule exists for: H17's two and, since K5a, the settings
# table's. The walk must meet them, so a walk that found no mappers — a
# package that stopped importing, a registry that moved — fails instead of
# passing on nothing.
KNOWN = {
    "app_settings.updated_at",
    "agent_step_configs.updated_at",
    "agent_settings.updated_at",
}


def _server_side_onupdates(mappers) -> tuple[set[str], list[str]]:
    """Every ``table.column`` whose UPDATE value the database computes, and
    those among them whose mapper would leave it expired after a flush."""
    met: set[str] = set()
    expired: list[str] = []
    for mapper in mappers:
        for column in mapper.columns:
            onupdate = column.onupdate
            computed_by_the_database = column.server_onupdate is not None or (
                onupdate is not None and onupdate.is_clause_element
            )
            if not computed_by_the_database:
                continue
            name = f"{column.table.name}.{column.name}"
            met.add(name)
            if mapper.eager_defaults is not True:
                expired.append(
                    f"{name} ({mapper.class_.__name__}: eager_defaults="
                    f"{mapper.eager_defaults!r})"
                )
    return met, sorted(expired)


def _app_mappers():
    import app.models
    from app.database import Base

    for module in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"app.models.{module.name}")
    return list(Base.registry.mappers)


def test_every_server_side_onupdate_is_fetched_eagerly():
    met, expired = _server_side_onupdates(_app_mappers())
    assert KNOWN <= met, (
        f"the walk met {sorted(met)}, not every updated_at column it must "
        f"see — it is not looking at the application's mappers"
    )
    assert expired == [], (
        "a column the database writes on UPDATE is left expired after the "
        "flush, so reading it in async code raises MissingGreenlet (H17): "
        "add __mapper_args__ = {'eager_defaults': True} to its class — "
        + "; ".join(expired)
    )


def test_the_rule_rejects_an_expired_onupdate():
    """The negative probe: a throwaway class shaped like H17's model is
    flagged without the mapper argument and passes with it."""

    class ProbeBase(DeclarativeBase):
        pass

    class Unfetched(ProbeBase):
        __tablename__ = "probe_unfetched"

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        updated_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
        )

    class Fetched(ProbeBase):
        __tablename__ = "probe_fetched"
        __mapper_args__ = {"eager_defaults": True}

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        updated_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
        )

    class PythonSide(ProbeBase):
        __tablename__ = "probe_python_side"

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        touched: Mapped[int] = mapped_column(Integer, onupdate=lambda: 1)

    met, expired = _server_side_onupdates(ProbeBase.registry.mappers)
    assert met == {"probe_unfetched.updated_at", "probe_fetched.updated_at"}
    assert len(expired) == 1 and expired[0].startswith("probe_unfetched.updated_at ")
    assert "Unfetched" in expired[0] and "'auto'" in expired[0]
