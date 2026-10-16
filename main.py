"""astrbot_plugin_pokedex — 宝可梦图鉴插件

本地中文图鉴库（pokemon-dataset-zh 生成，https://github.com/42arch/pokemon-dataset-zh）
+ Pillow 图片卡片为主，PokéAPI 官方数据作为补充（可在配置中关闭）。
属性相性优先使用本地 types.tsv 基准属性表。

指令：
  宝可梦 <编号或名称>   图鉴详情（图片卡片）
  特性 <名称>           特性查询
  招式 <名称>           招式查询
  道具 <名称>           道具查询
  图鉴 <编号或地区>     地区图鉴定位
  相性 <属性1> [属性2]  属性相性（本地属性表）
  搜索 <关键词>         跨表搜索
  宝可梦帮助            帮助
"""
import asyncio
from pathlib import Path

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp

try:
    from .pokedex_db import PokedexDB, TABLES
    from .pokeapi_client import PokeAPIClient
    from .card_renderer import CardRenderer
    from .wishmaker_card import build_wishmaker_markdown
except ImportError:  # 兼容以普通模块方式加载插件
    from pokedex_db import PokedexDB, TABLES
    from pokeapi_client import PokeAPIClient
    from card_renderer import CardRenderer
    from wishmaker_card import build_wishmaker_markdown

POKEMON_FIELDS = ("id_raw", "form_name", "name_zh", "name_en", "generation", "types", "category",
                  "height", "weight", "abilities", "hidden_abilities", "stats",
                  "weak_to", "resist_to", "immune_to", "evolution", "dex_entries")

WISHMAKER_NATURES = ("勤奋 怕寂寞 勇敢 固执 顽皮 大胆 坦率 悠闲 淘气 乐天 "
                     "胆小 急躁 认真 爽朗 天真 内敛 慢吞吞 温和 害羞 马虎 "
                     "沉着 温柔 自大 慎重 浮躁")


@register("astrbot_plugin_pokedex", "ca1e",
          "宝可梦图鉴：本地中文数据 + 图片卡片 + PokéAPI 补充",
          "v0.1.0")
class PokedexPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        images_dir = self.config.get("images_dir") or ""
        self.db = PokedexDB(Path(__file__).resolve().parent / "data")
        self.renderer = CardRenderer(images_dir or None)
        self.api = PokeAPIClient(timeout=int(self.config.get("pokeapi_timeout", 15)))

    async def terminate(self):
        await self.api.close()

    # ---------- 内部工具 ----------
    async def _artwork_bytes(self, row: dict) -> bytes | None:
        """本地立绘缺失时按配置回退到 PokéAPI 官方图。

        网络官方图只有默认形态，形态行不用它，避免张冠李戴。
        """
        if self.renderer.images_dir and self.db.find_image(row, self.renderer.images_dir):
            return None  # 渲染器可直接读本地文件
        if row.get("form_name") or not self.config.get("fallback_web_image", True):
            return None
        try:
            url = (f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/"
                   f"pokemon/other/official-artwork/{int(row.get('id', 0))}.png")
            if self.api._session is None or self.api._session.closed:
                self.api._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.api.timeout))
            async with self.api._session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception as e:
            logger.warning(f"网络立绘获取失败: {e}")
        return None

    async def _send_pokemon(self, event: AstrMessageEvent, row: dict, card: bool, hint: str = ""):
        text = self.db.format_row(row, fields=POKEMON_FIELDS) + hint
        if card and self.config.get("enable_image_card", True):
            img_bytes = await self._artwork_bytes(row)
            png = await self.renderer.render_pokemon(row, self.db, img_bytes)
            if png:
                comps = [Comp.Image.fromBytes(png)]
                if hint:
                    comps.append(Comp.Plain(hint))
                yield event.chain_result(comps)
                return
            logger.warning("卡片渲染失败，回退文本")
        yield event.plain_result(text)

    # ---------- 指令 ----------
    @filter.command("宝可梦")
    async def cmd_pokemon(self, event: AstrMessageEvent, keyword: str = ""):
        """查询宝可梦图鉴（编号或名称），图片卡片展示"""
        if not keyword:
            yield event.plain_result("用法：宝可梦 <编号或名称>，如：宝可梦 999 / 宝可梦 索财灵")
            return
        rows = self.db.query("pokemon", keyword)
        if not rows:
            yield event.plain_result(f"没有查到「{keyword}」的相关信息")
            return
        row = self.db.pick_best(rows, keyword)
        others = sorted({r["form_name"] for r in rows if r.get("form_name") and r is not row})
        hint = ""
        if others:
            hint = (f"\n\n———\n还匹配到其他形态：{'、'.join(others)}\n"
                    f"发送「宝可梦 {others[0]}」查看对应形态")
        async for r in self._send_pokemon(event, row, card=True, hint=hint):
            yield r

    @filter.command("特性")
    async def cmd_ability(self, event: AstrMessageEvent, keyword: str = ""):
        """查询特性（效果与持有宝可梦）"""
        if not keyword:
            yield event.plain_result("用法：特性 <名称>，如：特性 威吓")
            return
        rows = self.db.query("abilities", keyword) or self.db.query("ability", keyword)
        if not rows:
            yield event.plain_result(f"没有查到特性「{keyword}」的相关信息")
            return
        row = rows[0]
        text = (f"特性 {row.get('name_zh', '')}（{row.get('name_en', '')}）\n"
                f"介绍：{row.get('introduction', '')}\n"
                f"效果：{row.get('effect', '')[:400]}\n"
                f"生效条件：{row.get('basic_info', '')}\n"
                f"持有宝可梦：{(row.get('holders', '') or '')[:300]}")
        yield event.plain_result(text)

    @filter.command("招式")
    async def cmd_move(self, event: AstrMessageEvent, keyword: str = ""):
        """查询招式（总表优先，详情补充）"""
        if not keyword:
            yield event.plain_result("用法：招式 <名称>，如：招式 十万伏特")
            return
        rows = self.db.query("move", keyword)
        if not rows:
            detail = self.db.query("moves", keyword)
            if detail:
                rows = [{k: detail[0].get(k, "") for k in
                         ("id", "name_zh", "name_en", "type", "category", "power",
                          "accuracy", "pp", "range", "description", "additional_effect")}]
        if not rows:
            yield event.plain_result(f"没有查到招式「{keyword}」的相关信息")
            return
        r = rows[0]
        text = (f"招式 {r.get('name_zh', '')}（{r.get('name_en', '')}）\n"
                f"{r.get('type', '')}属性·{r.get('category', '')} | 威力 {r.get('power', '')} | "
                f"命中 {r.get('accuracy', '')} | PP {r.get('pp', '')}\n"
                f"说明：{r.get('description', '')}")
        if r.get("additional_effect"):
            text += f"\n追加效果：{r['additional_effect'][:200]}"
        yield event.plain_result(text)

    @filter.command("道具")
    async def cmd_item(self, event: AstrMessageEvent, keyword: str = ""):
        """查询道具"""
        if not keyword:
            yield event.plain_result("用法：道具 <名称>，如：道具 除虫喷雾")
            return
        rows = self.db.query("item", keyword)
        if not rows:
            yield event.plain_result(f"没有查到道具「{keyword}」的相关信息")
            return
        r = rows[0]
        yield event.plain_result(f"道具 {r.get('name_zh', '')}（{r.get('name_en', '')}）\n"
                                 f"类别：{r.get('category_path', '')}\n说明：{r.get('description', '')}")

    @filter.command("图鉴")
    async def cmd_dex(self, event: AstrMessageEvent, keyword: str = ""):
        """在 24 个地区图鉴中定位（编号或地区名）"""
        if not keyword:
            yield event.plain_result("用法：图鉴 <编号> 或 图鉴 <地区名>，如：图鉴 999 / 图鉴 丰缘")
            return
        rows = self.db.query("pokedex", keyword)
        if not rows:
            yield event.plain_result(f"没有查到「{keyword}」的图鉴记录")
            return
        lines = []
        for r in rows[:10]:
            if r.get("dex") == "national":
                lines.append(f"全国图鉴 {r['id_raw']}：{r.get('name', '')}（{r.get('types', '')}）")
            else:
                lines.append(f"{r.get('dex', '')}图鉴 {r.get('id_raw', '')}"
                             f"（全国 {r.get('national_id', '')}）：{r.get('name', '')}（{r.get('types', '')}）")
        yield event.plain_result("\n".join(lines))

    @filter.command("相性")
    async def cmd_matchups(self, event: AstrMessageEvent, type1: str = "", type2: str = ""):
        """属性相性（本地属性表），如：相性 火 飞行"""
        if not type1:
            yield event.plain_result("用法：相性 <属性1> [属性2]，如：相性 火 飞行")
            return
        result = self.db.matchups([t for t in (type1, type2) if t])
        if result is None:
            yield event.plain_result(f"没有查到属性「{type1}{'/' + type2 if type2 else ''}」")
            return
        combo = "/".join(x for x in (type1, type2) if x)
        lines = [f"【{combo}】属性相性"]
        lines += ["弱点：" + "、".join(result["weak"])] if result["weak"] else ["弱点：无"]
        lines += ["抗性：" + "、".join(result["resist"])] if result["resist"] else ["抗性：无"]
        lines += ["免疫：" + "、".join(result["immune"])] if result["immune"] else []
        yield event.plain_result("\n".join(lines))

    @filter.command("许愿星")
    async def cmd_wishmaker(self, event: AstrMessageEvent, nature: str = "",
                            min_iv: str = "", tid: str = "", sid: str = ""):
        """许愿星基拉祈目标筛选（红/蓝宝石），如：许愿星 固执 25 20040 0"""
        if not nature:
            yield event.plain_result(
                "用法：许愿星 <性格> [个体下限] [TID SID]\n"
                "性格：\n" + WISHMAKER_NATURES + "\n"
                "如：许愿星 固执 25 = 六项个体≥25 的固执；末尾附 TID SID 可标注异色")
            return
        try:
            iv_floor = max(0, min(31, int(min_iv))) if min_iv else 0
            tsv = (int(tid) ^ int(sid)) if tid and sid else None
        except ValueError:
            yield event.plain_result("个体下限 / TID / SID 需要是数字")
            return
        count, rows = self.db.wishmaker_query(nature=nature, min_iv=iv_floor, tsv=tsv)
        if not count:
            yield event.plain_result(f"没有符合的目标（性格请从：{WISHMAKER_NATURES} 中选择）")
            return
        # 默认图片卡片；wishmaker_style=markdown 时走 markdown（QQ 官方 / 微信个人号等支持渲染的平台）
        if self.config.get("wishmaker_style", "image") == "markdown":
            result = event.plain_result(
                build_wishmaker_markdown(nature, iv_floor, count, rows, tsv))
            result.use_markdown(True)  # 仅支持的平台消费该标记，其他平台忽略
            yield result
            return
        try:
            png = self.renderer.render_wishmaker_card(nature, iv_floor, count, rows, tsv)
            yield event.chain_result([Comp.Image.fromBytes(png)])
        except Exception as e:
            logger.warning(f"许愿星卡片渲染失败: {e}")
            yield event.plain_result("许愿星卡片渲染失败，请稍后再试")

    @staticmethod
    def _format_wishmaker(nature: str, iv_floor: int, count: int, rows: list[dict]) -> str:
        lines = [f"【许愿星基拉祈】性格 {nature or '任意'} · 个体≥{iv_floor}（命中 {count} 个种子）"]
        for i, r in enumerate(rows, 1):
            ivs = "/".join(r[k] for k in ("hp", "atk", "def", "spa", "spd", "spe"))
            mark = " ✨异色" if r.get("shiny") else ""
            lines.append(f"{i}. 种子 {r['seed']} | PID {r['pid']} | 个体 {ivs}（总和{r['total']}）"
                         f"| 觉醒力量 {r['hp_type']}{r['hp_power']}{mark}")
        lines.append("提示：种子 = 红/蓝宝石接收许愿星基拉祈时的 WishMaker 种子（16位十六进制）")
        return "\n".join(lines)

    @filter.command("搜索")
    async def cmd_search(self, event: AstrMessageEvent, keyword: str = ""):
        """跨表搜索名称"""
        if not keyword:
            yield event.plain_result("用法：搜索 <关键词>")
            return
        found = self.db.search(keyword)
        if not found:
            yield event.plain_result(f"没有查到「{keyword}」的相关信息")
            return
        lines = []
        for table, rows in found.items():
            for r in rows[:5]:
                parts = [f"{r.get(c, '')}" for c in ("id_raw", "national_id", "name_zh", "name_en")
                         if r.get(c)]
                lines.append(f"[{TABLES[table]}] {' | '.join(parts)}")
        yield event.plain_result("\n".join(lines[:15]))

    @filter.command("宝可梦帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """查看宝可梦插件帮助"""
        yield event.plain_result(
            "🎮 宝可梦图鉴插件\n"
            "宝可梦 <编号/名称> — 图鉴详情（图片卡片）\n"
            "特性 <名称> — 特性效果与持有者\n"
            "招式 <名称> — 招式数据\n"
            "道具 <名称> — 道具说明\n"
            "图鉴 <编号/地区> — 地区图鉴定位\n"
            "相性 <属性1> [属性2] — 属性相性（本地属性表）\n"
            "许愿星 <性格> [个体下限] [TID SID] — 许愿星基拉祈目标筛选\n"
            "搜索 <关键词> — 跨表搜索\n"
            "群聊 @我 直接提问时也会自动调用图鉴工具（需模型支持函数调用）")

    # ---------- LLM 函数调用工具 ----------
    @filter.llm_tool(name="query_pokemon")
    async def tool_query_pokemon(self, event: AstrMessageEvent, keyword: str):
        """查询宝可梦图鉴详情（中文）。适用于用户询问某只宝可梦的属性、种族值、特性、
        弱点、进化方式等。返回中文图鉴数据，回答时直接引用，不要自行翻译或编造。
        Args:
            keyword(str): 宝可梦的全国图鉴编号或名称（中/日/英均可）
        """
        rows = self.db.query("pokemon", keyword)
        if not rows:
            return f"没有查到「{keyword}」的相关信息"
        row = self.db.pick_best(rows, keyword)
        others = sorted({r["form_name"] for r in rows if r.get("form_name") and r is not row})
        text = self.db.format_row(row, fields=POKEMON_FIELDS)
        if others:
            text += f"\n其他形态（可用 query_pokemon 按形态名查询）：{'、'.join(others)}"
        return text

    @filter.llm_tool(name="search_pokemon")
    async def tool_search_pokemon(self, event: AstrMessageEvent, keyword: str):
        """模糊搜索宝可梦/特性/招式/道具名称，返回候选列表。适用于用户只记得部分名字时。
        Args:
            keyword(str): 名称关键词（支持片段）
        """
        found = self.db.search(keyword)
        if not found:
            return f"没有查到「{keyword}」的相关信息"
        out = []
        for table, rows in found.items():
            for r in rows[:5]:
                parts = [f"{r.get(c, '')}" for c in ("id_raw", "national_id", "name_zh", "name_en")
                         if r.get(c)]
                out.append(f"[{TABLES[table]}] {' | '.join(parts)}")
        return "\n".join(out)

    @filter.llm_tool(name="query_ability")
    async def tool_query_ability(self, event: AstrMessageEvent, keyword: str):
        """查询特性（ability）的效果与持有宝可梦。返回中文数据，直接引用即可。
        Args:
            keyword(str): 特性名称（中文或英文）
        """
        rows = self.db.query("abilities", keyword) or self.db.query("ability", keyword)
        if not rows:
            return f"没有查到特性「{keyword}」的相关信息"
        r = rows[0]
        return (f"特性 {r.get('name_zh', '')}（{r.get('name_en', '')}）\n"
                f"介绍：{r.get('introduction', '')}\n效果：{r.get('effect', '')[:600]}\n"
                f"持有宝可梦：{(r.get('holders', '') or '')[:400]}")

    @filter.llm_tool(name="query_move")
    async def tool_query_move(self, event: AstrMessageEvent, keyword: str):
        """查询招式（move）的属性、威力、命中、PP 与说明。返回中文数据。
        Args:
            keyword(str): 招式名称（中文或英文）
        """
        rows = self.db.query("move", keyword)
        if rows:
            r = rows[0]
            fields = ("id", "name_zh", "name_en", "type", "category", "power",
                      "accuracy", "pp", "generation", "description")
            return self.db.format_row(r, fields=fields)
        detail = self.db.query("moves", keyword)
        if not detail:
            return f"没有查到招式「{keyword}」的相关信息"
        r = detail[0]
        return self.db.format_row(r, fields=("name_zh", "name_en", "type", "category",
                                             "power", "accuracy", "pp", "range",
                                             "description", "additional_effect"))

    @filter.llm_tool(name="query_dex_region")
    async def tool_query_dex_region(self, event: AstrMessageEvent, keyword: str):
        """查询宝可梦在地区图鉴中的位置，或某地区图鉴的收录范围。返回中文数据。
        Args:
            keyword(str): 全国图鉴编号或地区名（如 999、丰缘、帕底亚）
        """
        rows = self.db.query("pokedex", keyword)
        if not rows:
            return f"没有查到「{keyword}」的图鉴记录"
        out = []
        for r in rows[:15]:
            if r.get("dex") == "national":
                out.append(f"全国图鉴 {r['id_raw']}：{r.get('name', '')}（{r.get('types', '')}，"
                           f"第 {r.get('generation', '?')} 世代）")
            else:
                out.append(f"{r.get('dex', '')}图鉴 {r.get('id_raw', '')}"
                           f"（全国 {r.get('national_id', '')}）：{r.get('name', '')}")
        return "\n".join(out)

    @filter.llm_tool(name="query_type_matchups")
    async def tool_query_type_matchups(self, event: AstrMessageEvent, type1: str, type2: str = ""):
        """查询属性相性倍率（本地属性表）。适用于询问某属性（或双属性组合）怕什么、抗什么、免疫什么。
        Args:
            type1(str): 第一个属性（中文，如 火）
            type2(str, optional): 第二个属性（可选，如 飞行）
        """
        result = self.db.matchups([t for t in (type1, type2) if t])
        if result is None:
            return f"没有查到属性「{type1}{'/' + type2 if type2 else ''}」"
        combo = "/".join(x for x in (type1, type2) if x)
        return (f"【{combo}】\n"
                f"弱点：{'、'.join(result['weak']) or '无'}\n"
                f"抗性：{'、'.join(result['resist']) or '无'}\n"
                f"免疫：{'、'.join(result['immune']) or '无'}")

    @filter.llm_tool(name="query_wishmaker_jirachi")
    async def tool_query_wishmaker(self, event: AstrMessageEvent, nature: str = "",
                                   min_iv: int = 0, hp_type: str = "",
                                   tid: int = -1, sid: int = -1,
                                   shiny_only: bool = False):
        """查询红/蓝宝石"许愿星基拉祈"（Bonus Disc 领取）的目标种子与个体值。
        覆盖全部 65536 个 WishMaker 种子（与 pokemonrng.com 教程数据一致），
        可按目标性格、六项个体值下限、觉醒力量属性筛选，提供 TID/SID 时可判定异色。
        Args:
            nature(str): 目标性格中文名（勤奋/怕寂寞/勇敢/固执/顽皮/大胆/坦率/悠闲/淘气/乐天/胆小/急躁/认真/爽朗/天真/内敛/慢吞吞/温和/害羞/马虎/沉着/温柔/自大/慎重/浮躁），留空不限
            min_iv(int): 六项个体值的下限（0-31），如 25 表示每项都≥25
            hp_type(str): 觉醒力量属性中文名（格斗/飞行/毒/地面/岩石/虫/幽灵/钢/火/水/草/电/超能力/冰/龙/恶），留空不限
            tid(int): 训练家 ID（异色判定用），不知道传 -1
            sid(int): 隐藏 ID（异色判定用），不知道传 -1
            shiny_only(bool): 只看异色目标（需提供 tid 和 sid）
        """
        tsv = (tid ^ sid) if tid >= 0 and sid >= 0 else None
        iv_floor = max(0, min(31, min_iv))
        count, rows = self.db.wishmaker_query(nature=nature or None, min_iv=iv_floor,
                                              hp_type=hp_type or None, tsv=tsv,
                                              shiny_only=shiny_only, limit=10)
        if not count:
            return "没有符合条件的目标，请放宽个体值下限或核对性格/属性名称"
        return self._format_wishmaker(nature, iv_floor, count, rows)

    @filter.llm_tool(name="find_pokemon_by_type")
    async def tool_find_by_type(self, event: AstrMessageEvent, type_name: str):
        """列出某属性的宝可梦（默认形态）。适用于"电属性宝可梦有哪些"这类问题。
        Args:
            type_name(str): 属性中文名（如 电、水）
        """
        hits = self.db.by_type(type_name)
        if not hits:
            return f"没有查到「{type_name}」属性的宝可梦"
        return f"{type_name}属性宝可梦（共 {len(hits)} 只，列出前 40）：\n" + \
               "、".join(f"{r['name_zh']}({r['id_raw']})" for r in hits)

    @filter.llm_tool(name="pokeapi_pokemon_detail")
    async def tool_pokeapi_detail(self, event: AstrMessageEvent, name_or_id: str):
        """从 PokéAPI 查询宝可梦官方数据（含中文官方译名与图鉴说明、特性英文效果）。
        本地图鉴查不到、或用户需要官方英文特性效果时使用。
        Args:
            name_or_id(str): 宝可梦的英文名或全国图鉴编号
        """
        if not self.config.get("pokeapi_enabled", True):
            return "PokéAPI 已在配置中禁用"
        try:
            data = await self.api.get_pokemon(name_or_id)
        except Exception as e:
            logger.warning(f"PokéAPI 请求失败: {e}")
            return "PokéAPI 查询失败，请稍后再试"
        if not data:
            return f"没有查到「{name_or_id}」的相关信息"
        stats = "、".join(f"{k}{v}" for k, v in data["stats"].items())
        abilities = "、".join(f"{a['name_zh'] or a['name_en']}({'隐藏特性，' if a['hidden'] else ''}{a['effect']})"
                              for a in data["abilities"])
        return (f"{data['name_zh']}（{data['name_en']}）#{data['id']}，第 {data['generation']} 世代，"
                f"{data['genus']}\n属性：{data['types']} | 身高 {data['height_m']}m | 体重 {data['weight_kg']}kg\n"
                f"种族值：{stats}\n特性：{abilities}\n图鉴：{data['flavor']}")

    @filter.llm_tool(name="pokeapi_type_matchups")
    async def tool_pokeapi_matchups(self, event: AstrMessageEvent, type1: str, type2: str = ""):
        """从 PokéAPI 查询属性相性倍率。query_type_matchups 查不到或需要官方倍率表时使用。
        Args:
            type1(str): 第一个属性（中文，如 火）
            type2(str, optional): 第二个属性（可选，如 飞行）
        """
        if not self.config.get("pokeapi_enabled", True):
            return "PokéAPI 已在配置中禁用"
        try:
            result = await self.api.get_type_matchups([t for t in (type1, type2) if t])
        except Exception as e:
            logger.warning(f"PokéAPI 请求失败: {e}")
            return "PokéAPI 查询失败，请稍后再试"
        if result is None:
            return f"没有查到属性「{type1}{'/' + type2 if type2 else ''}」"
        combo = "/".join(x for x in (type1, type2) if x)
        return (f"【{combo}】\n"
                f"弱点：{'、'.join(result['weak']) or '无'}\n"
                f"抗性：{'、'.join(result['resist']) or '无'}\n"
                f"免疫：{'、'.join(result['immune']) or '无'}")
