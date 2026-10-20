"""麦麦祝你生日快乐呀（maibot-birthday-plugin）插件入口。

模块分工：

- ``core/config.py``    强类型配置 Schema
- ``core/store.py``     本地 JSON 缓存 + 按天道贺去重
- ``core/napcat.py``    NapCat 名片生日读取
- ``core/collector.py`` 好友/群成员采集与合并
- ``core/scheduler.py`` 定时采集与寿星当天主动道贺
- ``core/text.py``      文案渲染

本文件只做三件事：生命周期管理、把能力暴露给麦麦（Hook / Tool / Command / API）、
以及指令参数解析。

对外暴露的三个入口：

1. ``maisaka.replyer.before_request`` Hook —— 把"今天谁过生日"注入本次回复要求，
   由麦麦在**本次回复里用自己的话**说（Hook 只在麦麦本来就要回复时触发）。
2. ``birthday_query`` Tool —— 让麦麦在被问到"谁快过生日了"时能自己查。
3. ``/birth`` 指令（``/生日`` 同样可用）—— help / list / recheck / group / custom / del。

分工原则：**"发不发"由插件决定（确定性），"怎么说"交给模型**。

- 主动道贺由 ``core/scheduler.py`` 直接投递，不经过麦麦"要不要回复"的判断，
  所以不会出现"它今天不想说话就不发"。
- 文案默认由 ``greeting.llm_task_name``（replyer）生成，失败自动回退
  ``greeting_template``，绝不因此漏发。
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from maibot_sdk import (
    API,
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    HookHandler,
    MaiBotPlugin,
    Tool,
)
from maibot_sdk.types import (
    ErrorPolicy,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

from .core import text as text_utils
from .core.collector import BirthdayCollector
from .core.config import BirthdayConfig
from .core.napcat import (
    NapCatBirthdaySource,
    extract_birthday,
    normalize_group_id,
    normalize_user_id,
    pick_name,
)
from .core.scheduler import BirthdayScheduler
from .core.store import BirthdayStore


STORE_FILENAME = "birthdays.json"

# 手工登记时最多探这么多个白名单群（每个群一次成员列表调用），防止一个指令打出去几十次请求
MAX_GROUP_PROBES = 8

HELP_TEXT = """【麦麦祝你生日快乐呀】指令（也可以写成 /生日）
/birth help                        这条帮助
/birth list                        发送缓存里的全部生日
—— 以下需要管理员 ——
/birth recheck                     重新读取一次生日（好友 + 白名单群成员）
/birth group <群号>                 把群号加进白名单，并打开群成员采集
/birth custom <QQ号> <日期> [名字]   手工登记（自动匹配白名单群/好友）
/birth del <QQ号>                   删掉这个 QQ 的缓存记录"""

# 单条消息的软上限：QQ 对单条文本有长度限制，超长必须拆成几条发，
# 否则要么被平台截断、要么整条发不出去（“view all”在人多时一定会碰到）。
CHUNK_CHARS = 1200
MAX_CHUNKS = 6


def _split_message(text: str, chunk_chars: int = CHUNK_CHARS, max_chunks: int = MAX_CHUNKS) -> list[str]:
    """按行把长回复切成若干条消息；超出上限时在最后一条注明被截断。"""

    if len(text) <= chunk_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if current and size + len(line) + 1 > chunk_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
            if len(chunks) >= max_chunks:
                break
        current.append(line)
        size += len(line) + 1
    if current and len(chunks) < max_chunks:
        chunks.append("\n".join(current))
    if not chunks:
        return [text]
    if sum(len(chunk) + 1 for chunk in chunks) < len(text):
        # 截断说明也要算进这一条的长度预算，否则最后一条会超出上限
        note = "\n…（太长已截断，可用 /birth view <关键词> 缩小范围）"
        while len(chunks[-1].splitlines()) > 1 and len(chunks[-1]) + len(note) > chunk_chars:
            chunks[-1] = "\n".join(chunks[-1].splitlines()[:-1])
        chunks[-1] += note
    return chunks


class BirthdayPlugin(MaiBotPlugin):
    """麦麦祝你生日快乐呀：群友生日采集与主动道贺插件。"""

    config_model = BirthdayConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: BirthdayStore | None = None
        self._source: NapCatBirthdaySource | None = None
        self._collector: BirthdayCollector | None = None
        self._scheduler: BirthdayScheduler | None = None
        self._ready = False

    # ==================== 生命周期 ====================

    async def on_load(self) -> None:
        """建缓存、装数据源、起后台调度。"""

        data_dir = Path(self.ctx.paths.data_dir)
        self._store = BirthdayStore(data_dir / STORE_FILENAME, self.ctx.logger)
        self._store.load()
        if self._store.prune_greeted():
            self._store.save()

        self._source = NapCatBirthdaySource(self)
        self._collector = BirthdayCollector(self, self._store, self._source)
        self._scheduler = BirthdayScheduler(self, self._store, self._collector)
        self._scheduler.start()
        self._ready = True

        self.ctx.logger.info(
            "麦麦祝你生日快乐呀插件已加载：缓存 %d 人，道贺 %s，文案 %s，适配器 %s，数据文件 %s",
            len(self._store.people),
            self._greeting_flags(),
            (
                f"{self.config.greeting.llm_task_name} 生成"
                if self.config.greeting.use_llm_polish
                else "模板原文"
            ),
            self.config.plugin.adapter_plugin_id or "（短名自动定位）",
            self._store.path,
        )

    async def on_unload(self) -> None:
        """停调度、落盘。"""

        self._ready = False
        if self._scheduler is not None:
            await self._scheduler.stop()
        if self._store is not None:
            self._store.save()
        self._scheduler = None
        self._collector = None
        self._source = None
        self._store = None
        self.ctx.logger.info("麦麦祝你生日快乐呀插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        """配置热更新。

        调度器每轮都重新读 ``self.config``，所以这里不需要推送新值；
        只记录一次，方便排查"改了配置没生效"之类的问题。
        """

        if scope != CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.debug("忽略非本插件范围的配置更新: %s", scope)
            return
        self.ctx.logger.info(
            "麦麦祝你生日快乐呀配置已更新(version=%s)：道贺 %s，文案 %s，采集间隔 %s 小时",
            version,
            self._greeting_flags(),
            (
                f"{self.config.greeting.llm_task_name} 生成"
                if self.config.greeting.use_llm_polish
                else "模板原文"
            ),
            self.config.source.refresh_interval_hours,
        )

    def _greeting_flags(self) -> str:
        """道贺开关的简短描述，用在日志里。"""

        greeting = self.config.greeting
        parts = []
        if greeting.greet_in_group:
            parts.append("群聊" + ("@寿星" if greeting.at_person else ""))
        if greeting.greet_in_private:
            parts.append("私聊好友")
        return "+".join(parts) if parts else "仅注入不主动发"

    # ==================== 适配器 API 名解析 ====================

    def adapter_api_name(self, short_name: str) -> str:
        """解析适配器 API 调用名。

        配置了 ``plugin.adapter_plugin_id`` 时拼成 ``<插件ID>.<短名>`` 全名：
        Host 按短名解析时若同时存在 NapCat 与 SnowLuma 两个提供方，会直接报
        "名称不唯一"而不是随便挑一个，全名走精确匹配分支可以绑定到指定适配器。
        """

        provider = self.config.plugin.adapter_plugin_id
        return f"{provider}.{short_name}" if provider else short_name

    # ==================== Hook：注入回复上下文 ====================

    @HookHandler(
        "maisaka.replyer.before_request",
        name="birthday_prompt_injector",
        description="在麦麦生成回复前，把今天的寿星信息追加进本次回复要求",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_replyer_before_request(self, **kwargs: Any) -> dict[str, Any]:
        """把生日提醒拼进 ``extra_prompt``。

        只改提示词、不做任何 IO，异常一律吞掉（``error_policy=SKIP`` 兜底），
        绝不允许因为生日数据的问题影响麦麦正常回消息。
        """

        if not self._ready:
            return {"action": "continue", "modified_kwargs": kwargs}

        try:
            notice = self._build_prompt_notice()
        except Exception as exc:
            self.ctx.logger.debug("构建生日提醒失败，跳过注入: %s", exc)
            return {"action": "continue", "modified_kwargs": kwargs}

        if not notice:
            return {"action": "continue", "modified_kwargs": kwargs}

        existing = str(kwargs.get("extra_prompt") or "")
        # 幂等：重试/重生成时 kwargs 里可能已经带上了同一段提醒
        if notice in existing:
            return {"action": "continue", "modified_kwargs": kwargs}

        modified = dict(kwargs)
        modified["extra_prompt"] = f"{existing}\n{notice}" if existing.strip() else notice
        self.ctx.logger.debug("已注入生日提醒: %s", notice.replace("\n", " / "))
        return {"action": "continue", "modified_kwargs": modified}

    def _build_prompt_notice(self) -> str:
        """生成注入文本；没有可说的内容时返回空串。"""

        if self._store is None or not self.config.prompt.inject_prompt:
            return ""

        prompt_config = self.config.prompt
        today = date.today()
        parts: list[str] = []

        people = self._store.people_on(today.month, today.day)
        if people and prompt_config.skip_greeted:
            # 主动道贺已经送达的人不再注入：一次道贺就够，避免麦麦当天反复重提。
            # 没送达（或两个道贺开关都关）的人不会被标记，注入照常发生。
            people = [
                person
                for person in people
                if not self._store.was_greeted(str(person.get("key") or ""), today)
            ]
        if people:
            parts.append(
                text_utils.render_prompt(
                    prompt_config.prompt_template,
                    people,
                    max_names=prompt_config.max_names,
                )
            )

        upcoming_days = int(prompt_config.include_upcoming_days)
        if upcoming_days > 0:
            upcoming = self._store.upcoming(today, upcoming_days)
            if upcoming:
                upcoming_names = "、".join(
                    f"{text_utils.display_name(person)}"
                    f"（{offset} 天后，{text_utils.format_month_day(person)}）"
                    for person, offset in upcoming[: prompt_config.max_names]
                )
                parts.append(f"【生日预告】{upcoming_names}。")

        return "\n".join(part for part in parts if part).strip()

    # ==================== Tool：让麦麦自己查 ====================

    @Tool(
        "birthday_query",
        brief_description="查询群友生日",
        description=(
            "查询本地缓存的群友生日。不传关键词时返回今天过生日的人以及最近 7 天的生日预告；"
            "传关键词时按昵称或 QQ 号搜索某个人的生日。"
            "数据来自插件定期从 QQ 名片采集的本地缓存，拿不到的生日说明对方没公开。"
        ),
        parameters=[
            ToolParameterInfo(
                name="keyword",
                param_type=ToolParamType.STRING,
                description="要搜索的昵称或 QQ 号；留空则返回今天和最近 7 天的生日",
                required=False,
            )
        ],
    )
    async def handle_birthday_query(self, **kwargs: Any) -> dict[str, Any]:
        if self._store is None:
            return {"content": "生日缓存还没准备好，请稍后再试。"}

        keyword = str(kwargs.get("keyword") or "").strip()
        if not keyword:
            return {"content": self._today_summary()}

        matched = self._store.search(keyword, limit=10)
        if not matched:
            return {
                "content": (
                    f"没有找到和「{keyword}」相关的生日记录。"
                    "如果这个人确实填了生日，可能需要先刷新缓存（/birth refresh）。"
                )
            }
        today = date.today()
        lines = [self._format_person_line(person, today) for person in matched]
        return {"content": "匹配到的生日记录：\n" + "\n".join(lines)}

    # ==================== Command：人来查 ====================

    @Command(
        "birthday",
        description="群友生日：/birth help 看指令 / list 看缓存 / recheck 重新读取 / group 加群 / custom 手工登记 / del 删缓存",
        pattern=r"(?i)^/?(?:birth\b|生日)",
    )
    async def handle_birthday_command(self, **kwargs: Any) -> tuple[bool, str, bool]:
        """``/birth`` 指令入口：算回复 → 自己发送 → 把文本交回给日志。

        ⚠️ **Host 不会替插件发命令回复**。实测 ``src/chat/message_receive/bot.py`` 的
        ``_handle_command_processing_result`` 只把返回文本写进日志
        （"命令处理完成，跳过后续消息处理: ..."），Host 自己的内置命令也都是自己调
        ``text_to_stream`` 发的（例如权限不足时先发"你没有权限使用此命令。"，
        再 ``return True, "没有权限", False``）。所以这里必须自己 ``send.text``。
        """

        if self._store is None:
            return True, "生日插件还没准备好，请稍后再试。", True

        success, reply, intercept = await self._birthday_command_reply(kwargs)
        await self._send_command_reply(reply, str(kwargs.get("stream_id") or ""))
        # 返回文本照旧带回去：留给日志和 chat.command.after_execute hook 看。
        # 万一将来 Host 改成自动发送这段文本，用户会收到两条——届时把返回值换成 "" 即可。
        return success, reply, intercept

    async def _send_command_reply(self, reply: str, stream_id: str) -> None:
        """把指令回复发到当前会话；过长时拆成几条，发不出去只记日志。"""

        if not reply:
            return
        if not stream_id:
            self.ctx.logger.debug("指令回复没有 stream_id，跳过发送: %s", reply[:60])
            return
        for index, chunk in enumerate(_split_message(reply)):
            if index:
                # 保序：连着发多条时给 QQ 端一点时间，避免乱序
                await asyncio.sleep(0.3)
            try:
                await self.ctx.send.text(chunk, stream_id)
            except Exception as exc:
                self.ctx.logger.warning("指令回复发送失败: %s", exc)
                return

    async def _birthday_command_reply(self, kwargs: dict[str, Any]) -> tuple[bool, str, bool]:
        """（实际实现）只负责算出回复文本，不负责发送。"""

        if self._store is None:
            return True, "生日插件还没准备好，请稍后再试。", True

        text = str(kwargs.get("text") or "").strip()
        body = text.lstrip("/").strip()
        # 前缀去掉命令名本身（birth / 生日 两种写法都支持）
        for prefix in ("birth", "生日"):
            if body.lower().startswith(prefix.lower()):
                body = body[len(prefix) :]
                break
        tokens = body.replace("：", " ").replace(",", " ").replace("，", " ").split()
        sub = tokens[0].lower() if tokens else ""
        args = tokens[1:]

        # 只保留这几个子命令：help / list / recheck / group / custom / del
        if sub in ("", "help", "帮助", "?", "-h"):
            return True, HELP_TEXT, True
        if sub in ("list", "列表"):
            return True, self._list_summary(), True

        # ---- 以下需要管理员 ----
        if sub in ("recheck", "重新读取", "重读"):
            if not self._is_admin(kwargs):
                return True, "重新读取生日需要管理员权限。", True
            return True, await self._recheck(), True
        if sub in ("group", "群"):
            if not self._is_admin(kwargs):
                return True, "添加群号需要管理员权限。", True
            return True, self._add_group(args), True
        if sub in ("custom", "自定义", "登记"):
            if not self._is_admin(kwargs):
                return True, "手工登记需要管理员权限。", True
            return True, await self._custom_birthday(args), True
        if sub in ("del", "delete", "删除"):
            if not self._is_admin(kwargs):
                return True, "删除缓存需要管理员权限。", True
            return True, self._delete_cached(" ".join(args)), True

        return True, f"不认识的子命令：{sub}\n\n{HELP_TEXT}", True

    # ==================== API：给别的插件用 ====================

    @API(
        "birthday.list",
        description="读取生日缓存（今天 + 未来若干天）",
        version="1",
        public=True,
    )
    async def api_birthday_list(self, **kwargs: Any) -> dict[str, Any]:
        if self._store is None:
            return {"success": False, "error": "not_ready"}

        today = date.today()
        try:
            within_days = int(kwargs.get("within_days") or 7)
        except Exception:
            within_days = 7
        return {
            "success": True,
            "updated_at": self._store.updated_at,
            "total": len(self._store.people),
            "today": [
                self._public_person(person)
                for person in self._store.people_on(today.month, today.day)
            ],
            "upcoming": [
                {**self._public_person(person), "days_until": offset}
                for person, offset in self._store.upcoming(today, max(0, min(within_days, 60)))
            ],
        }

    # ==================== 指令实现 ====================

    def _is_admin(self, kwargs: dict[str, Any]) -> bool:
        if kwargs.get("is_local_operator"):
            return True
        user_id = str(kwargs.get("user_id") or "").strip()
        if not user_id:
            return False
        allowed = {str(item).strip() for item in self.config.access.admin_user_ids}
        return user_id in allowed

    def _today_summary(self) -> str:
        assert self._store is not None
        today = date.today()
        lines = [f"📅 今天是 {today.month} 月 {today.day} 日。"]

        people = self._store.people_on(today.month, today.day)
        if people:
            lines.append(f"🎂 今天生日（{len(people)} 人）：")
            lines.extend(self._format_person_line(person, today) for person in people)
        else:
            lines.append("今天没有记录到有人过生日。")

        upcoming = self._store.upcoming(today, 7)
        if upcoming:
            lines.append("⏳ 最近 7 天：")
            lines.extend(
                self._format_person_line(person, today) for person, _ in upcoming
            )
        if not self._store.people:
            lines.append(
                "（本地还没有任何生日数据：可用 /birth refresh 采集，"
                "并确认 plugin.adapter_plugin_id 指向 NapCat 适配器）"
            )
        return "\n".join(lines)

    def _list_summary(self) -> str:
        """列出缓存里的全部生日（``/birth view all``）。"""

        assert self._store is not None
        total = len(self._store.people)
        if not total:
            return "缓存里还没有生日记录，先执行 /birth refresh 采集，或用 /birth set 手工登记。"
        today = date.today()
        lines = [f"共 {total} 人（缓存里的全部，按日期排序）："]
        lines.extend(
            self._format_person_line(person, today) for person in self._store.all_people()
        )
        return "\n".join(lines)



    async def _recheck(self) -> str:
        """``/birth recheck``：立刻重新读取一次生日（好友 + 白名单群成员）。"""

        assert self._scheduler is not None
        try:
            stats = await self._scheduler.refresh_if_needed(force=True)
        except Exception as exc:
            return f"重新读取失败：{exc}"
        if stats is None:
            return "已经有一次采集在进行中，请稍后再试。"
        return (
            "✅ 重新读取完成\n"
            f"好友 {stats.get('friends_total', 0)} 条 / 命中生日 {stats.get('friends_with_birthday', 0)} 人\n"
            f"群 {stats.get('groups_planned', 0) or stats.get('groups_discovered', 0)} 个 / "
            f"成员检查 {stats.get('group_members_checked', 0)} 人"
            f"（实际查名片 {stats.get('strangers_queried', 0)} 次）\n"
            f"新增或更新 {stats.get('saved', 0)} 条，缓存共 {stats.get('total_cached', 0)} 人，"
            f"耗时 {stats.get('elapsed_ms', 0)}ms，失败 {stats.get('errors', 0)} 次"
        )

    def _add_group(self, args: list[str]) -> str:
        """``/birth group <群号>``：加进群白名单，并打开群成员采集。

        白名单同时决定"采哪些群"和"去哪些群道贺"，所以加一个群 = 允许插件碰这个群；
        但只有打开群成员采集，插件才知道群里谁哪天生日，否则群道贺永远不会触发。
        """

        tokens = [item for item in args if item]
        if tokens and tokens[0].lower() in ("group", "群", "群号"):
            tokens = tokens[1:]
        if not tokens:
            return "用法：/birth group <群号>"

        raw = tokens[0]
        group_id = "".join(ch for ch in raw if ch.isdigit())
        if not 5 <= len(group_id) <= 12:
            return f"群号看起来不对：{raw}（要 5-12 位数字，例如 /birth group 123456789）"

        greeting = self.config.greeting
        source = self.config.source
        groups = [str(item) for item in greeting.announce_groups]
        if "*" in groups:
            return "群白名单里已经有 *（机器人所在的全部群），不用再单独加。"
        if group_id in groups:
            return f"{group_id} 已经在白名单里了（当前：{'、'.join(groups) or '空'}）。"

        new_groups = [*groups, group_id]
        array = "[" + ", ".join(f'"{item}"' for item in new_groups) + "]"
        updates = {"announce_groups": (array, "greeting")}
        open_members = not bool(source.include_group_members)
        if open_members:
            updates["include_group_members"] = ("true", "source")

        ok, detail = self._write_config_values(updates)
        # 内存里也立刻生效，不必等 Host 把配置推回来
        try:
            greeting.announce_groups = new_groups
            if open_members:
                source.include_group_members = True
        except Exception as exc:
            self.ctx.logger.warning("运行中配置热更新失败（等 Host 重载）: %s", exc)

        out = [f"✅ 已加进群白名单：{group_id}", f"群白名单：{'、'.join(new_groups)}"]
        if open_members:
            out.append("群成员采集：已打开（原来关着；不采集就不知道群里谁哪天生日）")
        if ok:
            out.append(f"已写入 {detail}，WebUI 里也能看到")
        else:
            out.append(f"⚠️ 配置没写进去（{detail}）：本次运行有效，重启后会丢，请在 WebUI 手动加")
        out.append("下一步：/birth recheck 立刻采一次（成员多时要等一会儿）")
        return "\n".join(out)

    async def _custom_birthday(self, args: list[str]) -> str:
        """``/birth custom <QQ号> <日期> [名字]``：手工写一条生日记录。

        主键只认 QQ 号：名录里已有的人保留原有的来源/好友标记/群关系，
        新人标成 ``source=manual``——采集的 ``drop_missing`` 只清理 friend 来源，
        所以手工记录既不会被采集覆盖，也不会被"人没了就删"清掉。

        还会顺手匹配一次身份：在哪些白名单群里（决定群道贺发不发得出去）、
        是不是好友（决定私聊道贺发不发得出去），匹配结果一起写进记录。
        """

        assert self._store is not None
        if len(args) < 2:
            return "用法：/birth custom <QQ号> <日期> [名字]\n例如：/birth custom 123456789 05-20"

        qq = "".join(ch for ch in args[0] if ch.isdigit())
        if not 5 <= len(qq) <= 12:
            return f"QQ 号看起来不对：{args[0]}（要 5-12 位数字，例如 /birth custom 123456789 05-20）"
        parsed = text_utils.parse_month_day(args[1])
        if parsed is None:
            return f"日期「{args[1]}」认不出来，写成 05-20 或 5/20 这种形式。"
        month, day = parsed
        typed_name = " ".join(args[2:]).strip()

        existing = self._store.get_person(qq) or {}
        card_name = ""
        card_birthday: tuple[int, int, int] | None = None
        if self._source is not None and not (typed_name and existing.get("name")):
            # 顺手查一次名片：既能补上真昵称，也能提醒"名片上的日期和你给的日期不一样"
            info = await self._source.fetch_stranger(qq)
            if isinstance(info, dict):
                card_name = pick_name(info)
                card_birthday = extract_birthday(info)

        # 身份匹配：在不在白名单群、是不是好友
        matched, probed, probe_failed = await self._match_whitelisted_groups(qq)
        is_friend_before = bool(existing.get("is_friend"))
        friend_flag = True if is_friend_before else await self._friend_status(qq)

        merged_groups = sorted(
            {
                *[str(item) for item in (existing.get("groups") or [])],
                *matched,
            }
        )
        name = typed_name or str(existing.get("name") or "").strip() or card_name or qq
        changed = self._store.upsert_person(
            {
                "key": qq,
                "user_id": qq,
                "name": name,
                "month": month,
                "day": day,
                "year": int(existing.get("year") or 0),
                "source": str(existing.get("source") or "manual"),
                "is_friend": is_friend_before or friend_flag is True,
                "groups": merged_groups,
            }
        )
        self._store.save()

        out = [f"{'已更新' if changed else '无变化'}：{name}（{qq}）→ {month:02d}-{day:02d}"]
        if card_birthday and (card_birthday[0], card_birthday[1]) != (month, day):
            out.append(f"⚠️ 名片上写的是 {card_birthday[0]:02d}-{card_birthday[1]:02d}，已按你给的日期记")
        if matched:
            out.append(f"在白名单群：{'、'.join(matched)}（已写进群关系，群道贺会发到这里）")
        elif not probed:
            out.append("白名单群是空的（announce_groups 没配群）：群道贺不会发；用 /birth group <群号> 加")
        elif len(probe_failed) == len(probed):
            out.append(
                f"白名单群都没查到（{'、'.join(probe_failed)}，适配器可能没响应）：群关系暂缺，适配器正常后重发一次即可"
            )
        else:
            checked = "、".join(item for item in probed if item not in probe_failed)
            out.append(f"不在白名单群（{checked}）里：群道贺不会发，只出现在 list 和麦麦的聊天上下文")
        if friend_flag is False:
            temp_ok = bool(self.config.greeting.private_temp_session)
            if not temp_ok:
                out.append("不是好友：私聊不会发（private_temp_session 关着，只有群道贺）")
            elif matched:
                out.append("不是好友：私聊改走群临时会话（前提是那个群允许成员发起临时会话）")
            else:
                out.append("不是好友，也没有白名单群可借：私聊发不出去（临时会话需要共同群）")
        elif friend_flag is True and not is_friend_before:
            out.append("是好友：私聊道贺也会发")
        if not existing:
            out.append("这是手工记录（source=manual），采集不会覆盖或清理它")
            if not typed_name and not card_name:
                out.append("没读到名片，显示名先用 QQ 号，等采集到真人就补上昵称")
        out.append(f"缓存共 {len(self._store.people)} 人（/birth list 查看）")
        return "\n".join(out)

    async def _match_whitelisted_groups(self, qq: str) -> tuple[list[str], list[str], list[str]]:
        """查这个 QQ 在哪些白名单群里，返回 ``(命中的群, 查过的群, 没查到的群)``。

        每个群要拉一次成员列表（一次 API 调用）；白名单是 ``*`` 时先取机器人所在的群，
        再只探前 ``MAX_GROUP_PROBES`` 个，避免一个指令打出去几十次请求。
        """

        if self._source is None:
            return [], [], []
        whitelist = [str(item) for item in self.config.greeting.announce_groups]
        if "*" in whitelist:
            discovered = await self._source.fetch_groups()
            if discovered is None:
                return [], ["*"], ["*"]
            targets = [
                group_id
                for item in discovered
                if (group_id := normalize_group_id(item))
            ][:MAX_GROUP_PROBES]
        else:
            targets = whitelist[:MAX_GROUP_PROBES]

        matched: list[str] = []
        failed: list[str] = []
        for group_id in targets:
            members = await self._source.fetch_group_members(group_id)
            if members is None:
                failed.append(group_id)
                continue
            if any(normalize_user_id(item) == qq for item in members):
                matched.append(group_id)
        return matched, targets, failed

    async def _friend_status(self, qq: str) -> bool | None:
        """这个 QQ 是不是好友；查不到返回 ``None``（不写进记录）。"""

        if self._source is None:
            return None
        friends = await self._source.fetch_friends()
        if friends is None:
            return None
        return any(normalize_user_id(item) == qq for item in friends)

    def _config_path(self) -> Path:
        """插件配置文件路径（Host 约定：插件目录下的 config.toml）。"""

        return Path(__file__).resolve().parent / "config.toml"

    def _write_config_values(self, updates: dict[str, tuple[str, str]]) -> tuple[bool, str]:
        """把配置项写回 ``config.toml``，返回 ``(是否成功, 路径或失败原因)``。

        Host 只有 ``config.get*`` 三个只读能力、没有"写配置"，所以想持久化只能改文件。
        做法：只替换目标键那一行（没有这个键就插到对应 section 下），其余内容原样保留；
        先写临时文件再 ``os.replace`` 原子替换，避免 Host 正好读到半个文件。
        """

        path = self._config_path()
        if not path.is_file():
            return False, f"没找到配置文件 {path}"
        try:
            text = path.read_text(encoding="utf-8")
            for key, (value, section) in updates.items():
                text = self._set_toml_value(text, key, value, section)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8", newline="\n")
            os.replace(tmp, path)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return True, str(path)

    @staticmethod
    def _set_toml_value(text: str, key: str, value: str, section: str) -> str:
        """替换 ``key = ...`` 那一行；没有这个键就插到 ``[section]`` 标题后面。"""

        replaced, count = re.subn(
            rf"(?m)^{re.escape(key)}\s*=\s*.*$",
            lambda match: f"{key} = {value}",
            text,
            count=1,
        )
        if count:
            return replaced
        inserted, count = re.subn(
            rf"(?m)^\[{re.escape(section)}\]\s*$",
            lambda match: f"{match.group(0)}\n{key} = {value}",
            text,
            count=1,
        )
        if count:
            return inserted
        return text.rstrip("\n") + f"\n\n[{section}]\n{key} = {value}\n"

    def _delete_cached(self, raw: str) -> str:
        """``/birth del <QQ号>``：精确删掉这个 QQ 的缓存记录（不做模糊匹配）。"""

        assert self._store is not None
        qq = "".join(ch for ch in str(raw or "").strip() if ch.isdigit())
        if not qq:
            return "用法：/birth del <QQ号>"
        person = self._store.get_person(qq)
        if person is None:
            return f"缓存里没有 QQ {qq} 的记录（/birth list 可以看都有谁）。"
        name = text_utils.display_name(person)
        removed = self._store.remove_keys([qq])
        self._store.save()
        return f"已删除：{name}（{qq}），还剩 {len(self._store.people)} 条缓存。"





    # ==================== 小工具 ====================

    def _format_person_line(self, person: dict[str, Any], today: date | None = None) -> str:
        current = today or date.today()
        name = text_utils.display_name(person)
        user_id = str(person.get("user_id") or "")
        month = int(person.get("month") or 0)
        day = int(person.get("day") or 0)
        age = text_utils.age_of(person, current)
        age_text = f"，{age} 岁" if age is not None else ""

        remaining = _days_until(month, day, current)
        if remaining == 0:
            suffix = "（今天）"
        elif remaining is not None:
            suffix = f"（{remaining} 天后）"
        else:
            suffix = ""

        who = f"{name}（{user_id}）" if user_id else name
        return f"· {who} {month:02d}-{day:02d}{age_text}{suffix}"

    @staticmethod
    def _public_person(person: dict[str, Any]) -> dict[str, Any]:
        return {
            "user_id": str(person.get("user_id") or ""),
            "name": text_utils.display_name(person),
            "month": int(person.get("month") or 0),
            "day": int(person.get("day") or 0),
            "year": int(person.get("year") or 0),
            "source": str(person.get("source") or ""),
        }


def _days_until(month: int, day: int, today: date) -> int | None:
    """距离下一次该月日还有多少天（今天为 0）；日期非法返回 ``None``。"""

    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return (candidate - today).days
    return None


def create_plugin() -> BirthdayPlugin:
    """插件入口：Runner 通过这个工厂函数拿到插件实例。"""

    return BirthdayPlugin()
