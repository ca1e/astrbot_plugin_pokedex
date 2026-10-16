# astrbot_plugin_pokedex — 宝可梦图鉴插件

AstrBot 插件：内置 [pokemon-dataset-zh](https://github.com/42arch/pokemon-dataset-zh) 中文图鉴库，支持指令与 LLM 函数调用查询，图鉴结果以 **图片卡片** 展示。查询**优先使用本地中文数据**；PokéAPI 官方数据作为补充（可配置关闭，关闭后纯离线运行）。结构参考 [astrbot_plugin_bazaar](https://github.com/shitlsh/astrbot_plugin_bazaar)。

## 功能

- **本地中文图鉴**（打包于 `data/*.tsv`，由数据集项目 `build_render.py` 生成）：
  - `pokemon` 1320 行 = 1025 个种族默认行 + 295 个形态行（`form_name` 列）。查形态名（如 超级烈咬陆鲨Ｚ）直接命中形态行；查种族名优选默认形态并提示其他形态
  - `types` 18×18 基准属性表（由源数据按多数票推导，已与官方标准逐格核对）
  - 特性（总表 + 详情含持有者）、招式（总表 + 详情）、道具（1425）、24 个地区图鉴
- **指令**（群聊/私聊均可用）：

  | 指令 | 说明 |
  |---|---|
  | `宝可梦 <编号/名称/形态名>` | 图鉴详情，图片卡片展示（999 与 0999 等价） |
  | `特性 <名称>` / `招式 <名称>` / `道具 <名称>` | 对应数据查询 |
  | `图鉴 <编号/地区>` | 在 24 个地区图鉴中定位 |
  | `相性 <属性1> [属性2]` | 属性相性（本地属性表） |
  | `搜索 <关键词>` | 跨表模糊搜索 |
  | `宝可梦帮助` | 帮助 |

- **LLM 函数调用工具**（需模型供应商支持 function calling；群聊 @机器人 自然语言提问时自动调用）：
  - 本地：`query_pokemon` / `search_pokemon` / `query_ability` / `query_move` / `query_dex_region` / `query_type_matchups` / `find_pokemon_by_type`
  - PokéAPI 补充：`pokeapi_pokemon_detail`（官方译名/图鉴说明/英文特性效果）、`pokeapi_type_matchups`（官方倍率表）
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

## 目录结构

```
astrbot_plugin_pokedex/
├── main.py            # 指令 + llm_tool 注册
├── pokedex_db.py      # 本地 TSV 查询（编号/名称/形态匹配、属性表、立绘定位）
├── pokeapi_client.py  # PokéAPI 客户端（补充数据源，带缓存，可配置关闭）
├── card_renderer.py   # Pillow 图片卡片渲染（参考 bazaar 插件，无浏览器依赖）
├── data/*.tsv         # 中文图鉴库（含 types.tsv 基准属性表）
├── metadata.yaml / _conf_schema.json / requirements.txt
```
