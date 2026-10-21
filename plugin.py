"""麦麦祝你生日快乐呀：采集好友 / 群成员的生日，寿星当天主动道贺，并把「今天谁过生日」注入麦麦的回复上下文。

单文件插件（标准布局：plugin.py + _manifest.json + README.md + LICENSE），按顺序分七段：

1. **配置模型** —— 强类型 Schema，Runner 据此生成 config.toml 与 WebUI 表单
2. **文案与格式化** —— 生日文案渲染、占位符替换、名字与日期格式化
3. **本地存储** —— birthdays.json 缓存 + 按天道贺去重
4. **NapCat 采集** —— 从名片读 birthday_year / birthday_month / birthday_day
5. **采集器** —— 好友与群成员合并去重
6. **调度与道贺** —— 定时采集、寿星当天在群里 @ 或私聊道贺
7. **插件主体** —— 生命周期、把能力暴露给麦麦（Hook / Tool / Command / API）

对外暴露的三个入口：

1. ``maisaka.replyer.before_request`` Hook —— 把"今天谁过生日"注入本次回复要求，
   由麦麦在**本次回复里用自己的话**说（Hook 只在麦麦本来就要回复时触发）。
2. ``birthday_query`` Tool —— 让麦麦在被问到"谁快过生日了"时能自己查。
3. ``/birth`` 指令（``/生日`` 同样可用）—— help / list / recheck / group / custom / del。

分工原则：**"发不发"由插件决定（确定性），"怎么说"交给模型**。
"""

from __future__ import annotations

from __future__ import annotations

import logging
from typing import Any, Literal
from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator
import re
from datetime import date
from typing import Any, Iterable
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
import asyncio
from datetime import date, datetime
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


# ============================================================================
# 配置模型（原 core/config.py）
# ============================================================================

_logger = logging.getLogger(__name__)


# 配置 schema 版本（与插件版本独立，仅在配置字段结构变更时手动上调）
CONFIG_SCHEMA_VERSION = "1.0.0"


# 默认绑定的适配器插件 ID。
#
# 本插件读取的是 NapCat 名片里的 birthday_year / birthday_month / birthday_day。
# SnowLuma 适配器的资料拉取走 OIDB 0xFE1_2，其属性键目录是
# 102 签名 / 103 备注 / 105 等级 / 20002 昵称 / 20009 性别 / 20037 年龄 / 27394 QID，
# 类型 ``UserProfileInfo`` 里也没有生日字段——所以数据只能从 NapCat 适配器取。
#
# 同时启用 NapCat 与 SnowLuma 两个兼容适配器时，Host 按短名（``adapter.napcat.*``）
# 解析会遇到多个提供方并直接拒绝调用；写成全名走 Host 的精确匹配分支。
DEFAULT_ADAPTER_PLUGIN_ID = "maibot-team.napcat-adapter"

ANNOUNCE_MODES: tuple[str, ...] = ("off", "prompt_only", "group", "private", "both")
# 仅用于兼容旧配置的 announce_mode；新配置用 greet_in_group / greet_in_private 两个开关
LLM_TASK_NAMES: tuple[str, ...] = ("replyer", "utils", "planner")


def _normalize_id_list(value: Any) -> list[str]:
    """把 QQ 号 / 群号列表统一归一化为去空白的字符串列表。

    pydantic v2 不会把 int 自动转成 str：用户在 config.toml 里写
    ``announce_groups = [123456]``（TOML 里裸数字很自然）会让 ``list[str]`` 校验
    直接失败——启动时插件被标记 FAILED 不激活，热更新时整次更新被拒绝、
    静默保留旧配置。这里在校验前先转字符串并丢弃空项，与运行时
    ``str(x).strip()`` 的口径一致（``"*"`` 这类通配符也会原样保留）。
    """
    if value is None:
        return []
    if isinstance(value, (str, int)):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        if item is None or isinstance(item, bool):
            continue
        text = str(item).strip()
        if text:
            result.append(text)
    return result


class PluginSection(PluginConfigBase):
    """插件基础元信息。"""

    __ui_label__ = "插件设置"

    name: str = Field(
        default="maibot_birthday_plugin",
        description="插件名称",
        json_schema_extra={"disabled": True},
    )
    config_version: str = Field(
        default=CONFIG_SCHEMA_VERSION,
        description="配置 schema 版本（与插件版本独立，仅在配置字段结构变更时手动上调）",
        json_schema_extra={"disabled": True},
    )
    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件"},
    )
    adapter_plugin_id: str = Field(
        default=DEFAULT_ADAPTER_PLUGIN_ID,
        description=(
            "提供 adapter.napcat.* API 的适配器插件 ID。"
            "读取生日依赖 NapCat 特有的 birthday_year/month/day 字段，"
            "SnowLuma 适配器没有这些字段；两个兼容适配器同时启用时短名不唯一、"
            "Host 会拒绝调用，因此默认填 NapCat 适配器的全名。"
            "留空则按短名调用，由 Host 自动定位唯一提供方"
        ),
        json_schema_extra={
            "label": "适配器插件 ID",
            "hint": "默认 NapCat 适配器；留空则按短名自动定位（同时启用两个适配器时会失败）",
            "placeholder": DEFAULT_ADAPTER_PLUGIN_ID,
        },
    )

    @field_validator("adapter_plugin_id", mode="before")
    @classmethod
    def _normalize_adapter_plugin_id(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()


class SourceSection(PluginConfigBase):
    """生日数据采集。"""

    __ui_label__ = "生日采集"

    include_friends: bool = Field(
        default=True,
        description=(
            "是否采集好友列表的生日。走 get_friend_list 一次拿全量，"
            "NapCat 的返回里直接带 birthday_year/month/day，成本最低"
        ),
        json_schema_extra={"label": "采集好友生日"},
    )
    include_group_members: bool = Field(
        default=False,
        description=(
            "[谨慎] 是否采集群成员生日。「群聊道贺」要靠它才知道群里有谁、他们的生日是几号。"
            "群成员列表本身不含生日，需要逐个调用 get_stranger_info，"
            "人数多时请求量大、有触发风控的风险，所以带每群上限和总量预算。"
            "注意只采 greeting.announce_groups 白名单里的群，白名单留空时一个群都不采"
        ),
        json_schema_extra={
            "label": "[谨慎] 采集群成员生日",
            "hint": "逐个查名片，人多时有风控风险；配合下面的每群上限与总量预算使用",
        },
    )
    max_members_per_group: int = Field(
        default=100,
        ge=0,
        le=3000,
        description="每个群单次最多为多少人查名片（防止在大群里一次刷几千个请求）；0 表示不限制",
        json_schema_extra={"label": "每群人数上限"},
    )
    max_stranger_queries: int = Field(
        default=300,
        ge=0,
        le=10000,
        description=(
            "单次采集里跨群合计最多查多少次名片，这是防风控的总闸；"
            "0 表示不限制。首次采集最坏情况 = 这个数字 × 查名片间隔"
        ),
        json_schema_extra={"label": "单次查名片总量上限"},
    )
    stranger_fetch_delay_ms: int = Field(
        default=800,
        ge=0,
        le=10000,
        description="逐个查名片之间的间隔（毫秒），用来错开请求避免风控",
        json_schema_extra={"label": "查名片间隔（毫秒）"},
    )
    refresh_interval_hours: int = Field(
        default=168,
        ge=1,
        le=8760,
        description=(
            "自动重新采集的间隔（小时），默认 168 小时（7 天）。"
            "生日数据变动很慢，没必要频繁拉取"
        ),
        json_schema_extra={"label": "采集间隔（小时）"},
    )
    initial_refresh_delay_seconds: int = Field(
        default=30,
        ge=0,
        le=3600,
        description="插件启动后延迟多少秒做首次采集，给适配器和连接留出就绪时间",
        json_schema_extra={"label": "启动延迟采集（秒）"},
    )
    no_cache: bool = Field(
        default=False,
        description=(
            "查询时是否禁用 NapCat 缓存（no_cache=True 会强制走 QQ 服务器）。"
            "默认 False 走本地缓存，更快、请求更轻；想让刚改过名片的生日立刻生效时再开"
        ),
        json_schema_extra={"label": "禁用 NapCat 缓存"},
    )
    drop_missing: bool = Field(
        default=False,
        description=(
            "采集成功时，是否把本次没出现的人从缓存里删掉。"
            "默认关闭：一次不完整的采集（比如适配器半途断开）不应该清空已有数据"
        ),
        json_schema_extra={"label": "清理消失的人"},
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_keys(cls, data: Any) -> Any:
        """兼容旧配置：watch_groups 已被 greeting.announce_groups 取代。"""

        if isinstance(data, dict) and "watch_groups" in data:
            data = dict(data)
            legacy = data.pop("watch_groups")
            _logger.warning(
                "source.watch_groups 已废弃（取值 %r）：群范围现在由 "
                "greeting.announce_groups 统一决定（留空 = 不采任何群，填 [\"*\"] = 全部群）",
                legacy,
            )
        return data


class GreetingSection(PluginConfigBase):
    """寿星当天的道贺行为。"""

    __ui_label__ = "生日道贺"

    greet_in_group: bool = Field(
        default=False,
        description=(
            "群聊道贺开关：开启后在寿星所在的群里 @ 他并祝贺。"
            "寿星范围来自群成员采集（source.include_group_members），"
            "群范围由下面的白名单决定。与私聊开关互相独立，可以只开一个"
        ),
        json_schema_extra={
            "label": "群聊道贺（@ 寿星）",
            "hint": "需要在群成员采集里读到生日，否则群里没人可选",
        },
    )
    greet_in_private: bool = Field(
        default=False,
        description=(
            "私聊道贺开关：开启后给过生日的好友单独发一条私聊。"
            "只对好友生效——非好友开不了私聊流。与群聊开关互相独立"
        ),
        json_schema_extra={"label": "私聊道贺（限好友）"},
    )
    private_temp_session: bool = Field(
        default=True,
        description=(
            "非好友私聊：好友走正常私聊，非好友改用「群临时会话」发（send_private_msg 带 group_id）。"
            "能否发出去取决于那个群是否允许成员发起临时会话（QQ 群设置里能关）；"
            "发送失败只记日志，群道贺不受影响"
        ),
        json_schema_extra={
            "label": "非好友用群临时会话私聊",
            "hint": "群设置里开了「允许群成员发起临时会话」才发得出去；否则只有群道贺",
        },
    )
    announce_groups: list[str] = Field(
        default_factory=list,
        description=(
            "群白名单：既是采集群成员的范围，也是群道贺的范围。"
            "**留空 = 一个群都不碰**（不采集、不群发）——空列表就照字面读作「没选群」。"
            "要把「机器人所在的全部群」都算进来，请显式写 [\"*\"]，"
            "这样「扫全部群」永远是一个看得见的决定，而不是空字段的副作用"
        ),
        json_schema_extra={
            "label": "群白名单",
            "hint": '留空 = 一个群都不碰；填群号 = 只这些群；填 ["*"] = 机器人所在的全部群',
        },
    )
    at_person: bool = Field(
        default=True,
        description="群道贺时是否 @ 寿星本人；关掉就发普通文本",
        json_schema_extra={"label": "群内 @ 寿星"},
    )
    hour_start: int = Field(
        default=8,
        ge=0,
        le=23,
        description="允许道贺的起始小时（本地时间，24 小时制）。早于这个时间不发，避免凌晨吵人",
        json_schema_extra={"label": "起始小时"},
    )
    hour_end: int = Field(
        default=23,
        ge=1,
        le=24,
        description="允许道贺的结束小时（本地时间，开区间）。到点后当天不再补发，避免深夜打扰",
        json_schema_extra={"label": "结束小时"},
    )
    greeting_template: str = Field(
        default="🎂 生日快乐，{name}！祝你新的一岁顺顺利利，想做的事都能成～",
        description=(
            "道贺文案模板。可用占位符："
            "{name}=昵称/备注、{age}=岁数（无名片年份时为空）、"
            "{month}、{day}=月日、{qq}=QQ号。"
            "开启「文案由模型生成」时，它退化为兜底文案（模型调用失败时发送）与风格参考"
        ),
        json_schema_extra={
            "label": "道贺文案（兜底）",
            "hint": "支持 {name} {age} {month} {day} {qq}；{age} 在没有出生年份时会变成空",
            "x-widget": "textarea",
        },
    )
    use_llm_polish: bool = Field(
        default=True,
        description=(
            "文案是否交给模型生成。默认开启：把生日信息注入生成提示后，由 llm_task_name "
            "指定的任务输出文案（相当于把「说什么」也交给麦麦的模型）。"
            "调用失败或输出为空时自动回退到 greeting_template，绝不会因此漏发道贺"
        ),
        json_schema_extra={
            "label": "文案由模型生成",
            "hint": "默认开启；关闭则直接发 greeting_template 原文，不额外消耗模型调用",
        },
    )
    llm_task_name: Literal["replyer", "utils", "planner"] = Field(
        default="replyer",
        description=(
            "生成文案用的模型任务槽位（Host model_task_config 下的任务名）。"
            "replyer=麦麦的主回复模型（默认，语气更贴人设）；"
            "utils=通用快模型（更省更快，风格偏中性）；planner=规划模型"
        ),
        json_schema_extra={
            "label": "文案模型槽位",
            "x-widget": "select",
            "options": [
                {"value": "replyer", "label": "replyer（主回复模型，默认）"},
                {"value": "utils", "label": "utils（通用快模型）"},
                {"value": "planner", "label": "planner（规划模型）"},
            ],
        },
    )
    llm_max_tokens: int = Field(
        default=10000,
        ge=0,
        le=32000,
        description=(
            "生成文案的最大 token 数，默认 10000（与 replyer 任务的默认值一致）；"
            "0 = 不覆盖、跟随 Host 模型配置（replyer 也是 10000）。"
            "**别调小**：回复类模型会先输出一段思考再给正文，额度太小会返回空内容"
            "（实测 128 必踩），此时会回退兜底文案；日志里的"
            "「达到最大输出 token 限制」+「文案生成返回空内容」就是这个原因。"
            "插件只取第一行、并截断到 120 字，所以额度给足不会让发出去的文案变长"
        ),
        json_schema_extra={"label": "文案最大 token"},
    )
    llm_temperature: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="文案生成采样温度（replyer 任务默认就是 1.0）",
        json_schema_extra={
            "label": "文案温度",
            "x-widget": "slider",
            "min": 0.0,
            "max": 2.0,
            "step": 0.05,
        },
    )

    @field_validator("announce_groups", mode="before")
    @classmethod
    def _normalize_announce_groups(cls, value: Any) -> list[str]:
        return _normalize_id_list(value)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        """兼容旧配置：announce_mode 枚举 → 两个独立开关；group_members_only 废弃。"""

        if not isinstance(data, dict):
            return data
        if "announce_mode" not in data and "group_members_only" not in data:
            return data

        data = dict(data)
        if "group_members_only" in data:
            _logger.warning(
                "greeting.group_members_only 已废弃（取值 %r）：现在按"
                "「寿星实际所属的群」发送，成员身份由群成员采集天然保证",
                data.pop("group_members_only"),
            )
        legacy_mode = data.pop("announce_mode", None)
        if legacy_mode is not None:
            mode = str(legacy_mode).strip().lower()
            mapping = {
                "off": (False, False),
                "prompt_only": (False, False),
                "group": (True, False),
                "private": (False, True),
                "both": (True, True),
            }
            group_on, private_on = mapping.get(mode, (False, False))
            data.setdefault("greet_in_group", group_on)
            data.setdefault("greet_in_private", private_on)
            _logger.warning(
                "greeting.announce_mode=%r 已废弃，已迁移为 greet_in_group=%s / "
                "greet_in_private=%s（不需要就直接改这两个开关）",
                legacy_mode,
                group_on,
                private_on,
            )
        return data

    @field_validator("llm_task_name", mode="before")
    @classmethod
    def _normalize_llm_task_name(cls, value: Any) -> str:
        normalized = "" if value is None else str(value).strip().lower()
        if normalized in LLM_TASK_NAMES:
            return normalized
        if normalized:
            _logger.warning(
                "greeting.llm_task_name 配置值 %r 非法，已回落到 'replyer'。合法取值: %s",
                value,
                " / ".join(LLM_TASK_NAMES),
            )
        return "replyer"

    @model_validator(mode="after")
    def _warn_announce(self) -> "GreetingSection":
        if self.greet_in_group and not self.announce_groups:
            _logger.warning(
                "greeting.greet_in_group 已开启，但 greeting.announce_groups 为空："
                "白名单为空 = 一个群都不碰，群道贺不会发生。"
                '请填具体群号，或填 ["*"] 表示机器人所在的全部群'
            )
        if "*" in self.announce_groups:
            _logger.info(
                'greeting.announce_groups 含 "*"：将对机器人所在的全部群采集与道贺'
            )
        if self.hour_end <= self.hour_start:
            _logger.warning(
                "greeting.hour_end(%s) 不大于 greeting.hour_start(%s)，"
                "道贺时间窗为空，当天不会主动道贺",
                self.hour_end,
                self.hour_start,
            )
        return self


class PromptSection(PluginConfigBase):
    """回复上下文注入（maisaka.replyer.before_request）。"""

    __ui_label__ = "上下文注入"

    inject_prompt: bool = Field(
        default=True,
        description=(
            "是否在麦麦生成回复前，把「今天是谁的生日」追加到本次回复要求里。"
            "只影响提示词、不做任何 IO；注意 Hook 只在麦麦本来就要回复时触发"
        ),
        json_schema_extra={"label": "注入生日提醒"},
    )
    skip_greeted: bool = Field(
        default=True,
        description=(
            "已经主动道贺过的人，当天不再注入。默认开启——避免"
            "「插件先主动发了一条祝福，麦麦聊天时又被提醒再提一次」的重复。"
            "注意只有**真正送达**才会被记为已道贺：主动发送失败时注入照常发生，"
            "相当于兜底。道贺方式为 off/prompt_only 时无人被标记，此项无效果"
        ),
        json_schema_extra={
            "label": "已道贺后不再注入",
            "hint": "主动道贺一次之后，当天的回复上下文里就不再出现这个人",
        },
    )
    max_names: int = Field(
        default=5,
        ge=1,
        le=20,
        description="一次最多列几个寿星名字。同一天过生日的人多时，只列前几个，避免提示词失控",
        json_schema_extra={"label": "最多列几个名字"},
    )
    include_upcoming_days: int = Field(
        default=0,
        ge=0,
        le=7,
        description=(
            "提前几天开始提醒（0=只在当天提醒）。例如填 1，"
            "生日前一天也会注入「X 明天生日」，方便麦麦提前准备"
        ),
        json_schema_extra={"label": "提前提醒天数"},
    )
    prompt_template: str = Field(
        default=(
            "【生日提醒】今天是 {names} 的生日。"
            "请在你这次回复里用自己的话顺带送上一句生日祝福，语气自然、别太正式，"
            "一句就够，不用单独再发一条；如果你这轮本来不适合说话，就不用勉强。"
        ),
        description=(
            "注入到回复要求里的文本模板，「文案怎么说」同样交给麦麦的回复模型。"
            "可用占位符：{names}=寿星列表（含昵称，可能多个）、{count}=人数、"
            "{upcoming}=未来几天即将过生日的人（可能为空）。"
            "想回到更保守的「合适才提」语气，把默认文本换成："
            "【生日提醒】今天是 {names} 的生日。如果当前对话里自然合适，可以顺口祝一句生日快乐；"
            "如果不合适或在聊别的事，就正常聊，不要硬提。"
        ),
        json_schema_extra={
            "label": "注入模板",
            "hint": "支持 {names} {count} {upcoming}；注意 Hook 只在麦麦本来就要回复时触发",
            "x-widget": "textarea",
        },
    )


class AccessSection(PluginConfigBase):
    """指令权限。"""

    __ui_label__ = "指令权限"

    admin_user_ids: list[str] = Field(
        default_factory=list,
        description=(
            "允许执行写操作指令（recheck / group / custom / del）的 QQ 号。"
            "本机操作者（is_local_operator）始终允许。留空表示只有本机操作者能改数据"
        ),
        json_schema_extra={"label": "管理员 QQ", "hint": "留空 = 只有本机操作者能改数据"},
    )

    @field_validator("admin_user_ids", mode="before")
    @classmethod
    def _normalize_admin_ids(cls, value: Any) -> list[str]:
        return _normalize_id_list(value)


class BirthdayConfig(PluginConfigBase):
    """麦麦祝你生日快乐呀完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    source: SourceSection = Field(default_factory=SourceSection)
    greeting: GreetingSection = Field(default_factory=GreetingSection)
    prompt: PromptSection = Field(default_factory=PromptSection)
    access: AccessSection = Field(default_factory=AccessSection)


# ============================================================================
# 文案与格式化（原 core/text.py）
# ============================================================================

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MD_PATTERN = re.compile(r"(\d{1,2})\s*[-/.月]\s*(\d{1,2})")


def parse_month_day(value: str) -> tuple[int, int] | None:
    """从 ``"05-20"`` / ``"5月20日"`` / ``"5/20"`` 解析出 ``(月, 日)``；非法返回 ``None``。

    同时用于手工登记指令（``/birth custom 10001 05-20``）和名片字段的字符串兜底，
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


# ============================================================================
# 本地存储（原 core/store.py）
# ============================================================================

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


# ============================================================================
# NapCat 采集（原 core/napcat.py）
# ============================================================================

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


# ============================================================================
# 采集器（原 core/collector.py）
# ============================================================================

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


# ============================================================================
# 调度与道贺（原 core/scheduler.py）
# ============================================================================

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

        发送与 ``/birth test`` 共用这一条路径，保证"测试看到的"就是"会发出去的"。
        """

        config = self._plugin.config.greeting
        text = render_greeting(
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
                outcomes.append(f"[试运行] {display_name(person)} ← {text}")
                continue

            delivered = False
            if config.greet_in_group:
                delivered = await self._deliver_group(person, text) or delivered
            if config.greet_in_private:
                delivered = await self._deliver_private(person, text) or delivered

            name = display_name(person)
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

    async def send_test_message(
        self, person: dict[str, Any], text: str, *, group_id: str = ""
    ) -> bool:
        """把一条测试祝福发到指定群或私聊，**不写"今天已道贺"**。

        给 ``/birth test`` 用：走的是和真实道贺同一套投递函数（群走 ``send_group_msg``
        带上 @ 段、私聊先走私聊流、不行再退回群临时会话），所以"测试通过"就等于
        "当天能发出去"。群号为空时只看私聊通道；群里发起的测试由调用方指定 ``group_id``。
        """

        if group_id:
            return await self._send_group(group_id, person, text)

        # 私聊：先按 QQ 号找/开私聊流（好友、以及已经私聊过的人都走这条）
        user_id = str(person.get("user_id") or "").strip()
        if user_id:
            stream_id = await self._resolve_stream(user_id=user_id)
            if stream_id and await self._send_text(text, stream_id):
                return True
        # 开不出私聊流（不是好友）时，借共同群发一条群临时会话
        target, borrow = self.temp_session_target(person)
        if target:
            return await self._send_temp_session(target, borrow, text)
        return False

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
                display_name(person),
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
                display_name(person),
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
        name = display_name(person)
        age = age_of(person)
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


# ============================================================================
# 插件主体（原 plugin.py）
# ============================================================================

STORE_FILENAME = "birthdays.json"

# 手工登记时最多探这么多个白名单群（每个群一次成员列表调用），防止一个指令打出去几十次请求
MAX_GROUP_PROBES = 8

HELP_TEXT = """【麦麦祝你生日快乐呀】指令（也可以写成 /生日）
/birth help                        这条帮助
/birth list                        发送缓存里的全部生日
/birth test                        在当前会话给我发一条测试祝福（不记入道贺）
/birth test <QQ号> [群号]           给指定的人发测试祝福（群里发则 @ 他）
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
        note = "\n…（太长已截断，可用 /birth custom 修正个别记录，或 /birth del 删掉多余的）"
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
                render_prompt(
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
                    f"{display_name(person)}"
                    f"（{offset} 天后，{format_month_day(person)}）"
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
                    "如果这个人确实填了生日，可能需要先刷新缓存（/birth recheck）。"
                )
            }
        today = date.today()
        lines = [self._format_person_line(person, today) for person in matched]
        return {"content": "匹配到的生日记录：\n" + "\n".join(lines)}

    # ==================== Command：人来查 ====================

    @Command(
        "birthday",
        description="群友生日：/birth help 看指令 / list 看缓存 / test 测试发送 / recheck 重新读取 / group 加群 / custom 手工登记 / del 删缓存",
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

        # 只保留这几个子命令：help / list / test / recheck / group / custom / del
        if sub in ("", "help", "帮助", "?", "-h"):
            return True, HELP_TEXT, True
        if sub in ("list", "列表"):
            return True, self._list_summary(), True
        if sub in ("test", "测试"):
            # 不带参数只测自己：任何人可用；带了 QQ 号就是往别人那儿发，要管理员
            if args and not self._is_admin(kwargs):
                return True, "给指定 QQ 发测试祝福需要管理员权限（不带参数只测你自己）。", True
            return True, await self._test_greeting(args, kwargs), True

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
                "（本地还没有任何生日数据：可用 /birth recheck 采集，"
                "并确认 plugin.adapter_plugin_id 指向 NapCat 适配器）"
            )
        return "\n".join(lines)

    def _list_summary(self) -> str:
        """列出缓存里的全部生日（``/birth list``）。"""

        assert self._store is not None
        total = len(self._store.people)
        if not total:
            return "缓存里还没有生日记录，先执行 /birth recheck 采集，或用 /birth custom 手工登记。"
        today = date.today()
        lines = [f"共 {total} 人（缓存里的全部，按日期排序）："]
        lines.extend(
            self._format_person_line(person, today) for person in self._store.all_people()
        )
        return "\n".join(lines)


    async def _test_greeting(self, args: list[str], kwargs: dict[str, Any]) -> str:
        """``/birth test [QQ号] [群号]``：当场发一条测试祝福，不记入"今天已道贺"。

        不带参数就在当前会话测自己；群里发起时默认发到当前群（@ 目标），私聊发起时
        走私聊通道。文案和真实道贺完全同一条生成路径，只有前缀多一个「【测试】」。
        """

        if self._scheduler is None or self._store is None:
            return "生日插件还没准备好，请稍后再试。"

        target = ""
        group_id = ""
        for token in args:
            if not re.fullmatch(r"\d{4,12}", token):
                return f"test 只认数字：{token} 既不是 QQ 号也不是群号。\n用法：/birth test [QQ号] [群号]"
            if not target:
                target = token
            elif not group_id:
                group_id = token
            else:
                return "用法：/birth test [QQ号] [群号]（最多两个数字）"

        target = target or str(kwargs.get("user_id") or "").strip()
        if not target:
            return "没拿到你的 QQ 号，请写成 /birth test <QQ号> [群号]。"
        group_id = group_id or str(kwargs.get("group_id") or "").strip()

        # 缓存里有这个人就用真实记录（好友状态、所属群、年龄都照旧），没有就造一条最小的
        person = dict(self._store.get_person(target) or {})
        person["key"] = target
        person["user_id"] = target
        if not str(person.get("name") or "").strip():
            person["name"] = await self._name_for_test(target, kwargs)

        text = "【测试】" + await self._scheduler.compose_greeting(person)
        try:
            sent = await self._scheduler.send_test_message(person, text, group_id=group_id)
        except Exception as exc:
            return f"测试祝福发送异常：{exc}"

        where = f"群 {group_id}" if group_id else f"私聊 {target}"
        if not sent:
            hint = (
                "群里发不出去，先看群号对不对、麦麦在不在那个群、适配器是否正常。"
                if group_id
                else "私聊发不出去：对方不是好友、没有可借的共同群，或那个群没允许发起临时会话。"
            )
            return f"测试祝福没有发出去（{where}）。\n{hint}"

        self.ctx.logger.info("[测试] 已向 %s 发送测试祝福（%s）：%s", target, where, text)
        return f"已发送到{where}（未记入道贺记录）：\n{text}"

    async def _name_for_test(self, qq: str, kwargs: dict[str, Any]) -> str:
        """测试祝福要写进文案里，所以名字得先弄对：本次消息的群名片/昵称 → 名片接口 → QQ 号。

        自己测自己时直接用手上这条消息里的群名片/昵称，不额外打接口；
        测别人（或消息里拿不到名字）时才读一次名片，读不到就退回 QQ 号。
        """

        if qq == str(kwargs.get("user_id") or "").strip():
            message = kwargs.get("message")
            info = message.get("message_info") if isinstance(message, dict) else None
            user = info.get("user_info") if isinstance(info, dict) else None
            if isinstance(user, dict):
                for field in ("user_cardname", "user_nickname"):
                    value = str(user.get(field) or "").strip()
                    if value:
                        return value

        if self._source is not None:
            try:
                profile = await self._source.fetch_stranger(qq, no_cache=True)
            except Exception as exc:
                self.ctx.logger.debug("测试祝福读名片失败(%s): %s", qq, exc)
                profile = None
            if profile:
                name = pick_name(profile)
                if name:
                    return name
        return qq

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
        parsed = parse_month_day(args[1])
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
        name = display_name(person)
        removed = self._store.remove_keys([qq])
        self._store.save()
        return f"已删除：{name}（{qq}），还剩 {len(self._store.people)} 条缓存。"


    # ==================== 小工具 ====================

    def _format_person_line(self, person: dict[str, Any], today: date | None = None) -> str:
        current = today or date.today()
        name = display_name(person)
        user_id = str(person.get("user_id") or "")
        month = int(person.get("month") or 0)
        day = int(person.get("day") or 0)
        age = age_of(person, current)
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
            "name": display_name(person),
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
