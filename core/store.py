"""生日缓存的本地持久化。

数据落在 ``ctx.paths.data_dir / birthdays.json``，结构：

```json
{
  "version": 1,
  "updated_at": 1700000000.0,
  "sources": {"friends": {"at": 1700000000.0, "count": 120}},
  "people": {
    "123456": {
      "key": "123456", "user_id": "123456", "name": "小明", "month": 5, "day": 20,
      "year": 0, "source": "friend", "is_friend": true,
      "groups": ["987654"], "fetched_at": 1700000000.0
    }
  },
  "greeted": {"123456": "2026-05-20"}
}
```

设计取向：

- **只读失败不丢数据**：文件损坏时按空库启动并备份原文件，绝不因为一次解析失败
  就把缓存清成空。
- **原子写**：先写 ``.tmp`` 再 ``os.replace``，避免掉电/崩溃留下半个 JSON。
- **道贺去重按天**：``greeted`` 记录 ``key -> YYYY-MM-DD``，同一天不会重复道贺；
  历史记录按保留天数裁剪，避免无限增长。
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


STORE_VERSION = 1

# greeted 历史保留天数：只影响文件大小，判定只看当天
GREETED_KEEP_DAYS = 30


class BirthdayStore:
    """生日与道贺记录的本地存储。"""

    def __init__(self, path: Path, logger: Any) -> None:
        self._path = Path(path)
        self._logger = logger
        self._data: dict[str, Any] = self._empty()

    # ---------- 基础读写 ----------

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "updated_at": 0.0,
            "sources": {},
            "people": {},
            "greeted": {},
        }

    def load(self) -> None:
        """从磁盘读取缓存；文件缺失或损坏时保持空库（损坏时先备份）。"""

        if not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
        except Exception as exc:
            backup = self._path.with_suffix(self._path.suffix + ".broken")
            try:
                os.replace(self._path, backup)
                self._logger.warning(
                    "生日缓存解析失败(%s)，已备份到 %s 并按空库启动", exc, backup
                )
            except Exception:
                self._logger.warning("生日缓存解析失败(%s)，且备份失败，按空库启动", exc)
            return

        if not isinstance(parsed, dict):
            self._logger.warning("生日缓存顶层不是对象，按空库启动")
            return

        data = self._empty()
        data["version"] = int(parsed.get("version") or STORE_VERSION)
        data["updated_at"] = float(parsed.get("updated_at") or 0.0)
        sources = parsed.get("sources")
        if isinstance(sources, dict):
            data["sources"] = {str(k): v for k, v in sources.items() if isinstance(v, dict)}
        people = parsed.get("people")
        if isinstance(people, dict):
            data["people"] = {
                str(k): v for k, v in people.items() if isinstance(v, dict) and _valid_person(v)
            }
        greeted = parsed.get("greeted")
        if isinstance(greeted, dict):
            data["greeted"] = {str(k): str(v) for k, v in greeted.items()}
        self._data = data
        self._logger.debug("生日缓存已载入：%d 人", len(self._data["people"]))

    def save(self) -> bool:
        """原子写回磁盘。"""

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, self._path)
            return True
        except Exception as exc:
            self._logger.warning("生日缓存写入失败: %s", exc)
            return False

    # ---------- 属性 ----------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def people(self) -> dict[str, dict[str, Any]]:
        return self._data["people"]

    @property
    def updated_at(self) -> float:
        return float(self._data.get("updated_at") or 0.0)

    def touch_updated_at(self) -> None:
        self._data["updated_at"] = time.time()

    def source_info(self, name: str) -> dict[str, Any] | None:
        info = self._data["sources"].get(name)
        return info if isinstance(info, dict) else None

    def source_age_seconds(self, name: str) -> float | None:
        info = self.source_info(name)
        if not info:
            return None
        at = info.get("at")
        if not isinstance(at, (int, float)) or at <= 0:
            return None
        return max(0.0, time.time() - float(at))

    def mark_source(self, name: str, count: int) -> None:
        self._data["sources"][name] = {"at": time.time(), "count": int(count)}

    # ---------- 写入 ----------

    def upsert_person(self, person: dict[str, Any]) -> bool:
        """写入/更新一个人，返回内容是否发生变化。"""

        key = str(person.get("key") or "").strip()
        if not key:
            return False
        normalized = {
            "key": key,
            "user_id": str(person.get("user_id") or ""),
            "name": str(person.get("name") or key),
            "month": int(person.get("month") or 0),
            "day": int(person.get("day") or 0),
            "year": int(person.get("year") or 0),
            "source": str(person.get("source") or ""),
            # 好友标记：只有好友能收到私聊（非好友开不了私聊流）。用独立字段而不是
            # 只看 source，是因为"先作为群成员被发现、后来才加好友"的人 source 仍是 group
            "is_friend": bool(person.get("is_friend")),
            "groups": sorted({str(g) for g in (person.get("groups") or []) if str(g).strip()}),
            "fetched_at": float(person.get("fetched_at") or time.time()),
        }
        existing = self._data["people"].get(key)
        # fetched_at 每次都变，比较时排除它，否则任何一次采集都会被判成"有变化"
        if isinstance(existing, dict) and _same_person(existing, normalized):
            changed = False
            merged_groups = sorted(set(existing.get("groups") or []) | set(normalized["groups"]))
            if merged_groups != (existing.get("groups") or []):
                existing["groups"] = merged_groups
                changed = True
            # 好友标记只能由假变真：一次采集没看到不代表好友关系没了
            if normalized["is_friend"] and not existing.get("is_friend"):
                existing["is_friend"] = True
                changed = True
            existing["fetched_at"] = normalized["fetched_at"]
            return changed
        self._data["people"][key] = normalized
        return True

    def add_group_membership(self, key: str, group_id: str) -> bool:
        """把某人标记为属于某个群（群道贺的成员过滤依据）。"""

        person = self._data["people"].get(key)
        if not isinstance(person, dict):
            return False
        groups = set(person.get("groups") or [])
        if group_id in groups:
            return False
        groups.add(group_id)
        person["groups"] = sorted(groups)
        return True

    def remove_keys(self, keys: Iterable[str]) -> int:
        removed = 0
        for key in keys:
            if self._data["people"].pop(str(key), None) is not None:
                removed += 1
        return removed

    # ---------- 查询 ----------

    def people_on(self, month: int, day: int) -> list[dict[str, Any]]:
        return [
            person
            for person in self._data["people"].values()
            if int(person.get("month") or 0) == month and int(person.get("day") or 0) == day
        ]

    def get_person(self, key: str) -> dict[str, Any] | None:
        person = self._data["people"].get(str(key))
        return person if isinstance(person, dict) else None

    def has_birthday(self, key: str) -> bool:
        return self.get_person(key) is not None

    def upcoming(self, today: date, within_days: int) -> list[tuple[dict[str, Any], int]]:
        """返回未来 ``within_days`` 天内（不含当天）过生日的人与剩余天数。"""

        if within_days <= 0:
            return []
        window_end = today + timedelta(days=within_days)
        result: list[tuple[dict[str, Any], int]] = []
        for person in self._data["people"].values():
            month = int(person.get("month") or 0)
            day = int(person.get("day") or 0)
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue
            # 跨越年末时生日落在明年，所以今年明年各试一次
            for year in (today.year, today.year + 1):
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    # 2 月 29 日在平年不存在，这一年就当作没有生日
                    continue
                if today < candidate <= window_end:
                    result.append((person, (candidate - today).days))
                    break
        return sorted(result, key=lambda item: item[1])

    def search(self, keyword: str, limit: int = 50) -> list[dict[str, Any]]:
        text = str(keyword or "").strip().lower()
        if not text:
            return []
        matched = [
            person
            for person in self._data["people"].values()
            if text in str(person.get("name") or "").lower()
            or text in str(person.get("user_id") or "")
            or text in str(person.get("key") or "").lower()
        ]
        return matched[:limit]

    def all_people(self, limit: int = 0) -> list[dict[str, Any]]:
        people = list(self._data["people"].values())
        people.sort(key=lambda p: (int(p.get("month") or 0), int(p.get("day") or 0)))
        return people[:limit] if limit > 0 else people

    # ---------- 道贺去重 ----------

    @staticmethod
    def _today_key(today: date | None = None) -> str:
        return (today or date.today()).strftime("%Y-%m-%d")

    def was_greeted(self, key: str, today: date | None = None) -> bool:
        return self._data["greeted"].get(str(key)) == self._today_key(today)

    def mark_greeted(self, key: str, today: date | None = None) -> None:
        self._data["greeted"][str(key)] = self._today_key(today)

    def prune_greeted(self, keep_days: int = GREETED_KEEP_DAYS) -> int:
        """裁掉过期道贺记录，返回删除条数。"""

        cutoff = (date.today() - timedelta(days=max(0, keep_days))).strftime("%Y-%m-%d")
        greeted = self._data["greeted"]
        stale = [key for key, value in greeted.items() if str(value) < cutoff]
        for key in stale:
            greeted.pop(key, None)
        return len(stale)


def _valid_person(person: dict[str, Any]) -> bool:
    month = person.get("month")
    day = person.get("day")
    try:
        return 1 <= int(month) <= 12 and 1 <= int(day) <= 31
    except Exception:
        return False


def _same_person(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for field in ("name", "month", "day", "year", "source", "user_id"):
        if left.get(field) != right.get(field):
            return False
    return True


def now_month_day(today: datetime | date | None = None) -> tuple[int, int]:
    current = today or datetime.now()
    return current.month, current.day
