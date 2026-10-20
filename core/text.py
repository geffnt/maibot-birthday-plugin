"""文案渲染工具。

统一走 :func:`safe_format`：只替换已知的 ``{占位符}``，未知占位符原样保留，
绝不抛异常——用户可以在模板里随便写 ``{}`` 而不会让道贺流程崩掉。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MD_PATTERN = re.compile(r"(\d{1,2})\s*[-/.月]\s*(\d{1,2})")


def parse_month_day(value: str) -> tuple[int, int] | None:
    """从 ``"05-20"`` / ``"5月20日"`` / ``"5/20"`` 解析出 ``(月, 日)``；非法返回 ``None``。

    同时用于手工登记指令（``/birth set 小明 05-20``）和名片字段的字符串兜底，
    保证两处口径一致。
    """

    if not isinstance(value, str):
        return None
    matched = _MD_PATTERN.search(value)
    if not matched:
        return None
    month, day = int(matched.group(1)), int(matched.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        # 用闰年校验，允许 2 月 29 日
        date(2024, month, day)
    except ValueError:
        return None
    return month, day


def format_month_day_of(month: int, day: int) -> str:
    return f"{int(month or 0):02d}-{int(day or 0):02d}"


def safe_format(template: str, mapping: dict[str, Any]) -> str:
    """只替换已知占位符的模板渲染，未知占位符保持原样。"""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in mapping:
            return match.group(0)
        value = mapping[key]
        return "" if value is None else str(value)

    return _PLACEHOLDER_RE.sub(_replace, str(template or ""))


def display_name(person: dict[str, Any]) -> str:
    """取显示名：优先备注/昵称，最后回落到 QQ 号。"""

    for field in ("name", "remark"):
        value = str(person.get(field) or "").strip()
        if value:
            return value
    user_id = str(person.get("user_id") or "").strip()
    return user_id or "有人"


def format_month_day(person: dict[str, Any]) -> str:
    return f"{int(person.get('month') or 0)}月{int(person.get('day') or 0)}日"


def age_of(person: dict[str, Any], today: date | None = None) -> int | None:
    """按名片出生年份推算年龄；没有年份或年份不合理时返回 ``None``。

    QQ 名片里出生年份经常为空或不可见，所以这里不猜、不补默认值：
    拿不到就返回 ``None``，让模板里的 ``{age}`` 渲染成空串。
    """

    try:
        year = int(person.get("year") or 0)
    except Exception:
        return None
    current = today or date.today()
    if year < 1900 or year > current.year:
        return None
    age = current.year - year
    if age < 0 or age > 150:
        return None
    return age


def render_greeting(template: str, person: dict[str, Any], today: date | None = None) -> str:
    """按模板渲染道贺文案。"""

    current = today or date.today()
    age = age_of(person, current)
    return safe_format(
        template,
        {
            "name": display_name(person),
            "age": "" if age is None else age,
            "month": int(person.get("month") or 0),
            "day": int(person.get("day") or 0),
            "qq": str(person.get("user_id") or ""),
        },
    ).strip()


def join_names(people: Iterable[dict[str, Any]], limit: int) -> str:
    """把人名列表拼成 ``A、B 等 3 人`` 形式。"""

    names = [display_name(person) for person in people]
    if not names:
        return ""
    if len(names) <= limit:
        return "、".join(names)
    head = "、".join(names[:limit])
    return f"{head} 等 {len(names)} 人"


def render_prompt(
    template: str,
    people: Iterable[dict[str, Any]],
    *,
    max_names: int,
    upcoming_text: str = "",
) -> str:
    """渲染注入到回复要求里的生日提醒。"""

    people_list = list(people)
    return safe_format(
        template,
        {
            "names": join_names(people_list, max_names),
            "count": len(people_list),
            "upcoming": upcoming_text,
        },
    ).strip()
