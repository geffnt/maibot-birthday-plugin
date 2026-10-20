"""通过 NapCat 适配器读取 QQ 名片里的生日。

只依赖 NapCat 适配器插件暴露的强类型 API：

- ``adapter.napcat.account.get_friend_list`` —— 一次拿全量好友，返回里直接带
  ``birthday_year`` / ``birthday_month`` / ``birthday_day``，成本最低。
- ``adapter.napcat.account.get_stranger_info`` —— 单个 QQ 号的资料卡，同样带生日；
  群成员列表本身不含生日，所以群维度只能逐个查。
- ``adapter.napcat.group.get_group_member_list`` —— 取群成员名单，作为逐个查名的输入。
- ``adapter.napcat.group.get_group_list`` —— 取机器人所在的群；只有在群白名单里
  显式写了 ``"*"`` 时才会用到它，以及"人在哪个群就发哪"靠的是群成员采集里的群归属。

**为什么必须走 NapCat 适配器**：这些生日字段来自 QQ NT 的
``NodeIKernelProfileService/getCoreAndBaseInfo`` 与 ``ProfileService/fetchUserDetailInfo``；
SnowLuma 适配器的资料拉取走 OIDB 0xFE1_2，其属性键目录只有
102 签名 / 103 备注 / 105 等级 / 20002 昵称 / 20009 性别 / 20037 年龄 / 27394 QID，
类型 ``UserProfileInfo`` 里也没有生日字段，拿不到数据。

**返回信封**：SDK 会把 ``{success, result}`` 拆开（``api.call`` 的 result key 就是
``result``），但插件与适配器版本不同时形态可能不一样，所以这里两种都兼容。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .text import parse_month_day


FRIEND_LIST_API = "adapter.napcat.account.get_friend_list"
GROUP_LIST_API = "adapter.napcat.group.get_group_list"
GROUP_MEMBER_LIST_API = "adapter.napcat.group.get_group_member_list"
STRANGER_INFO_API = "adapter.napcat.account.get_stranger_info"

_LIST_KEYS = (
    "friend_list",
    "friends",
    "list",
    "data",
    "members",
    "group_member_list",
    "group_list",
    "groups",
)


class NapCatBirthdaySource:
    """NapCat 名片生日数据源。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    @property
    def _logger(self) -> Any:
        return self._plugin.ctx.logger

    def api_name(self, short_name: str) -> str:
        """把短名解析成实际调用名（可能带适配器插件 ID 前缀）。"""

        return self._plugin.adapter_api_name(short_name)

    async def _call(self, short_name: str, **kwargs: Any) -> Any:
        api = self.api_name(short_name)
        try:
            raw = await self._plugin.ctx.api.call(api, **kwargs)
        except Exception as exc:
            self._logger.warning("调用 %s 失败: %s", api, exc)
            return None
        return self._unwrap(api, raw)

    def _unwrap(self, api: str, raw: Any) -> Any:
        """兼容直接返回值与 ``{success, result}`` 信封两种形态。"""

        if raw is None:
            self._logger.warning("%s 无响应（返回 None）", api)
            return None
        if isinstance(raw, dict) and "success" in raw:
            if not raw.get("success"):
                self._logger.warning(
                    "%s 返回失败: %s", api, raw.get("error") or raw.get("message") or raw
                )
                return None
            return raw.get("result")
        return raw

    @staticmethod
    def _as_list(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in _LIST_KEYS:
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    # ---------- 采集入口 ----------

    async def fetch_friends(self, *, no_cache: bool = False) -> list[dict[str, Any]] | None:
        """拉取好友列表；失败返回 ``None``（与"确实没有好友"区分开）。"""

        payload = await self._call(FRIEND_LIST_API, no_cache=bool(no_cache))
        if payload is None:
            return None
        return self._as_list(payload)

    async def fetch_groups(self, *, no_cache: bool = False) -> list[dict[str, Any]] | None:
        """拉取机器人所在的群列表；失败返回 ``None``。"""

        payload = await self._call(GROUP_LIST_API, no_cache=bool(no_cache))
        if payload is None:
            return None
        return self._as_list(payload)

    async def fetch_group_members(
        self, group_id: str, *, no_cache: bool = False
    ) -> list[dict[str, Any]] | None:
        """拉取群成员列表；失败返回 ``None``。"""

        payload = await self._call(
            GROUP_MEMBER_LIST_API, group_id=str(group_id), no_cache=bool(no_cache)
        )
        if payload is None:
            return None
        return self._as_list(payload)

    async def fetch_stranger(self, user_id: str, *, no_cache: bool = False) -> dict[str, Any] | None:
        """查单个 QQ 号的资料卡；失败返回 ``None``。"""

        payload = await self._call(
            STRANGER_INFO_API, user_id=str(user_id), no_cache=bool(no_cache)
        )
        if isinstance(payload, dict):
            return payload
        return None


def normalize_user_id(info: dict[str, Any]) -> str:
    """从资料里取出 QQ 号。

    只用 ``user_id`` / ``uin``：``uid`` 是 QQ NT 内部的字符串 ID，不是 QQ 号，
    拿它当键会让缓存和道贺目标都错位。
    """

    if not isinstance(info, dict):
        return ""
    for key in ("user_id", "uin"):
        value = info.get(key)
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip()
        # 群成员/陌生人资料里的 uin 偶尔是 "0"，视为无效
        if text and text != "0":
            return text
    return ""


def normalize_group_id(info: dict[str, Any]) -> str:
    """从群列表条目里取出群号。"""

    if not isinstance(info, dict):
        return ""
    for key in ("group_id", "groupCode", "group_code", "group", "id"):
        value = info.get(key)
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip()
        if text and text != "0":
            return text
    return ""


def pick_name(info: dict[str, Any]) -> str:
    """取显示名：备注优先（用户自己起的名字更好认），其次昵称、群名片。"""

    if not isinstance(info, dict):
        return ""
    for key in ("remark", "nickname", "card", "nick", "nickName"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def extract_birthday(info: dict[str, Any]) -> tuple[int, int, int] | None:
    """从资料里解析 ``(月, 日, 年)``；没有有效生日时返回 ``None``。

    生日字段在 NapCat 的 schema 里是 Optional：对方没填、或隐私不公开时就是
    空/0。这里全部按"拿不到"处理，绝不把 0 当成有效日期。
    """

    if not isinstance(info, dict):
        return None

    month = _to_int(info.get("birthday_month"))
    day = _to_int(info.get("birthday_day"))
    year = _to_int(info.get("birthday_year"))

    if not _valid_month_day(month, day):
        # 兼容 "05-20" / "5月20日" / "5/20" 这类字符串形态
        month = day = 0
        for key in ("birthday", "birthday_str", "birthDay", "birthdayDate"):
            value = info.get(key)
            if isinstance(value, str):
                parsed = parse_month_day(value)
                if parsed:
                    month, day = parsed
                    break
        if not _valid_month_day(month, day):
            return None

    return month, day, max(0, year)


def _to_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(str(value).strip())
    except Exception:
        return 0


def _valid_month_day(month: int, day: int) -> bool:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return False
    try:
        # 用闰年校验，允许 2 月 29 日
        date(2024, month, day)
        return True
    except ValueError:
        return False
