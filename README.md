# 麦麦祝你生日快乐呀（maibot-birthday-plugin）

给麦麦（MaiBot）用的生日插件：从 QQ 名片把好友/群成员的生日采集到本地，寿星当天**主动道贺**，
同时把"今天谁过生日"**注入到麦麦的回复上下文**里，并提供查询工具与指令。

- 插件 ID：`geffnt.maibot-birthday-plugin`
- 版本：`1.0.0`
- 依赖：MaiBot ≥ 1.2.0、插件 SDK ≥ 2.5.0、**NapCat 适配器**（`maibot-team.napcat-adapter`）
- 许可：MIT

---

## 1. 它能做什么

| 能力 | 说明 |
| --- | --- |
| 生日采集 | 好友列表一次拿全量（含生日）；群成员可选用"逐个查名片"的方式补全 |
| 本地缓存 | 数据存成插件数据目录下的 `birthdays.json`，不在聊天时反复请求接口 |
| 定时道贺 | 每天在设定时间窗内扫描寿星，在指定群或私聊主动发祝福 |
| 上下文注入 | 回复前把生日提醒追加进麦麦本次的回复要求，让它自己决定要不要提 |
| 工具查询 | 麦麦可调用 `birthday_query` 查"谁快过生日了" |
| 人工管理 | `/birth` 指令：看缓存、重新读取、加群、手工登记、删缓存 |
| 去重 | 同一个人同一天只道贺一次；插件重启/热重载不会重复发 |

## 2. 前置条件（重要）

**生日数据只有 NapCat 适配器能提供。**

- NapCat 的 `get_friend_list` / `get_stranger_info` 会返回 `birthday_year` / `birthday_month` /
  `birthday_day`，字段来自 QQ NT 的 `NodeIKernelProfileService/getCoreAndBaseInfo` 与
  `ProfileService/fetchUserDetailInfo`。
- SnowLuma 适配器的资料拉取走 OIDB `0xFE1_2`，属性键目录是
  102 签名 / 103 备注 / 105 等级 / 20002 昵称 / 20009 性别 / 20037 年龄 / 27394 QID，
  其 `UserProfileInfo`、`FriendInfo`、`GroupMemberInfo` 类型里**都没有生日字段**。

因此：

1. 必须启用 NapCat 适配器；
2. 同时启用 NapCat 与 SnowLuma 时，`adapter.napcat.*` 短名会有两个提供方，Host 会拒绝调用，
   所以 `plugin.adapter_plugin_id` **必须填全名**（默认已填 `maibot-team.napcat-adapter`）。

另外，QQ 名片里的生日是**可选字段**：对方没填、或隐私设置不公开，接口就返回空/0。
插件把这种情况一律当作"没有数据"，不会瞎猜，也不会因此删掉已有记录。

## 3. 安装

1. 把整个 `maibot-birthday-plugin` 目录放进 MaiBot 的插件目录：

   ```
   <MaiBot>/plugins/maibot-birthday-plugin/
       _manifest.json
       plugin.py
       core/
       config.example.toml
       README.md
       LICENSE
   ```

2. 在 MaiBot 里重载插件（或重启）。插件启动时会自动生成
   `plugins/maibot-birthday-plugin/config.toml`。
   OneKey Desktop 会**监视插件目录并自动重载模块**：改完代码或 `config.toml` 存盘后，
   日志里会看到「麦麦祝你生日快乐呀插件已卸载 → 已加载」，不需要手动重启。
3. 确认 `[plugin] adapter_plugin_id` 指向你实际在用的 NapCat 适配器。
4. 在群里发 `/birth recheck`（需要管理员或本机操作者）完成第一次采集。
5. 想让插件盯某个群，发 `/birth group <群号>`——它会写进 `announce_groups` 并顺带打开群成员采集。
   想先看有哪些人生日，发 `/birth list`；名片上没写生日的人，用 `/birth custom <QQ号> <日期>` 补。

## 4. 快速上手（推荐顺序）

```toml
[source]
include_friends = true          # 好友先采起来（默认就是开的）

[greeting]
greet_in_group = false          # 第一步：两个主动开关都先关着，只让麦麦知道
greet_in_private = false
```

想让它真的说话时，两个开关独立选择：

```toml
[source]
include_group_members = true    # 群聊道贺要靠它才知道群里谁哪天生日（首次约几百次名片查询）

[greeting]
greet_in_group = true           # 在寿星所在的群里 @ 他并祝贺
greet_in_private = true         # 顺便私聊好友一条
announce_groups = ["你的群号"]   # 必填：留空 = 一个群都不碰；填 ["*"] = 全部群
at_person = true
hour_start = 8
hour_end = 23
```

## 5. 配置说明

完整字段见 `config.example.toml`（由自检脚本与 Schema 双向比对，不会与代码脱节）。

### `[plugin]`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用 |
| `adapter_plugin_id` | `maibot-team.napcat-adapter` | 提供 `adapter.napcat.*` 的适配器插件 ID；留空则按短名自动定位（两个适配器同时启用时会失败） |

### `[source]` 采集

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `include_friends` | `true` | 采集好友生日（一次全量，成本低） |
| `include_group_members` | `false` | **[谨慎]** 采集群成员生日：群成员列表不含生日，需逐个调 `get_stranger_info`。只采白名单里的群 |
| `max_members_per_group` | `100` | 每群单次最多查多少人（0 = 不限制） |
| `max_stranger_queries` | `300` | **跨群合计**的单次查名片总量上限，防风控总闸（0 = 不限制） |
| `stranger_fetch_delay_ms` | `800` | 逐个查名片的间隔，用来错开请求 |
| `refresh_interval_hours` | `168` | 自动重新采集间隔（默认 7 天） |
| `initial_refresh_delay_seconds` | `30` | 启动后延迟多久做首次采集 |
| `no_cache` | `false` | `true` = 绕过 NapCat 本地缓存强制走服务器 |
| `drop_missing` | `false` | 采集成功时是否删掉本次没出现的人（默认关闭，避免一次不完整采集清空数据） |

### `[greeting]` 道贺

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `greet_in_group` | `false` | **群聊道贺开关**：在寿星所在的群里 @ 他并祝贺 |
| `greet_in_private` | `false` | **私聊好友开关**：给过生日的好友单独发一条（非好友走下面的临时会话，走不通就自动跳过） |
| `private_temp_session` | `true` | **非好友私聊**：好友走正常私聊，非好友改用「群临时会话」（`send_private_msg` 带 `group_id`）。能否发出去取决于那个群是否允许成员发起临时会话（QQ 群设置里能关）；失败只记日志，不影响群道贺 |
| `announce_groups` | `[]` | 群白名单：**同时是采集范围与道贺范围**。**留空 = 一个群都不碰**（不采集、不群发）；填群号 = 只这些群；填 `["*"]` = 机器人所在的全部群 |
| `at_person` | `true` | 群道贺时是否 @ 寿星 |
| `hour_start` / `hour_end` | `8` / `23` | 允许道贺的时间窗（本地时间，左闭右开） |
| `greeting_template` | `🎂 生日快乐，{name}！…` | **兜底文案**（模型生成失败或关闭生成时发送）。占位符：`{name}` `{age}` `{month}` `{day}` `{qq}`；`{age}` 无名片年份时为空 |
| `use_llm_polish` | `true` | **文案是否交给模型生成**：把生日信息注入生成提示，由 `llm_task_name` 输出；关闭则直接发模板原文 |
| `llm_task_name` | `replyer` | 文案生成用的模型槽位：`replyer`（麦麦的主回复模型，默认）/ `utils` / `planner` |
| `llm_max_tokens` / `llm_temperature` | `10000` / `1.0` | 文案生成参数。默认与 `replyer` 任务一致（10000），`0` = 跟随 Host 配置。**别调小**：回复模型先思考再写正文，额度被思考吃光就返回空内容并回退模板（实测 `128` 必踩），日志里表现为「达到最大输出 token 限制」+「文案生成返回空内容」。发出去的文案只取第一行、截断到 120 字，额度给足不会让消息变长 |

两个开关的语义（互相独立，可任意组合）：

- **群聊道贺**：目标是"**寿星实际所在的群**"（群归属来自群成员采集）。
  `announce_groups` 既限定采集范围也限定发送范围，而且**留空就是不碰任何群**——
  空列表就照字面读作"一个都没选"。想覆盖机器人所在的全部群，必须显式写 `["*"]`，
  这样"扫全部群"永远是看得见的决定，而不是空字段的副作用。
- **私聊道贺**：好友走正常私聊流；**非好友**（但和白名单里的群有交集）改用「群临时会话」发——
  即适配器的 `send_private_msg` 带上 `group_id`，走那个群的临时会话通道。前提是**该群允许成员发起临时会话**
  （QQ 群设置里能关，默认一般开着）；关着的话会失败并只记一条 warning，群道贺照旧。
  交集群为空（不在任何白名单群）时没有通道可用，直接跳过。
- 两个都关 = 只做上下文注入（等价于旧的 `prompt_only`）。
- 旧配置里的 `announce_mode` 和 `group_members_only` 已废弃，加载时会自动迁移并打一条
  日志提醒，不会导致插件起不来。

### `[prompt]` 上下文注入

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `inject_prompt` | `true` | 是否注入生日提醒 |
| `skip_greeted` | `true` | **已主动道贺送达的人，当天不再注入**——一次道贺就够，避免麦麦聊天时又提一次；发送失败不算已道贺，注入照常 |
| `max_names` | `5` | 一次最多列几个名字 |
| `include_upcoming_days` | `0` | 提前几天预告（0–7） |
| `prompt_template` | `【生日提醒】今天是 {names} 的生日。请在你这次回复里…` | 注入文本，**文案怎么说同样交给麦麦的回复模型**。占位符：`{names}` `{count}` `{upcoming}` |

### `[access]` 权限

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `admin_user_ids` | `[]` | 允许执行写操作指令的 QQ 号；本机操作者始终允许 |

## 6. 指令

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/birth` | 所有人 | 没带子命令 = 打印帮助 |
| `/birth help` | 所有人 | 帮助（有多少条指令就看它） |
| `/birth list` | 所有人 | 发送缓存里的全部生日（按日期排序） |
| `/birth recheck` | 管理员 | 立刻重新读取一次生日（好友 + 白名单群成员） |
| `/birth group <群号>` | 管理员 | 把群号加进 `announce_groups`，并顺带打开群成员采集 |
| `/birth custom <QQ号> <日期> [名字]` | 管理员 | 手工登记一条生日（日期如 `05-20` / `5/20`） |
| `/birth del <QQ号>` | 管理员 | 精确删掉这个 QQ 的缓存记录 |

- 命令名大小写不敏感（`/Birth`、`/BIRTH` 都行）；`/生日` 是等价的旧写法（中文子命令同样可用：`帮助/列表/重新读取/群/custom/删除`）。
- `custom` 的主键是 **QQ 号**：名录里已有的人只改生日（可加 `[名字]` 改名），原有的来源、好友标记、群关系都保留；新人标成 `source=manual`，采集的 `drop_missing` 只清理好友来源，所以手工记录不会被覆盖或清掉。顺手会读一次名片：能补上真昵称，并在"名片上的日期和你给的日期不一致"时提醒你。
- `custom` 还会**自动匹配身份**：逐个拉白名单群的成员列表，看你登记的这个 QQ 在哪些群里（写进 `person.groups`，决定群道贺发不发得出去），再查一次好友列表（决定私聊道贺发不发得出去）；回复里会直说"群道贺会发到这里 / 群道贺不会发 / 不是好友"。最多探 8 个群、每群一次调用；适配器没响应时只说"群关系暂缺"，不影响登记本身。
- `group` 写的是插件目录下的 `config.toml`：Host 只提供 `config.get*` 三个只读能力、没有"写配置"，所以插件只能改文件（临时文件 + `os.replace` 原子替换，只动目标键那一行，其余内容原样保留）。
- `del` **只认 QQ 号**，不做名字模糊匹配；给名字会提示用法，不会误删。
- 列表超过单条消息上限时（人很多才会遇到）会自动**拆成多条**发出，最后一条注明被截断。
- 指令回复由**插件自己发送**（`ctx.send.text`）——Host 只把命令返回值写进日志，不会替你发。

## 7. 工作原理

```
NapCat 适配器                     本插件
─────────────                    ──────────────────────────────────────
get_friend_list ────┐
                    ├─→ 采集器 ─→ birthdays.json（原子写，损坏自动备份）
get_group_list ─────┤                │   · 群成员列表里免费拿到"谁在哪个群"
get_group_member_list ┤              │   · 缺生日的人才逐个查名片
get_stranger_info ──┘                │
                                     ├─→ 60s 步进的调度器
                                     │     · 到间隔就重新采集
                                     │     · 到点扫描寿星 → 群聊（@ 寿星）+ 私聊好友
                                     │       文案由 replyer 生成，失败回退模板
                                     │     · 按 key+日期去重，重启不重发
                                     │
                                     └─→ maisaka.replyer.before_request Hook
                                           · 把生日提醒追加到 extra_prompt
                                           · 已主动道贺过的人当天不再注入
                                           · 幂等：同一段提醒不重复叠加
                                           · 只改提示词、不做 IO，异常一律吞掉
```

三个设计取舍：

1. **"发不发"由插件定，"怎么说"交给模型**。发不发是确定性行为，交给 LLM 判断会出现
   "它今天不想说话就不发"；而文案默认走 `greeting.llm_task_name = replyer`，
   也就是用麦麦平时说话的那套模型来生成（`use_llm_polish`，默认开）。
   模型调用失败或输出为空时自动回退到 `greeting_template`，**绝不因此漏发**。
2. **注入模板默认要求"这次回复里带上祝福"**。注意 Hook 只在麦麦本来就要回复时触发，
   所以这不是"让它无缘无故插一句"，而是"既然要说话，就顺手说一句"。
   想回到更保守的"合适才提"，把 `prompt_template` 换成 `config.example.toml` 里注释的那版。
3. **群道贺走"寿星所属群"，不往陌生群广播**。群归属来自群成员采集（成员列表里就有），
   所以白名单只用来收窄范围。这也是为什么"好友同时是群成员"必须单独补记归属——
   否则他们的群道贺永远发不出来。

## 8. 风控与隐私

- 好友列表是一次性批量接口，开销极小；**群成员生日才是风险点**：一个 500 人群等于 500 次
  `get_stranger_info`（已在缓存里的人不重复查，只补群归属）。所以它默认关闭，
  并且有每群上限 `max_members_per_group`、跨群总量预算 `max_stranger_queries`
  和请求间隔 `stranger_fetch_delay_ms` 三道闸。**群白名单留空 = 一个群都不碰**，
  不存在"忘了配就偷偷扫全部群"这种情况；要扫全部群必须显式写 `["*"]`。
- 采集到的生日只存在本地 `birthdays.json`，插件不会外发这些数据。
- 提醒：QQ 名片的生日可见性由对方决定，拿不到是正常现象，不是插件坏了。

## 9. 数据结构

`data/plugins/geffnt.maibot-birthday-plugin/birthdays.json`：

```json
{
  "version": 1,
  "updated_at": 1700000000.0,
  "sources": { "friends": { "at": 1700000000.0, "count": 120 } },
  "people": {
    "123456": {
      "key": "123456", "user_id": "123456", "name": "小明",
      "month": 5, "day": 20, "year": 0,
      "source": "friend", "groups": ["987654"], "fetched_at": 1700000000.0
    }
  },
  "greeted": { "123456": "2026-05-20" }
}
```

- 文件写坏时不会清空数据：插件会把坏文件改名为 `birthdays.json.broken` 再按空库启动。
- `greeted` 只保留最近 30 天，判定只看当天。

## 10. 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| `/birth recheck` 提示失败 / 命中 0 人 | 适配器没起或 `adapter_plugin_id` 不对。看日志里 `adapter.napcat.account.get_friend_list` 那一行的报错；如果报"名称不唯一"，说明两个适配器都在跑，把 `adapter_plugin_id` 填成全名 |
| 采集到了好友但没人有生日 | QQ 名片生日是可选字段，多数人没公开；可以让对方在 QQ 里填上，或者用 `/birth custom <QQ号> <日期>` 手工补（名片上没写也能登记） |
| 群道贺没发出去 | ① `greet_in_group` 是否打开；② `announce_groups` 是否为空（**空 = 一个群都不发**，用 `/birth group <群号>` 加）；③ `include_group_members` 是否打开（不采集群成员就不知道谁在群里）；④ 寿星的 `groups` 里是否有目标群 |
| 群道贺发出来了但没有 @ | 检查 `at_person`；@ 走适配器原生 `send_group_msg`，若该调用失败会自动回退成普通文本（日志里有 warning） |
| 非好友没收到私聊 | 先看那个群有没有开「允许群成员发起临时会话」（`private_temp_session` 为 `true` 时走这条通道），关着的话日志里会有"发群临时会话失败"；他不在任何白名单群里则没有通道可用 |
| 麦麦聊天里不提生日 | 若这个人当天已被主动道贺过，`[prompt] skip_greeted` 会让它不再注入（预期行为）；否则检查 `inject_prompt` |
| 日志里 `未送达` | 目标群/人没有可用聊天流，或适配器发送失败，日志有具体报错 |
| 指令**只有日志、群里没消息** | Host **不会**替插件发命令回复：`bot.py::_handle_command_processing_result` 只把返回文本写进日志（所以你会看到"命令处理完成，跳过后续消息处理: …"），Host 自己的内置命令也都是自己发的。插件必须显式 `await self.ctx.send.text(文本, stream_id)`——本插件已这么做；改坏了这条路径就会重现"日志有、群里没有" |
| `/birth group` 说"配置没写进去" | 插件目录里的 `config.toml` 不存在或不可写（Host 只有只读的 `config.get*` 能力，插件只能改文件），这次运行有效但重启会丢，请在 WebUI 手动加 |
| 想确认插件是否正常加载 | 日志里应有"麦麦祝你生日快乐呀插件已加载：缓存 N 人，道贺 群聊@寿星+私聊好友 …" |

## 11. 开发与自检

仓库根目录的 `maibot-birthday-plugin-selfcheck.py` 是一个开发用自检脚本（用 MaiBot 自带的 venv 跑），
它会：

1. 用 MaiBot 自己的 `PluginManifest` 校验 `_manifest.json`；
2. 静态交叉校验"源码里调用的能力"与 manifest 声明是否一致；
3. 按 Runner 的方式（`spec_from_file_location` + `submodule_search_locations`）加载 `plugin.py`；
4. 用假的 PluginContext 跑 `on_load` → Hook → 指令 → 工具 → 道贺投递 → `on_unload`；
5. 用字段边界用例测 `extract_birthday`，用畸形配置测配置容错；
6. 校验 `config.example.toml` 与配置 Schema 字段完全一致。

```powershell
& "<MaiBot>/python-env/python.exe" "path/to/maibot-birthday-plugin-selfcheck.py"
```

## 12. 提交到插件中心

按 [plugin-repo 的 CONTRIBUTING](https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md)，本目录已经满足的部分：

- `_manifest.json` 位于根目录，manifest v2（`manifest_version: 2`），三段式 `version`、`host_application` / `sdk` 区间、
  `author{name,url}`、`license`、`urls.repository`、`capabilities`、`i18n` 齐全，且没有 Schema 未声明的字段
  （`categories` / `keywords` / `repository_url` 这类字段会让 Host 校验直接失败）；
- `plugin.py` / `LICENSE`（MIT，与 manifest 的 `license` 一致）/ `README.md`（功能、安装、配置、指令、示例）。

还差的（需要 GitHub 权限，插件作者自己操作）：

1. **公开仓库**：把本目录推到 `https://github.com/geffnt/maibot-birthday-plugin`，`_manifest.json` 必须在**仓库根目录**
   （main/master/dev/develop 任一分支）。注意本机 hosts 把 `github.com` 等域名指到 `127.0.0.1`，推送前要先处理。
2. **GitHub Release**：先提交代码，再打 `v1.0.0` Tag 并发布 Release。Tag 必须与 manifest 的 `version` 完全一致，
   草稿或只打 Tag 都不满足要求（用 GitHub 自动生成的源码包即可）。
3. **提 Issue**：用 "Add Plugin / 添加插件" 模板，填插件 ID 和仓库地址。CI 会自动读取仓库根目录的 `_manifest.json`
   校验并把结果评论在 Issue 里；通过后由维护者 `/approve` 收录。之后修 bug 要发新版本号（同一版本收录后 Tag/commit 不能再变）。

## 13. 许可

MIT，见 `LICENSE`。
