# astrbot_plugin_pokedex — 宝可梦图鉴插件

AstrBot 插件：内置 [pokemon-dataset-zh](https://github.com/42arch/pokemon-dataset-zh) 中文图鉴库，支持指令与 LLM 函数调用查询，图鉴结果以 **图片卡片** 展示。查询**优先使用本地中文数据**；PokéAPI 官方数据作为补充（可配置关闭，关闭后纯离线运行）。结构参考 [astrbot_plugin_bazaar](https://github.com/shitlsh/astrbot_plugin_bazaar)。

v0.2.0 起新增**计算与模拟**能力：等级能力值计算、双向属性相性、宝可梦对比、克制推荐、招式表/配招、对战模拟、队伍分析——全部基于本地数据，离线可用。

v0.3.0 起新增**宝可梦 Champions 冠军赛数据**查询：使用率排名、招式/道具/特性/性格/能力点/队友配置、道具反查、跨赛季趋势。数据由**独立脚本** `champions_fetch.py` 从 [PokéChamp DB](https://pokechamdb.com) 抓取写入本地 SQLite（`data/plugin_data/astrbot_plugin_pokedex/champions.db`），**插件运行时只读该库、零网络请求**。

## 设计原则

**LLM 工具原子化，聊天指令模块化**：工具层保持一个工具一个动作（25+ 个原子能力，
供模型按问题自由组合，如 `analyze_team` → 逐只 `query_champions_build` → `validate_pokemon_moveset`）；
聊天指令只提供粗粒度功能模块，不镜像每个原子能力——例如跨表搜索只是工具层的原子
（`search_pokemon`），不设指令入口。新能力优先以工具形式加入。

## 功能

- **本地中文图鉴**（打包于 `data/*.tsv`，由数据集项目 `build_render.py` 生成）：
  - `pokemon` 1320 行 = 1025 个种族默认行 + 295 个形态行（`form_name` 列），升级招式带等级（`藤鞭(3)`，初始招式计 1 级）。查形态名（如 超级烈咬陆鲨Ｚ）直接命中形态行；查种族名优选默认形态并提示其他形态
  - `types` 18×18 基准属性表（由源数据按多数票推导，已与官方标准逐格核对）
  - 特性（总表 + 详情含持有者）、招式（总表 + 详情）、道具（1425）、24 个地区图鉴
- **指令**（群聊/私聊均可用）：

  | 指令 | 说明 |
  |---|---|
  | `宝可梦` | 插件帮助 |
  | `宝可梦 <编号/名称/形态名>` | 图鉴卡片（头部带冠军赛单双打名次徽章，尾部带后续指令反查） |
  | `宝可梦 <名称> 招式表 [等级]` | 可学招式（升级/学习器/遗传）+ 推荐配招 |
  | `宝可梦 <名称> 能力值 [等级 个体 性格]` | 官方公式计算等级能力值（等级默认 50，个体默认 31） |
  | `特性 <名称>` / `招式 <名称>` / `道具 <名称>` | 对应数据查询 |
  | `性格 [名称]` | 25 种性格能力修正速查（性格 固执 看单项 + 口味偏好） |
  | `相性 <属性1> [属性2]` / `相性 打 <属性>` | 属性相性防守/进攻双向（含受击类特性修正提示） |
  | `克制 <宝可梦>` | 推荐克制它的候选（双向验证 + 种族值排序） |
  | `克制 <A> <B>` | 多维度对比：逐项种族值 + 属性互克 + 特性 + 小结 |
  | `对战 <名称1> <名称2> [等级]` | 回合制对战模拟 + 战报（含双方冠军赛单双打名次） |
  | `队伍分析 <名称...>` | 2-6 只队伍的弱点配置分析（含成员冠军赛名次） |
  | `筛选 <条件>` | 按种族值筛选，如 `筛选 速度>100 特攻>=120` |
  | `许愿星 <性格> [个体] [TID SID]` | 许愿星基拉祈目标筛选（红/蓝宝石） |
  | `宝冠军 [关键词]` | 冠军赛一站式查询（下表），`宝冠军 帮助` 看用法 |
  | `宝冠军 单打` / `宝冠军 双打` | Top10；加赛季：`宝冠军 双打 上一轮`、`宝冠军 单打 6` |
  | `宝冠军 3` / `宝冠军 双打 3` | 第 3 名是谁（附招式/道具摘要） |
  | `宝冠军 烈咬陆鲨` | 名次（单+双）+ 招式/道具/特性/性格/能力点/队友使用率 |
  | `宝冠军 烈咬陆鲨 趋势` | 跨赛季名次；`宝冠军 烈咬陆鲨 M-6` = 指定赛季 |
  | `宝冠军 生命宝珠` | 反查谁在带这个道具（含携带率） |
  | `宝冠军 数据` | 本地库状态、来源与更新时间 |

- **LLM 函数调用工具**（需模型供应商支持 function calling；群聊 @机器人 自然语言提问时自动调用）：
  - 查询：`query_pokemon` / `search_pokemon`（跨表，含地区图鉴） / `query_ability` / `query_move` / `query_item` / `query_nature`（性格修正） / `query_dex_region` / `query_type_matchups` / `find_pokemon_by_type`
  - 计算/分析：`calc_pokemon_stats`（等级能力值）/ `query_type_offense`（进攻相性）/ `compare_pokemon`（对比）/ `find_pokemon_counters`（克制推荐）/ `query_learnset`（招式表）/ `validate_pokemon_moveset`（配招校验）/ `simulate_battle`（对战模拟）/ `analyze_team`（队伍分析）/ `find_pokemon_by_stats`（按种族值筛选）
  - PokéAPI 补充：`pokeapi_pokemon_detail`（官方译名/图鉴说明/英文特性效果）、`pokeapi_type_matchups`（官方倍率表）
  - 冠军赛：`query_champions_ranking`（排名）/ `query_pokemon_rank`（某宝可梦名次）/ `query_rank_pokemon`（名次反查宝可梦）/ `query_champions_build`（配置）/ `find_champions_by_item`（道具反查）/ `query_champions_trend`（趋势）/ `champions_data_status`（数据状态）
- **受击类特性免疫**：避雷针/蓄电→电、引火→火、储水→水、飘浮→地面；图鉴卡片免疫区追加条件徽章（如「电【避雷针】」），相性查询返回特性修正；
- **战斗引擎**（`battle_engine.py`）：官方伤害公式 + STAB/暴击 6.25%/随机系数、速度先手、PP 与挣扎（含 1/4 反伤）、灼伤/中毒/麻痹（含电/火/毒钢属性免疫）；简化项：无特性/道具/天气/招式附加效果。支持注入随机种子，同种子结果可复现
- **回答规范**：本地数据字段均为中文，回答直接引用中文原文，不自行翻译或改写；仅 PokéAPI 英文内容才翻译。

## 安装

1. 将本目录放入 AstrBot 的 `data/plugins/`（或通过 WebUI「插件管理 → 从 ZIP 安装」上传 zip 包）。
2. 安装依赖（WebUI 会自动执行）：`pillow`、`aiohttp`。
3. （推荐）在插件配置中把 `images_dir` 指向数据集的 `images` 目录，卡片即可使用本地官方立绘：
   ```
   images_dir = /path/to/pokemon-dataset-zh/data/images
   ```
   注意：若 AstrBot 跑在 Docker 中，需先把该目录挂载进容器，配置里填容器内路径。不配置则按 `fallback_web_image` 决定是否使用网络官方图（仅默认形态），或无立绘。
4. **离线/本地调试环境**：在插件配置中把 `pokeapi_enabled` 和 `fallback_web_image` 设为 `false`，即完全只用本地数据，不发起任何网络请求。

## 数据更新

本插件 `data/*.tsv` 来自 [pokemon-dataset-zh](https://github.com/42arch/pokemon-dataset-zh) 项目的渲染产物。更新数据：

```bash
cd /path/to/pokemon-dataset-zh
python3 build_render.py
cp render/{simple_pokedex,pokemon,ability_list,abilities,move_list,moves,item_list,pokedex,types}.tsv \
   /path/to/astrbot_plugin_pokedex/data/
```

注意：本插件依赖 `build_render.py` 的两处本地修改——`learn_names()` 为升级招式内嵌等级（`藤鞭(3)`，`—` 按 1 级）、`egg_moves` 列改用 `learn_names()` 渲染（原为 JSON 原文）。插件侧对两种格式（带/不带等级）均兼容，旧数据可正常运行但等级过滤不可用。

## 冠军赛数据（v0.3.0）

查询端（插件）与抓取端（独立脚本）完全解耦：插件只读 SQLite，不联网；数据新鲜度取决于你跑脚本的频率（建议每日一次，配合 cron / launchd 定时）。

### 抓取

```bash
cd /path/to/astrbot_plugin_pokedex
uv pip install -r requirements-scraper.txt        # playwright
uv run playwright install chromium                # 首次需下载浏览器

uv run python champions_fetch.py                  # 最新赛季 单打+双打，详情取 Top20
uv run python champions_fetch.py --top 50         # 单打配置明细多抓些
uv run python champions_fetch.py --all            # 全榜 262 只（较慢，约 15 分钟）
uv run python champions_fetch.py --season M-6     # 指定历史赛季
uv run python champions_fetch.py --pokemon garchomp   # 只补一只（调试）
uv run python champions_fetch.py --proxy none     # 强制直连（默认自动探测本机代理端口）
uv run python champions_fetch.py --headed         # 显示浏览器窗口（调试选择器）
```

行为说明：

- **存储位置**：默认 `AstrBot数据目录/data/plugin_data/astrbot_plugin_pokedex/champions.db`（自动识别，与插件读取路径一致）；可用 `--db` 或环境变量 `POKEDEX_CHAMPIONS_DB` 覆盖。
- **幂等**：所有写入走 `INSERT ... ON CONFLICT`，重复执行安全。抓取前比对页面「最后更新」时间戳，未变化自动跳过（`--force` 排名+详情都强制重抓；`--force-detail` 仅强制重抓配置明细，用于排名不变但想刷新明细时）。
- **频率**：脚本自身不做定时，请用系统 cron 每日跑一次，例如 `0 4 * * * cd /path/to/astrbot_plugin_pokedex && uv run python champions_fetch.py >> data/fetch.log 2>&1`。
- **代理**：默认依次尝试环境变量（`POKEMON_TRACKER_PROXY`/`HTTPS_PROXY`）与本机常见代理端口（7890/7897/34523 等），都不通则直连；`--proxy http://127.0.0.1:xxxx` 或 `--proxy none` 可显式指定。
- **单双打详情都要等水合**：详情页 SSR 固定输出**单打**数据，客户端水合后才按 URL 的 `format` 参数重渲染为双打（例如烈咬陆鲨单打招式第一是地震 65%，双打是守住 59%）。脚本对双打额外等待页面出现「双打」字样再解析；meta description 始终是单打的（含单打名次），双打详情不使用它，避免污染双打排名。排名页不提供使用率百分比，只知名次。

### 数据表

`champion_pokemon`（仅 id+名称，属性/种族值等基础信息不入库）、`champion_usage`（排名快照，含 source/season/format）、`champion_option`（配置使用率，option_type = move/item/ability/nature/ev_spread/teammate）、`champion_team`/`champion_team_member`（上位队伍，预留）、`champion_sync_log`（同步日志，驱动「未更新跳过」与查询结果的「本地更新时间」标注）。

### 选择器失效排查

所有 DOM 提取集中在 `champions_fetch.py` 顶部的 `JS_*` 常量中，基于**可见文本**（「招式」「道具」标签、`#12` 排名、`65%` 百分比）定位，不依赖易变 class。站点改版后：

```bash
uv run python champions_fetch.py --headed --pokemon garchomp   # 看真实渲染
uv run playwright codegen "https://pokechamdb.com/zh-Hans?view=pokemon"  # 录制新选择器
```

解析为空时脚本会重试 2 次并拒绝写入空数据，日志给出具体失败 URL。

## 测试

```bash
uv venv && uv pip install -r requirements.txt   # 或直接 pip install pillow aiohttp
uv run python tests.py
```

测试不依赖 AstrBot 运行时（main.py 通过 stub 导入，校验指令/llm_tool 注册与 docstring 参数类型标注）。

## 目录结构

```
astrbot_plugin_pokedex/
├── main.py            # 指令 + llm_tool 注册
├── pokedex_db.py      # 本地 TSV 查询（图鉴/属性表/招式表/配招/克制/队伍/筛选/立绘定位）
├── champions_db.py    # 冠军赛 SQLite 查询层（只读）+ 路径解析
├── champions_fetch.py # 冠军赛独立抓取脚本（Playwright，写入 champions.db）
├── battle_engine.py   # 能力值计算（25 性格）+ 回合制对战模拟引擎
├── pokeapi_client.py  # PokéAPI 客户端（补充数据源，带缓存，可配置关闭）
├── card_renderer.py   # Pillow 图片卡片渲染（参考 bazaar 插件，无浏览器依赖）
├── data/*.tsv         # 中文图鉴库（含 types.tsv 基准属性表）
├── tests.py           # 功能测试（uv run python tests.py）
├── metadata.yaml / _conf_schema.json / requirements.txt / requirements-scraper.txt
```
