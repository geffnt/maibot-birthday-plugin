"""寿星当日道贺的调度与投递。

调度：一个 60 秒步进的后台协程，负责"按需重新采集"和"到点道贺"两件事。
所有时间判断都用本机本地时间，时间窗由 ``greeting.hour_start`` / ``hour_end`` 决定；
窗口内启动（比如机器人 15:00 才上线）也会补发当天还没道贺的人。

投递是两个互相独立的开关（``greet_in_group`` / ``greet_in_private``）：

- **群聊**：发到"寿星实际所在的群"（``groups`` 来自群成员采集），配了白名单则取交集；
  默认 @ 寿星本人（SDK 没有 @ 段，走适配器原生 ``send_group_msg`` 的消息段数组）。
- **群白名单留空 = 一个群都不发**；写了 ``"*"`` 才表示"他所在的全部群"。
- **私聊**：只发给好友——非好友开不了私聊流。

不依赖"当前有消息流"：群道贺直接给群号，私聊先找已有聊天流、找不到就
``chat.open_session`` 现开一个，再 ``send.text``。
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime
from typing import Any, Iterable

from . import text as text_utils


_STREAM_ID_KEYS = ("stream_id", "session_id", "streamId", "id", "stream")

# 模型常见的"多话"包装：代码块围栏、前缀标签、成对引号
_PREFIX_LABELS = ("祝福语：", "祝福语:", "生日祝福：", "生日祝福:", "文案：", "文案:")
_QUOTE_CHARS = "\"'“”‘’「」『』"


def _sanitize_greeting(text: str) -> str:
    """清洗模型输出：只留第一句有效内容，去掉围栏/前缀/引号，并限长。"""

    if not text:
        return ""
    cleaned = str(text).strip().strip("`").strip()
    head, _, tail = cleaned.partition("\n")
    if head.strip().lower() in ("text", "txt", "markdown", "plaintext"):
        # 去掉模型自带的语言标记行
        cleaned = tail
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0]
    for label in _PREFIX_LABELS:
        if first.startswith(label):
            first = first[len(label):].strip()
            break
    first = first.strip().strip(_QUOTE_CHARS).strip()
    if len(first) > 120:
        first = first[:120].rstrip() + "…"
    return first


def _is_friend(person: dict[str, Any]) -> bool:
    """是否好友。旧缓存没有 is_friend 字段，退回看 source。"""

    if bool(person.get("is_friend")):
        return True
    return str(person.get("source") or "") == "friend"


def _api_ok(result: Any) -> bool:
    """判断适配器 API 返回是否成功（兼容 success / status / retcode 三种信封）。"""

    if not isinstance(result, dict):
        return bool(result)
    if "success" in result:
        return bool(result.get("success"))
    status = result.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip().lower() in ("ok", "async")
    if "retcode" in result:
        try:
            return int(result.get("retcode") or 0) == 0
        except Exception:
            return False
    return bool(result)


def _stream_id_of(value: Any) -> str:
    """从各种形态的"聊天流"返回值里取出 stream_id。"""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in _STREAM_ID_KEYS:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, dict):
                nested = _stream_id_of(item)
                if nested:
                    return nested
    return ""


class BirthdayScheduler:
    """后台调度器：定期采集 + 寿星当天道贺。"""

    TICK_SECONDS = 60

    # 采集失败（适配器还没就绪等）时的重试退避：300s 起，翻倍，封顶 2 小时。
    # 不用一整个采集周期（默认 7 天）去等，也避免每分钟无脑重试刷日志。
    RETRY_BACKOFF_MIN_SECONDS = 300
    RETRY_BACKOFF_MAX_SECONDS = 7200

    def __init__(self, plugin: Any, store: Any, collector: Any) -> None:
        self._plugin = plugin
        self._store = store
        self._collector = collector
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._refresh_lock = asyncio.Lock()
        self._retry_backoff_seconds = 0
        self._next_attempt_at = 0.0

    @property
    def _logger(self) -> Any:
        return self._plugin.ctx.logger

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="maibot-birthday-scheduler")
        self._logger.info("生日调度已启动")

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # 卸载阶段不再往上抛
            self._logger.debug("生日调度退出时异常: %s", exc)
        self._logger.info("生日调度已停止")

    async def _run(self) -> None:
        try:
            delay = max(0, int(self._plugin.config.source.initial_refresh_delay_seconds))
            if delay:
                await asyncio.sleep(delay)
            while not self._stopping:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._logger.warning("生日调度本轮出错: %s", exc, exc_info=True)
                await asyncio.sleep(self.TICK_SECONDS)
        except asyncio.CancelledError:
            raise

    async def _tick(self) -> None:
        await self.refresh_if_needed()
        await self.greet_due()

    # ---------- 采集 ----------

    async def refresh_if_needed(self, *, force: bool = False) -> dict[str, Any] | None:
        """到间隔就重新采集；已有采集在跑时直接返回 ``None``。

        采集失败（一条数据都没拿到）时按退避重试，而不是等下一个完整采集周期：
        插件通常比 NapCat 更早就绪，启动后第一次采集很容易扑空。
        """

        if self._refresh_lock.locked():
            return None
        async with self._refresh_lock:
            now = time.time()
            if not force and now < self._next_attempt_at:
                return None

            interval_seconds = max(1, int(self._plugin.config.source.refresh_interval_hours)) * 3600
            updated_at = self._store.updated_at
            if not force and updated_at > 0 and (now - updated_at) < interval_seconds:
                return None

            stats = await self._collector.refresh(reason="强制" if force else "定时")
            if isinstance(stats, dict) and stats.get("ok"):
                self._retry_backoff_seconds = 0
                self._next_attempt_at = 0.0
            else:
                self._retry_backoff_seconds = min(
                    max(self._retry_backoff_seconds * 2, self.RETRY_BACKOFF_MIN_SECONDS),
                    self.RETRY_BACKOFF_MAX_SECONDS,
                )
                self._next_attempt_at = time.time() + self._retry_backoff_seconds
                self._logger.warning(
                    "本次采集没有取得任何数据（适配器未就绪？），%d 秒后重试",
                    self._retry_backoff_seconds,
                )
            return stats

    # ---------- 道贺 ----------

    async def greet_due(self) -> list[str]:
        """给今天过生日、且还没道贺过的人发祝福。"""

        config = self._plugin.config.greeting
        if not (config.greet_in_group or config.greet_in_private):
            return []
        if not self._in_window():
            return []

        today = date.today()
        people = self._store.people_on(today.month, today.day)
        due = [
            person
            for person in people
            if not self._store.was_greeted(str(person.get("key") or ""), today)
        ]
        if not due:
            return []
        return await self.greet_people(due, label="定时")

    async def compose_greeting(
        self, person: dict[str, Any], today: date | None = None
    ) -> str:
        """算出某个人的道贺文案：模板兜底 → （默认）交给模型生成。

        发送与 ``/birth preview`` 共用这一条路径，保证"预览看到的"就是"会发出去的"。
        """

        config = self._plugin.config.greeting
        text = text_utils.render_greeting(
            config.greeting_template, person, today or date.today()
        )
        if config.use_llm_polish:
            text = await self._compose_greeting(text, person)
        return text

    async def greet_people(
        self,
        people: Iterable[dict[str, Any]],
        *,
        dry_run: bool = False,
        mark: bool = True,
        label: str = "",
    ) -> list[str]:
        """对指定的人执行道贺，返回结果描述列表。

        Args:
            people: 目标人物列表。
            dry_run: 只渲染不发送（用于试运行）。
            mark: 成功后是否写入"今天已道贺"，试发时传 ``False``。
            label: 日志标签。
        """

        config = self._plugin.config.greeting
        if not (config.greet_in_group or config.greet_in_private):
            return []

        today = date.today()
        outcomes: list[str] = []
        for person in people:
            key = str(person.get("key") or "")
            text = await self.compose_greeting(person, today)

            if dry_run:
                outcomes.append(f"[试运行] {text_utils.display_name(person)} ← {text}")
                continue

            delivered = False
            if config.greet_in_group:
                delivered = await self._deliver_group(person, text) or delivered
            if config.greet_in_private:
                delivered = await self._deliver_private(person, text) or delivered

            name = text_utils.display_name(person)
            if delivered:
                if mark and key:
                    self._store.mark_greeted(key, today)
                outcomes.append(f"已道贺 {name}")
                self._logger.info("[%s] 已向 %s 道贺：%s", label or "生日", name, text)
            else:
                outcomes.append(f"未送达 {name}")
                self._logger.warning(
                    "[%s] %s 的道贺没有送达：群聊=%s（目标群 %s）私聊=%s",
                    label or "生日",
                    name,
                    config.greet_in_group,
                    "、".join(self.group_targets(person)) or "无",
                    config.greet_in_private,
                )

        if not dry_run and outcomes:
            self._store.save()
        return outcomes

    # ---------- 目标解析 ----------

    def group_targets(self, person: dict[str, Any]) -> list[str]:
        """这个人应该被道贺的群。

        规则：**人在哪个群就发哪**——只发到他的群归属里（`groups` 来自群成员采集）。
        白名单只起"收窄"作用，且**留空就是不发**：空列表读作"一个群都没选"，
        绝不能反过来变成"所有群"。写了 ``"*"`` 才表示"他所在的全部群"。
        """

        member_of = [str(g) for g in (person.get("groups") or []) if str(g).strip()]
        allow = [
            str(g).strip()
            for g in self._plugin.config.greeting.announce_groups
            if str(g).strip()
        ]
        if not allow:
            return []
        if "*" in allow:
            return member_of
        return [group for group in member_of if group in allow]

    def private_target(self, person: dict[str, Any]) -> str:
        """私聊对象：只有好友才发（非好友开不了私聊流），返回 QQ 号或空串。"""

        if not _is_friend(person):
            return ""
        return str(person.get("user_id") or "").strip()

    def temp_session_target(self, person: dict[str, Any]) -> tuple[str, str]:
        """非好友的私聊通道：借共同群发「群临时会话」。

        返回 ``(QQ 号, 借用哪个群)``；是好友、开关关着、或没有交集群时返回空。
        交集群用的是和白名单同一套规则（``group_targets``），不会为了私聊去碰
        白名单以外的群。
        """

        if _is_friend(person):
            return "", ""
        if not bool(self._plugin.config.greeting.private_temp_session):
            return "", ""
        user_id = str(person.get("user_id") or "").strip()
        groups = self.group_targets(person)
        if not user_id or not groups:
            return "", ""
        return user_id, groups[0]

    # ---------- 投递 ----------

    async def _deliver_group(self, person: dict[str, Any], text: str) -> bool:
        targets = self.group_targets(person)
        if not targets:
            self._logger.debug(
                "%s 没有可用的道贺群（白名单=%s / 所属群=%s）",
                text_utils.display_name(person),
                self._plugin.config.greeting.announce_groups or "空（不群发）",
                person.get("groups") or "无",
            )
            return False

        sent = False
        for group_id in targets:
            sent = await self._send_group(group_id, person, text) or sent
        return sent

    async def _send_group(self, group_id: str, person: dict[str, Any], text: str) -> bool:
        """在群里发道贺，默认 @ 寿星。

        SDK 的 send 能力只有 text / image / forward / hybrid，**没有 @ 段**，
        所以这里走适配器原生的 ``send_group_msg``（OneBot 消息段数组）。
        失败时退回 Host 的 ``send.text``（没有 @），让"发不了 @"不至于变成"不发"。
        """

        user_id = str(person.get("user_id") or "").strip()
        at_person = bool(self._plugin.config.greeting.at_person) and bool(user_id)
        segments: list[dict[str, Any]] = []
        if at_person:
            segments.append({"type": "at", "data": {"qq": user_id}})
            segments.append({"type": "text", "data": {"text": " " + text}})
        else:
            segments.append({"type": "text", "data": {"text": text}})

        try:
            api = self._plugin.adapter_api_name("adapter.napcat.group.send_group_msg")
            result = await self._plugin.ctx.api.call(
                api, params={"group_id": str(group_id), "message": segments}
            )
            if _api_ok(result):
                return True
            self._logger.warning(
                "群 %s 用 send_group_msg 发送失败，回退普通文本: %s",
                group_id,
                str(result)[:200],
            )
        except Exception as exc:
            self._logger.warning("群 %s 用 send_group_msg 发送异常，回退普通文本: %s", group_id, exc)

        stream_id = await self._resolve_stream(group_id=group_id)
        if not stream_id:
            return False
        return await self._send_text(text, stream_id)

    async def _deliver_private(self, person: dict[str, Any], text: str) -> bool:
        user_id = self.private_target(person)
        if user_id:
            stream_id = await self._resolve_stream(user_id=user_id)
            if not stream_id:
                return False
            return await self._send_text(text, stream_id)

        # 非好友：试一次群临时会话——QQ 群允许发起临时会话时，非好友也能收到这条私聊
        user_id, group_id = self.temp_session_target(person)
        if not user_id:
            self._logger.debug(
                "%s 不是好友（或临时会话关着/没有交集群），跳过私聊",
                text_utils.display_name(person),
            )
            return False
        return await self._send_temp_session(user_id, group_id, text)

    async def _send_temp_session(self, user_id: str, group_id: str, text: str) -> bool:
        """用适配器的 ``send_private_msg``（带 ``group_id``）发群临时会话。

        这是 OneBot/NapCat 里"给非好友发一条私聊"的标准做法：消息从一个共同群
        发起临时会话。能不能成功取决于该群是否允许成员发起临时会话（群设置里能关），
        失败只记一条日志——群道贺已经发过了，不因为它失败而变成"没道贺"。
        """

        try:
            api = self._plugin.adapter_api_name("adapter.napcat.message.send_private_msg")
            result = await self._plugin.ctx.api.call(
                api,
                params={
                    "user_id": str(user_id),
                    "group_id": str(group_id),
                    "message": [{"type": "text", "data": {"text": text}}],
                },
            )
        except Exception as exc:
            self._logger.warning("给 %s 发群临时会话异常: %s", user_id, exc)
            return False

        if _api_ok(result):
            self._logger.info("已给非好友 %s 用群 %s 的临时会话发了一条私聊", user_id, group_id)
            return True
        self._logger.warning(
            "给 %s 发群临时会话失败（多半是这个群没允许发起临时会话）: %s",
            user_id,
            str(result)[:200],
        )
        return False

    async def _resolve_stream(self, *, group_id: str = "", user_id: str = "") -> str:
        """找到（或现开）目标聊天流，返回 stream_id；失败返回空串。"""

        chat = self._plugin.ctx.chat
        platform = "qq"
        try:
            if group_id:
                stream_id = _stream_id_of(await chat.get_stream_by_group_id(str(group_id)))
                if stream_id:
                    return stream_id
                created = await chat.open_session(
                    platform=platform, chat_type="group", group_id=str(group_id)
                )
                stream_id = _stream_id_of(created)
                if not stream_id:
                    self._logger.warning("无法为群 %s 打开聊天流", group_id)
                return stream_id

            if user_id:
                stream_id = _stream_id_of(await chat.get_stream_by_user_id(str(user_id)))
                if stream_id:
                    return stream_id
                created = await chat.open_session(
                    platform=platform, chat_type="private", user_id=str(user_id)
                )
                stream_id = _stream_id_of(created)
                if not stream_id:
                    self._logger.warning("无法为用户 %s 打开私聊流", user_id)
                return stream_id
        except Exception as exc:
            self._logger.warning(
                "解析聊天流失败(group=%s user=%s): %s", group_id, user_id, exc
            )
        return ""

    async def _send_text(self, text: str, stream_id: str) -> bool:
        try:
            result = await self._plugin.ctx.send.text(text, stream_id)
        except Exception as exc:
            self._logger.warning("发送生日祝福失败(stream=%s): %s", stream_id, exc)
            return False
        if isinstance(result, dict):
            return bool(result.get("sent", result.get("success")))
        return bool(result)

    async def _compose_greeting(self, fallback_text: str, person: dict[str, Any]) -> str:
        """把生日信息注入生成提示，让模型自己写这句祝福。

        默认走 ``greeting.llm_task_name``（replyer）——也就是用麦麦平时说话的那套模型来
        决定"怎么说"。任何异常、失败或空输出都回退到 ``greeting_template`` 渲染结果，
        保证道贺不会因为模型抽风而漏发。
        """

        config = self._plugin.config.greeting
        name = text_utils.display_name(person)
        age = text_utils.age_of(person)
        today = date.today()

        facts = [f"对象昵称：{name}"]
        if age is not None:
            facts.append(f"对方年龄：{age} 岁")
        facts.append(f"今天日期：{today.month} 月 {today.day} 日")

        prompt = (
            "现在是给群里的人道贺生日的时刻，请用你自己的话写一句生日祝福。\n"
            "要求：只输出这一句祝福本身；口语化、别端着、别用客服腔；"
            "一句话就够（二十字上下）；不要引号、不要解释、不要换行。\n"
            + "\n".join(facts)
            + f"\n可参考的直白说法（不要照抄）：{fallback_text}"
        )

        try:
            result = await self._plugin.ctx.llm.generate(
                prompt,
                task_name=config.llm_task_name,
                temperature=config.llm_temperature,
                max_tokens=config.llm_max_tokens or None,
            )
        except Exception as exc:
            self._logger.warning("文案生成调用失败，回退兜底文案: %s", exc)
            return fallback_text

        if not (isinstance(result, dict) and result.get("success")):
            self._logger.warning("文案生成未成功，回退兜底文案: %s", str(result)[:200])
            return fallback_text

        composed = _sanitize_greeting(str(result.get("response") or ""))
        if not composed:
            self._logger.warning(
                "文案生成返回空内容，回退兜底文案（常见原因：greeting.llm_max_tokens=%s "
                "太小，模型把额度用在思考上；日志里的「达到最大输出 token 限制」可确认）",
                config.llm_max_tokens,
            )
            return fallback_text
        self._logger.debug("文案由 %s 生成：%s", config.llm_task_name, composed)
        return composed

    def _in_window(self) -> bool:
        config = self._plugin.config.greeting
        hour = datetime.now().hour
        return config.hour_start <= hour < config.hour_end
