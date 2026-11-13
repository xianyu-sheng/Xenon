"""Three report renderers that duplicate the same header logic."""

from __future__ import annotations


def _header(title: str, width: int) -> list[str]:
    """Return the shared three-line header block for a report."""
    bar = "=" * width
    return [bar, title.center(width), bar]


def render_users(rows: list[tuple[str, str]], width: int = 30) -> str:
    lines = _header("USERS", width)
    for name, role in rows:
        lines.append(f"{name:<15}{role:>15}")
    return "\n".join(lines)


def render_orders(rows: list[tuple[str, int]], width: int = 30) -> str:
    lines = _header("ORDERS", width)
    for order_id, total in rows:
        lines.append(f"{order_id:<15}{total:>15}")
    return "\n".join(lines)


def render_stock(rows: list[tuple[str, int]], width: int = 30) -> str:
    lines = _header("STOCK", width)
    for sku, qty in rows:
        lines.append(f"{sku:<15}{qty:>15}")
    return "\n".join(lines)
