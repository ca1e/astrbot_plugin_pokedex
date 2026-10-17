# astrbot_plugin_pokedex — 宝可梦图鉴插件

AstrBot 插件：内置 [pokemon-dataset-zh](https://github.com/42arch/pokemon-dataset-zh) 中文图鉴库，支持指令与 LLM 函数调用查询，图鉴结果以 **图片卡片** 展示。查询**优先使用本地中文数据**；PokéAPI 官方数据作为补充（可配置关闭，关闭后纯离线运行）。结构参考 [astrbot_plugin_bazaar](https://github.com/shitlsh/astrbot_plugin_bazaar)。

v0.2.0 起新增**计算与模拟**能力：等级能力值计算、双向属性相性、宝可梦对比、克制推荐、招式表/配招、对战模拟、队伍分析——全部基于本地数据，离线可用。

## 功能

- **本地中文图鉴**（打包于 `data/*.tsv`，由数据集项目 `build_render.py` 生成）：
  - `pokemon` 1320 行 = 1025 个种族默认行 + 295 个形态行（`form_name` 列），升级招式带等级（`藤鞭(3)`，初始招式计 1 级）。查形态名（如 超级烈咬陆鲨Ｚ）直接命中形态行；查种族名优选默认形态并提示其他形态
  - `types` 18×18 基准属性表（由源数据按多数票推导，已与官方标准逐格核对）
  - 特性（总表 + 详情含持有者）、招式（总表 + 详情）、道具（1425）、24 个地区图鉴
- **指令**（群聊/私聊均可用）：

  | 指令 | 说明 |
  |---|---|
  | `宝可梦 <编号/名称/形态名>` | 图鉴详情，图片卡片展示（999 与 0999 等价） |
  | `特性 <名称>` / `招式 <名称>` / `道具 <名称>` | 对应数据查询 |
  | `图鉴 <编号/地区>` | 在 24 个地区图鉴中定位 |
  | `相性 <属性1> [属性2]` | 属性相性（防守方向，含受击类特性修正提示，如「持避雷针免疫 电」） |
  | `克制 <属性>` | 进攻方向相性（"火属性打谁效果拔群"） |
  | `能力值 <名称> [等级] [个体] [性格]` | 官方公式计算等级能力值（含性格修正） |
  | `对比 <名称1> <名称2>` | 逐项种族值对比 + 属性互克倍率 + 特性 |
  | `怎么打 <名称>` | 推荐克制它的宝可梦（双向验证 + 种族值排序） |
  | `招式表 <名称> [等级]` | 可学招式（升级/学习器/遗传）+ 推荐配招 |
  | `对战 <名称1> <名称2> [等级]` | 回合制对战模拟（简化规则）+ 战报 |
  | `队伍分析 <名称...>` | 2-6 只队伍的弱点配置分析 |
  | `筛选 <条件>` | 按种族值筛选，如 `筛选 速度>100 特攻>=120` |
  | `搜索 <关键词>` | 跨表模糊搜索 |
  | `宝可梦帮助` | 帮助 |

- **LLM 函数调用工具**（需模型供应商支持 function calling；群聊 @机器人 自然语言提问时自动调用）：
  - 查询：`query_pokemon` / `search_pokemon` / `query_ability` / `query_move` / `query_dex_region` / `query_type_matchups` / `find_pokemon_by_type`
  - 计算/分析：`calc_pokemon_stats`（等级能力值）/ `query_type_offense`（进攻相性）/ `compare_pokemon`（对比）/ `find_pokemon_counters`（克制推荐）/ `query_learnset`（招式表）/ `validate_pokemon_moveset`（配招校验）/ `simulate_battle`（对战模拟）/ `analyze_team`（队伍分析）/ `find_pokemon_by_stats`（按种族值筛选）
  - PokéAPI 补充：`pokeapi_pokemon_detail`（官方译名/图鉴说明/英文特性效果）、`pokeapi_type_matchups`（官方倍率表）
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
├── battle_engine.py   # 能力值计算（25 性格）+ 回合制对战模拟引擎
├── pokeapi_client.py  # PokéAPI 客户端（补充数据源，带缓存，可配置关闭）
├── card_renderer.py   # Pillow 图片卡片渲染（参考 bazaar 插件，无浏览器依赖）
├── data/*.tsv         # 中文图鉴库（含 types.tsv 基准属性表）
├── tests.py           # 功能测试（uv run python tests.py）
├── metadata.yaml / _conf_schema.json / requirements.txt
```
