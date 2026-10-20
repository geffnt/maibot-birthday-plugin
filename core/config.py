"""麦麦祝你生日快乐呀的强类型配置 Schema。

五个 Section（plugin / source / greeting / prompt / access）聚合到 ``BirthdayConfig``，
由 ``BirthdayPlugin.config_model`` 绑定，Runner 据此生成 ``config.toml`` 与 WebUI 表单。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator, model_validator


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
