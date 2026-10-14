"""SQLAlchemy 2.1 types ``desc()`` as ``OrderByList | UnaryExpression``, which Query.order_by rejects."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import UnaryExpression
from sqlalchemy import desc as sa_desc


def desc(column: Any) -> UnaryExpression[Any]:
    return cast(UnaryExpression[Any], sa_desc(column))
