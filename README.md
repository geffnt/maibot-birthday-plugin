# 麦麦祝你生日快乐呀 (maibot-birthday-plugin)

把好友和群成员名片里的生日读进本地缓存，寿星当天在群里 @ 他道一句，也能给好友发私聊，
顺手把「今天是谁的生日」塞进麦麦的回复上下文里。

## 功能

- 读名片取生日：走 napcat-adapter 的 `get_friend_list` / `get_stranger_info`，NapCat 的返回里直接带 `birthday_year` / `birthday_month` / `birthday_day`。
- 本地缓存：采到的生日存在 `data/plugins/geffnt.maibot-birthday-plugin/birthdays.json`，聊天时不再反复请求接口。
- 主动道贺：到点在白名单群里 @ 寿星发一句，或给过生日的好友发私聊；非好友走群临时会话。
- 上下文注入：麦麦本来就要回复时，把生日提醒追加进这次回复的要求里，让它自己决定要不要提。
- 工具与指令：麦麦能调 `birthday_query` 工具查谁快过生日，群友能用 `/birth` 系列指令看缓存、重采、登记、删除。
- 文案交给模型：默认用 `replyer` 任务现写，模型失败或返回空时回退 `greeting_template`，不会因此漏发。
- 去重：同一个人同一天只道贺一次，插件重启、热重载都不会重发。

## 配置

主要配置位于 config.toml（宿主按插件的配置模型自动生成，不用手写；改完存盘即热生效，
日志里会出现「麦麦祝你生日快乐呀配置已更新」）：

`plugin.name`：插件内部名字，保持默认，改掉等于换一份配置。

`plugin.config_version`：配置版本，由宿主管理，升级时按新默认值重建 config.toml 并保留注释。

`plugin.enabled`：是否启用插件。

`plugin.adapter_plugin_id`：提供 `adapter.napcat.*` 的适配器插件 ID，默认 `maibot-team.napcat-adapter`。
同时启用两个兼容适配器时短名不唯一、宿主会拒绝调用，必须写全名。

`source.include_friends`：是否采集好友生日，默认开启。好友列表一次拿全量，返回里就带生日，成本最低。

`source.include_group_members`：是否采集群成员生日，默认关闭。群成员列表本身不含生日，要逐个调
`get_stranger_info`，人多时请求量大、有风控风险，所以配了每群上限和跨群总量；只采
`greeting.announce_groups` 白名单里的群，白名单留空时一个群都不采。

`source.max_members_per_group`：每个群单次最多为多少人查名片，默认 100，0 表示不限制。

`source.max_stranger_queries`：单次采集里跨群合计最多查多少次名片，默认 300，这是防风控的总闸，
0 表示不限制。首次采集最坏情况 = 这个数字 × `stranger_fetch_delay_ms`。

`source.stranger_fetch_delay_ms`：逐个查名片之间的间隔，默认 800 毫秒，用来错开请求。

`source.refresh_interval_hours`：自动重新采集的间隔，默认 168 小时（7 天）。生日变动很慢，没必要频繁拉。

`source.initial_refresh_delay_seconds`：插件启动后延迟多少秒做首次采集，默认 30 秒，给适配器留出就绪时间。

`source.no_cache`：查询时是否禁用 NapCat 缓存，默认关闭。走本地缓存更快、请求更轻，想让刚改过名片的生日立刻生效时再开。

`source.drop_missing`：采集成功时是否把本次没出现的人从缓存里删掉，默认关闭。一次不完整的采集（比如适配器半途断开）
不应该清空已有数据。

`greeting.greet_in_group`：群聊道贺开关，默认关闭。开启后在寿星所在的群里 @ 他并祝贺，寿星范围来自
`source.include_group_members`，群范围由 `greeting.announce_groups` 决定。

`greeting.greet_in_private`：私聊道贺开关，默认关闭。开启后给过生日的好友单独发一条；和群聊开关互相独立，可以只开一个。

`greeting.private_temp_session`：非好友改用「群临时会话」发私聊，默认开启。能否发出去取决于那个群是否允许成员发起
临时会话（QQ 群设置里能关）；发送失败只记日志，群道贺不受影响。

`greeting.announce_groups`：群白名单，既是采集群成员的范围，也是群道贺的范围。留空 = 一个群都不碰（不采集、不群发）；
填群号 = 只这些群；填 `["*"]` = 机器人所在的全部群。扫全部群必须显式写出来，空字段不会偷偷变成全部群。

`greeting.at_person`：群道贺时是否 @ 寿星本人，默认开启；关掉就发普通文本。

`greeting.hour_start` / `greeting.hour_end`：允许道贺的时间窗，默认 8 点到 23 点（本地时间，左闭右开）。
早于起点不发、到点后当天不再补发，避免凌晨或深夜打扰。

`greeting.greeting_template`：兜底文案，模型生成失败或关闭生成时发它。占位符 `{name}` 昵称、`{age}` 岁数
（名片没有年份时为空）、`{month}`、`{day}`、`{qq}`。

`greeting.use_llm_polish`：文案是否交给模型生成，默认开启。把生日信息注入生成提示，由 `greeting.llm_task_name`
指定的任务输出文案；关掉就直接发模板原文，不额外消耗模型调用。

`greeting.llm_task_name`：生成文案用的模型任务，默认 `replyer`（语气更贴人设）。可选 `utils`（更省更快，风格偏中性）、
`planner`。

`greeting.llm_max_tokens`：生成文案的最大 token 数，默认 10000，0 表示跟随宿主配置。别调小：回复类模型会先输出一段
思考再给正文，额度被思考吃光就返回空内容并回退兜底文案（实测 128 必踩），日志里表现为「达到最大输出 token 限制」
加「文案生成返回空内容」。

`greeting.llm_temperature`：生成文案的采样温度，默认 1.0。

`prompt.inject_prompt`：是否把「今天是谁的生日」注入麦麦的回复要求，默认开启。只影响提示词、不做任何 IO，
Hook 只在麦麦本来就要回复时触发。

`prompt.skip_greeted`：已经主动道贺过的人，当天不再注入，默认开启，避免插件先发一条、麦麦聊天时又提一次。
只有真正送达才会被记为已道贺，发送失败时注入照常发生，相当于兜底。

`prompt.max_names`：一次最多列几个寿星名字，默认 5，同一天过生日的人多时只列前几个。

`prompt.include_upcoming_days`：提前几天开始提醒，默认 0（只在当天）。填 1 时生日前一天也会注入「明天生日」。

`prompt.prompt_template`：注入文本，占位符 `{names}` 寿星列表、`{count}` 人数、`{upcoming}` 未来几天即将过生日的人。
想回到更保守的「合适才提」语气，换成 `config.example.toml` 里注释的那版。

`access.admin_user_ids`：允许执行写操作指令（`recheck` / `group` / `custom` / `del`）的 QQ 号。本机操作者始终允许，
留空表示只有本机操作者能改数据。

群号和 QQ 号都填数字本身：

```toml
[greeting]
greet_in_group = true
announce_groups = ["123456789"]

[access]
admin_user_ids = ["10001"]
```

## 手动命令

在群里可以直接使用：

```
/birth                            # 没带子命令 = 打印帮助
/birth help                       # 这条帮助
/birth list                       # 发缓存里的全部生日（按日期排序）
/birth recheck                    # 立刻重新采集一次（好友 + 白名单群成员）
/birth group 123456789            # 把群号加进白名单，并顺带打开群成员采集
/birth custom 10001 05-20 小明    # 手工登记，日期也可写 5/20，名字可省
/birth del 10001                  # 删掉这个 QQ 的缓存记录
```

示例：

```
/birth list
/birth custom 10001 5/20 小明
```

`/生日` 是等价的旧写法，命令名和中文子命令都不区分大小写。

`custom` 的主键是 QQ 号：名录里已有的人只改生日（带名字就连昵称一起改），原有的来源、好友标记、群关系都保留；
新人标成手工登记，采集时的 `source.drop_missing` 不会清掉它。它还会顺手读一次名片补上真昵称，
名片上的日期和你给的不一致时会在回复里提醒。登记时还会自动匹配身份：逐个拉白名单群的成员列表，看这个 QQ 在哪些群里，
再查一次好友列表，回复里直说「群道贺会发到这里 / 群道贺不会发 / 不是好友」；最多探 8 个群，适配器没响应只说群关系暂缺，
不影响登记。

`group` 写的是插件目录下的 config.toml（宿主只有只读的 `config.get*` 能力，插件只能改文件，改的是那一行、其余原样保留），
所以改完不用重启，宿主会自己重新校验并推给插件。

`del` 只认 QQ 号，不做名字模糊匹配，给名字只会提示用法。`list` 太长时会拆成多条发出，最后一条注明被截断。

指令的回复由插件自己发（`ctx.send.text`）：宿主只把命令返回值写进日志，不会替插件发消息。

`recheck` / `group` / `custom` / `del` 需要 `access.admin_user_ids` 里的账号或本机操作者，其他人发了只会看到提示。

## 注意事项

本版本要求 MaiBot 1.3.x（1.3.0~1.3.99）与 maibot-plugin-sdk 2.x（2.5.0~2.99.99）。换到更高或更低的宿主/SDK 上可能被判为
不兼容，遇到这种情况提个 Issue 说明你的版本。

从插件市场搜「麦麦祝你生日快乐呀」安装即可，也可以把文件夹放进 MaiBot 根目录的 plugins/ 后重启 MaiBot。
config.toml 由宿主按配置模型自动生成，不需要手写；模板是仓库里的 config.example.toml。

生日数据只有 NapCat 适配器能提供：SnowLuma 的资料接口走 OIDB `0xFE1_2`，属性目录里没有生日字段，
所以换到 SnowLuma 适配器也采不到生日。同时启用两个适配器时记得把 `plugin.adapter_plugin_id` 填成全名。

QQ 名片里的生日是可选字段：对方没填、或隐私设置里不公开，接口就返回空，插件一律当作没有数据，不会瞎猜，也不会因此删掉已有记录。
拿不到是正常现象，不是插件坏了；名片上没写的人可以用 `/birth custom` 手工登记。

群成员采集默认关闭，因为成本在它身上：一个 500 人群等于 500 次 `get_stranger_info`。打开后有三道闸——
每群上限 `source.max_members_per_group`、跨群总量 `source.max_stranger_queries`、请求间隔
`source.stranger_fetch_delay_ms`，并且只采白名单里的群；群白名单留空就是一个群都不碰。

非好友收不到私聊时，先看那个群有没有开「允许群成员发起临时会话」；他不在任何白名单群里则根本没有通道可用。

群道贺没发出去，按顺序查四处：`greeting.greet_in_group` 是否打开、`greeting.announce_groups` 是否为空
（空 = 一个群都不发）、`source.include_group_members` 是否打开（不采集群成员就不知道谁在群里）、这个人是否真在目标群里。

采到的生日只存在本地 birthdays.json，插件不会外发。文件写坏时不会清空数据，会先把坏文件改名成
`birthdays.json.broken` 再按空库启动；道贺记录只保留最近 30 天，判定只看当天。

仓库根目录的 `maibot-birthday-plugin-selfcheck.py` 是开发用自检脚本，用 MaiBot 自带的 venv 运行，
会用宿主的 `PluginManifest` 校验 manifest、按 Runner 的方式加载 plugin.py，再用假的上下文跑一遍
Hook、指令、工具和道贺投递，最后比对 config.example.toml 与配置 Schema 是否一致。

许可 MIT，见 LICENSE。
