"""生日采集：好友全量一次拿完，群成员逐个查名片。

采集策略上的取舍：

- **好友走批量**：``get_friend_list`` 一次返回全量且直接带生日，成本极低，默认开启。
- **群成员走逐个查**：群成员列表没有生日字段，只能对每个人调 ``get_stranger_info``。
  这在大群里等于几十上百次请求，有触发风控的风险，因此带每群人数上限
  (``max_members_per_group``) 与跨群总量预算 (``max_stranger_queries``)。
- **群范围只有一个来源**：``greeting.announce_groups``。**留空 = 一个群都不碰**；
  填了群号 = 只在这些群；显式写 ``["*"]`` = 用 ``get_group_list`` 自动发现机器人所在的全部群。
- **群归属要单独记**：成员列表里就有群号，属于"免费信息"，必须先落库再考虑查生日；
  否则"好友同时是群成员"的人拿不到群归属，群道贺就发不出来。
- **读不到不等于要删**：QQ 名片生日是 Optional 字段（对方没填或隐私不公开就是空），
  所以"这次没读到"只当作没数据，绝不据此清掉已有记录——除非用户显式开启
  ``drop_missing`` 且本次好友拉取确实成功。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .napcat import (
    NapCatBirthdaySource,
    extract_birthday,
    normalize_group_id,
    normalize_user_id,
    pick_name,
)


class BirthdayCollector:
    """把 NapCat 名片里的生日合并进本地缓存。"""

    def __init__(self, plugin: Any, store: Any, source: NapCatBirthdaySource) -> None:
        self._plugin = plugin
        self._store = store
        self._source = source

    @property
    def _logger(self) -> Any:
        return self._plugin.ctx.logger

    async def refresh(self, *, reason: str = "") -> dict[str, Any]:
        """执行一次完整采集并落盘，返回统计信息。"""

        config = self._plugin.config.source
        stats: dict[str, Any] = {
            "friends_total": 0,
            "friends_with_birthday": 0,
            "groups_planned": 0,
            "groups_discovered": 0,
            "group_members_checked": 0,
            "strangers_queried": 0,
            "saved": 0,
            "errors": 0,
            "ok": False,
            "reason": reason,
        }
        started_at = time.time()
        seen_keys: set[str] = set()

        # ---- 好友 ----
        friends_ok = False
        if config.include_friends:
            friends = await self._source.fetch_friends(no_cache=config.no_cache)
            if friends is None:
                stats["errors"] += 1
            else:
                friends_ok = True
                stats["friends_total"] = len(friends)
                for item in friends:
                    user_id = normalize_user_id(item)
                    if not user_id:
                        continue
                    seen_keys.add(user_id)
                    if self._ingest(item, source="friend"):
                        stats["saved"] += 1
                        stats["friends_with_birthday"] += 1
                self._store.mark_source("friends", len(friends))

        # ---- 群成员 ----
        group_ok = False
        groups: list[str] = []
        whitelist = [
            str(gid).strip()
            for gid in self._plugin.config.greeting.announce_groups
            if str(gid).strip()
        ]
        if config.include_group_members:
            if not whitelist:
                # 白名单为空 = 一个群都不碰。
                # 群成员采集是整条链路里唯一有风控风险的动作，绝不能靠"空字段"隐式打开：
                # 空列表的自然读法就是"一个都没选"。想采全部群要显式写 ["*"]。
                self._logger.info(
                    '群白名单为空，跳过群成员采集（一个群都不碰；要采全部群请显式填 ["*"]）'
                )
            elif "*" in whitelist:
                # 只有显式写了 * 才去自动发现
                discovered = await self._source.fetch_groups(no_cache=config.no_cache)
                if discovered is None:
                    stats["errors"] += 1
                else:
                    groups = [
                        gid
                        for gid in (normalize_group_id(item) for item in discovered)
                        if gid
                    ]
                    stats["groups_discovered"] = len(groups)
                    self._logger.info(
                        '群白名单为 ["*"]，使用机器人所在的 %d 个群：%s',
                        len(groups),
                        "、".join(groups[:10]) or "无",
                    )
            else:
                groups = whitelist
                stats["groups_planned"] = len(groups)

            budget = int(config.max_stranger_queries)
            for group_id in groups:
                if budget > 0 and stats["strangers_queried"] >= budget:
                    self._logger.warning(
                        "已达单次查名片上限(%d 次)，剩余群本轮跳过，下次采集继续", budget
                    )
                    break
                members = await self._source.fetch_group_members(
                    group_id, no_cache=config.no_cache
                )
                if members is None:
                    stats["errors"] += 1
                    continue
                group_ok = True
                targets = members
                if config.max_members_per_group > 0:
                    targets = members[: config.max_members_per_group]
                self._store.mark_source(f"group:{group_id}", len(members))

                for member in targets:
                    user_id = normalize_user_id(member)
                    if not user_id:
                        continue
                    # 群归属是"免费"的（就在成员列表里），必须先记：
                    # 好友在上一步已经进过 seen_keys，如果这里因为 seen_keys 直接跳过，
                    # "好友同时是群成员"的人就永远拿不到群归属，
                    # 于是"人在哪个群就发哪"对他们一条都发不出去。
                    if user_id in seen_keys:
                        if self._store.has_birthday(user_id):
                            self._store.add_group_membership(user_id, group_id)
                        continue
                    seen_keys.add(user_id)
                    stats["group_members_checked"] += 1

                    # 成员条目本身可能带生日，先合并一次（拿不到就什么都不写）
                    if self._ingest(member, source="group", group_id=group_id):
                        stats["saved"] += 1
                    if self._store.has_birthday(user_id):
                        # 已经有生日记录，只补群归属，不再多打一次接口
                        self._store.add_group_membership(user_id, group_id)
                        continue

                    if budget > 0 and stats["strangers_queried"] >= budget:
                        continue
                    detail = await self._source.fetch_stranger(
                        user_id, no_cache=config.no_cache
                    )
                    stats["strangers_queried"] += 1
                    if detail is None:
                        stats["errors"] += 1
                    elif self._ingest(
                        detail,
                        source="group",
                        group_id=group_id,
                        fallback_name=pick_name(member),
                    ):
                        stats["saved"] += 1
                        self._store.add_group_membership(user_id, group_id)

                    if config.stranger_fetch_delay_ms > 0:
                        await asyncio.sleep(config.stranger_fetch_delay_ms / 1000.0)

        # ---- 清理本次没出现的好友（默认关闭）----
        if config.drop_missing and friends_ok:
            stale = [
                key
                for key, person in list(self._store.people.items())
                if person.get("source") == "friend" and key not in seen_keys
            ]
            removed = self._store.remove_keys(stale)
            if removed:
                stats["dropped"] = removed

        stats["ok"] = bool(friends_ok or group_ok)
        # 只有真的拿到数据才推进"上次采集时间"。否则在适配器还没连上时（插件启动
        # 往往早于 NapCat 就绪）会把时间戳推到现在，之后要白等一整个采集周期
        # （默认 7 天）才重试。失败时交给调度器的退避重试。
        if stats["ok"]:
            self._store.touch_updated_at()
        self._store.save()
        stats["total_cached"] = len(self._store.people)
        stats["elapsed_ms"] = int((time.time() - started_at) * 1000)
        self._logger.info(
            "生日采集完成(%s)：好友 %d 条命中 %d 人，群 %d 个 / 成员检查 %d 人（查名片 %d 次），"
            "新增/更新 %d 条，缓存共 %d 人，耗时 %dms，失败 %d 次%s",
            reason or "手动",
            stats["friends_total"],
            stats["friends_with_birthday"],
            stats["groups_planned"] or stats["groups_discovered"],
            stats["group_members_checked"],
            stats["strangers_queried"],
            stats["saved"],
            stats["total_cached"],
            stats["elapsed_ms"],
            stats["errors"],
            "" if stats["ok"] else "（未取得任何数据，稍后重试）",
        )
        return stats

    def _ingest(
        self,
        info: dict[str, Any],
        *,
        source: str,
        group_id: str = "",
        fallback_name: str = "",
    ) -> bool:
        """把一条资料合并进缓存，返回是否写入/更新了生日记录。"""

        user_id = normalize_user_id(info)
        if not user_id:
            return False

        existing = self._store.get_person(user_id)
        birthday = extract_birthday(info)
        if birthday is None:
            if existing and group_id:
                self._store.add_group_membership(user_id, group_id)
            return False

        month, day, year = birthday
        name = (
            pick_name(info)
            or fallback_name
            or (existing or {}).get("name")
            or user_id
        )
        # 出生年份经常为空：不要用空年份覆盖已经拿到的年份
        if year == 0 and existing:
            year = int(existing.get("year") or 0)

        groups = list(existing.get("groups") or []) if existing else []
        if group_id:
            groups.append(group_id)

        return self._store.upsert_person(
            {
                "key": user_id,
                "user_id": user_id,
                "name": name,
                "month": month,
                "day": day,
                "year": year,
                "source": source if not existing else (existing.get("source") or source),
                "groups": groups,
            }
        )
