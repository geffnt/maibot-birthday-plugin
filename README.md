# 麦麦祝你生日快乐呀 (maibot-birthday-plugin)

通过 napcat 读取好友和群成员名片里的生日写入本地缓存，寿星当天在群里 @ 进行祝贺，也能给好友发私聊，
把「今天是谁的生日」塞进麦麦的回复上下文里。
snowluma 并没有读取资料卡的生日的功能，所以不支持 snowluma。

## 功能

- 读名片取生日：使用 napcat 的 `get_friend_list` / `get_stranger_info` 获取 `birthday_year` / `birthday_month` / `birthday_day`。
- 本地缓存：采到的生日存在 `data/plugins/geffnt.maibot-birthday-plugin/birthdays.json`，聊天时不再反复请求接口。
- 主动道贺：到点在白名单群里 @ 寿星发送祝贺，或给过生日的好友发私聊；非好友可以走群临时会话。
- 上下文注入：麦麦本来就要回复时，把生日提醒追加进这次回复的要求里，让它自己决定要不要提。
- 工具与指令：麦麦能调 `birthday_query` 工具查谁快过生日，群友能用 `/birth` 系列指令看缓存、重采、登记、删除，`/birth test` 可以发送祝福测试功能。
- 文案交给模型：默认用 `replyer` 任务现写，模型失败或返回空时回退 `greeting_template`，不会因此漏发。
- 去重：同一个人同一天只道贺一次，插件重启、热重载都不会重发。

## 配置

主要配置位于 config.toml。

`plugin.config_version`：配置版本，由宿主管理，升级时按新默认值重建 config.toml 并保留注释。

`plugin.enabled`：是否启用插件。

`plugin.adapter_plugin_id`：提供 `adapter.napcat.*` 的适配器插件 ID，默认 `maibot-team.napcat-adapter`。
同时启用两个兼容适配器时短名不唯一、宿主会拒绝调用，必须写全名。

`source.include_friends`：是否采集好友生日，默认开启。

`source.include_group_members`：是否采集群成员生日，默认关闭。群成员列表本身不含生日，要逐个调
`get_stranger_info`，人多时请求量大、有风控风险，所以配了每群上限和跨群总量；只采
`greeting.announce_groups` 白名单里的群，白名单留空时一个群都不采。

`source.max_members_per_group`：每个群单次最多为多少人查询名片，默认 100，0 表示不限制。

`source.max_stranger_queries`：单次采集里跨群合计最多查多少次名片，默认 300，
0 表示不限制。首次采集最坏情况 = 这个数字 × `stranger_fetch_delay_ms`。

`source.stranger_fetch_delay_ms`：逐个查名片之间的间隔，默认 800 毫秒，用来错开请求。

`source.refresh_interval_hours`：自动重新采集的间隔，默认 168 小时（7 天）。

`source.initial_refresh_delay_seconds`：插件启动后延迟多少秒做首次采集，默认 30 秒。

`source.no_cache`：查询时是否禁用 NapCat 缓存，默认关闭。走本地缓存更快、请求更轻，想让刚改过名片的生日立刻生效时再开。

`source.drop_missing`：采集成功时是否把本次没出现的人从缓存里删掉，默认关闭。一次不完整的采集（比如适配器半途断开）
不应该清空已有数据。

`greeting.greet_in_group`：群聊道贺开关，默认关闭。开启后在寿星所在的群里 @ 他并祝贺，寿星范围来自
`source.include_group_members`，群范围由 `greeting.announce_groups` 决定。

`greeting.greet_in_private`：私聊道贺开关，默认关闭。开启后给过生日的好友单独发一条；和群聊开关互相独立，可以只开一个。

`greeting.private_temp_session`：非好友改用「群临时会话」发私聊，默认开启。需要群开启「允许群成员发起临时会话」；
发送失败只记日志，群道贺不受影响。

`greeting.announce_groups`：群白名单，既是采集群成员的范围，也是群道贺的范围。

`greeting.at_person`：群道贺时是否 @ 寿星本人，默认开启；关掉就发普通文本。

`greeting.hour_start` / `greeting.hour_end`：允许道贺的时间窗，默认 8 点到 23 点（本地时间，左闭右开）。
早于起点不发、到点后当天不再补发，避免凌晨或深夜打扰。

`greeting.greeting_template`：兜底文案，模型生成失败或关闭生成时发送默认文本。占位符 `{name}` 昵称、`{age}` 岁数
（名片没有年份时为空）、`{month}`、`{day}`、`{qq}`。

`greeting.use_llm_polish`：文案是否交给模型生成，默认开启。把生日信息注入生成提示，由 `greeting.llm_task_name`
指定的任务输出文案；关掉发送模板原文，不额外消耗模型调用。

`greeting.llm_task_name`：生成文案用的模型任务，默认 `replyer`。可选 `utils`、`planner`。

`greeting.llm_max_tokens`：生成文案的最大 token 数，默认 10000。

`greeting.llm_temperature`：生成文案的采样温度，默认 1.0。

`prompt.inject_prompt`：是否把「今天是谁的生日」注入麦麦的回复要求，默认开启。只影响提示词、不做任何 IO，
Hook 在麦麦回复时触发。

`prompt.skip_greeted`：已经主动道贺过的人，当天不再注入，默认开启。

`prompt.max_names`：一次最多列几个寿星名字，默认 5，同一天过生日的人多时只列前几个。

`prompt.include_upcoming_days`：提前几天开始提醒，默认 0（只在当天）。填 1 时生日前一天会注入「明天生日」。

`prompt.prompt_template`：注入文本，占位符 `{names}` 寿星列表、`{count}` 人数、`{upcoming}` 未来几天即将过生日的人。
想回到更保守的「合适才提」语气，就换成：`【生日提醒】今天是 {names} 的生日。如果当前对话里自然合适，可以顺口祝一句生日快乐；如果不合适或在聊别的事，就正常聊，不要硬提。`

`access.admin_user_ids`：允许执行写操作指令（`recheck` / `group` / `custom` / `del` / 指定对象的 `test`）的 QQ 号。
本机操作者始终允许，留空表示只有本机操作者能改数据。

`access.list_visible_to_all`：`/birth list` 是否对所有人开放，默认 `true`。关闭仅管理员可使用。

群号和 QQ 号都填数字本身：

```toml
[greeting]
greet_in_group = true
announce_groups = ["123456789"]

[access]
admin_user_ids = ["10001"]
list_visible_to_all = true
```

`/birth group` 会改插件目录下的 `config.toml`（Host 没有"写配置"的能力，只能改文件）。改动是定点替换：
先确认现有文件能解析、改完再用 `tomllib` 校验一遍（连"目标键确实变成了期望的值"一起查），
任何一步不成立就原样放弃并提示去 WebUI 改——不会给 Runner 留一个解不开的配置。
落盘前还会再比对一次文件内容：如果这期间 WebUI 或 Runner 把文件改过，就整笔放弃让你重试，
不会互相覆盖。

## 手动命令

在群里可以直接使用：

```
/birth                            # 打印帮助
/birth help                       # 打印帮助
/birth list                       # 发缓存里的全部生日（按日期排序）
/birth test                       # 在当前会话发一条测试祝福（不记入道贺）
/birth test 10001 [群号]          # 指定对象发测试祝福，群里发则 @ 他
/birth recheck                    # 立刻重新采集一次（好友 + 白名单群成员）
/birth group 123456789            # 把群号加进白名单，并顺带打开群成员采集
/birth custom 10001 05-20 麦麦    # 手工登记，日期也可写 5/20，名字可省
/birth del 10001                  # 删掉这个 QQ 的缓存记录
```

示例：

```
/birth list
/birth custom 10001 5/20 麦麦
```

`/生日` 是等价的旧写法，命令名和中文子命令都不区分大小写。

`test` 是用来验证是否可生成：不带参数就给发指令的人自己发一条，在群里发起时发到当前群并 @ 目标，
文案和真实道贺走同一条生成路径，只在前面多一个「【测试】」，不会写进「今天已道贺」，
不影响当天的正式祝福。带 QQ 号（或群号）才需要管理员。文案里的名字优先用群名片/昵称，测别人时读一次名片补昵称，
读不到才退回 QQ 号。

`custom` 的主键是 QQ 号：名录里已有的人只改生日（带名字就连昵称一起改），原有的来源、好友标记、群关系都保留；
新人标成手工登记，采集时的 `source.drop_missing` 不会清掉它。它还会顺手读一次名片补上真昵称，
名片上的日期和你给的不一致时会在回复里提醒。登记时还会自动匹配身份：逐个拉白名单群的成员列表，看这个 QQ 在哪些群里，
再查一次好友列表，回复里直说「群道贺会发到这里 / 群道贺不会发 / 不是好友」；最多探 8 个群，适配器没响应只说群关系暂缺，
不影响登记。

`group` 写的是插件目录下的 config.toml（宿主只有只读的 `config.get*` 能力，插件只能改文件，改的是那一行、其余原样保留），
改完不用重启。

`del` 只使用 QQ 号，不做名字模糊匹配。`list` 太长时会拆成多条发出，最后一条注明被截断。

指令的回复由插件自己发（`ctx.send.text`）：宿主只把命令返回值写进日志，不会替插件发消息。

`recheck` / `group` / `custom` / `del` 需要 `access.admin_user_ids` 里的账号或本机操作者。
`list` 默认所有人可用，关掉 `access.list_visible_to_all` 后需要管理员。
`test` 不带参数时人人可用（只能测自己），带 QQ 号才算写操作，需要管理员。

## 注意事项

本版本要求 MaiBot 1.3.x（1.3.0~1.3.99）与 maibot-plugin-sdk 2.x（2.5.0~2.99.99）。换到更高或更低的宿主/SDK 上可能被判为
不兼容，遇到这种情况提个 Issue 说明你的版本。

从插件市场搜「麦麦祝你生日快乐呀」安装即可，也可以把文件夹放进 MaiBot 根目录的 plugins/ 后重启 MaiBot。
插件没有额外依赖；config.toml 由宿主按配置模型自动生成，字段说明和默认值都在里面，不需要手写。

读生日靠的是名片里的 `birthday_year` / `birthday_month` / `birthday_day` 三个字段。NapCat 客户端提供这三个字段；
SnowLuma 没有或者我没找到，有方法可以提 issue 谢谢喵。

同时启用两个适配器时短名不唯一、宿主会拒绝调用，这时把 `plugin.adapter_plugin_id` 填成全名即可。

QQ 名片里的生日是可选字段：对方没填、或隐私设置里不公开，接口不返回数据。
名片上没写的人可以用 `/birth custom` 手工登记。

群道贺没发出去，按顺序查四处：`greeting.greet_in_group` 是否打开、`greeting.announce_groups` 是否为空、
`source.include_group_members` 是否打开（不采集群成员就不知道谁在群里）、这个人是否真在目标群里。

采到的生日只存在本地 birthdays.json，插件不会外发。文件写坏时不会清空数据，会先把坏文件改名成
`birthdays.json.broken` 再按空库启动；道贺记录只保留最近 30 天，判定只看当天。

许可 MIT，见 LICENSE。
