"""astrbot_plugin_pokedex — 宝可梦图鉴插件

本地中文图鉴库（pokemon-dataset-zh 生成，https://github.com/42arch/pokemon-dataset-zh）
+ Pillow 图片卡片为主，PokéAPI 官方数据作为补充（可在配置中关闭）。
属性相性优先使用本地 types.tsv 基准属性表。

设计原则：LLM 工具保持原子化（供模型组合调用）；聊天指令只保留粗粒度
功能模块，不镜像每个原子能力（跨表搜索等原子能力仅存在于工具层）。

指令：
  宝可梦                帮助（宝可梦 <名> 招式表/能力值 … 为其子视图）
  宝可梦 <编号或名称>   图鉴详情（图片卡片，含冠军赛名次）
  特性/招式/道具 <名称> 数据查询
  相性 <属性1> [属性2]  属性相性（相性 打 <属性> = 进攻方向）
  克制 <宝可梦> [B]     克制推荐 / 多维度对比
  对战/队伍分析/筛选    组合分析
  宝冠军 <关键词>        冠军赛一站式查询
  许愿星 <…>            许愿星基拉祈 RNG 筛选
"""

import re
from pathlib import Path
from typing import ClassVar

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from .battle_engine import (
        NATURES,
        STAT_KEYS,
        BattlePokemon,
        BattleSimulator,
        calc_stats,
        make_move,
    )
    from .card_renderer import CardRenderer
    from .champions_db import (
        OPTION_LABELS,
        ChampionsDB,
        fmt_label,
        source_label,
    )
    from .pokeapi_client import PokeAPIClient
    from .pokedex_db import TABLES, PokedexDB
    from .wishmaker_card import build_wishmaker_markdown
except ImportError:  # 兼容以普通模块方式加载插件
    from battle_engine import (
        NATURES,
        STAT_KEYS,
        BattlePokemon,
        BattleSimulator,
        calc_stats,
        make_move,
    )
    from card_renderer import CardRenderer
    from champions_db import (
        OPTION_LABELS,
        ChampionsDB,
        fmt_label,
        source_label,
    )
    from pokeapi_client import PokeAPIClient
    from pokedex_db import TABLES, PokedexDB
    from wishmaker_card import build_wishmaker_markdown

STAT_COND_RE = re.compile(
    r"^(HP|攻击|防御|特攻|特防|速度|总和)\s*(>=|<=|>|<|=)\s*(\d{1,4})$"
)


def parse_stat_conditions(text: str) -> list[tuple[str, str, int]] | None:
    """解析种族值筛选条件，如 "速度>100 特攻>=120 总和<=500"；全部合法才返回列表。"""
    tokens = [t for t in text.replace("，", " ").replace("、", " ").split() if t]
    if not tokens:
        return None
    conds = []
    for tok in tokens:
        m = STAT_COND_RE.match(tok.strip())
        if not m:
            return None
        conds.append((m.group(1), m.group(2), int(m.group(3))))
    return conds


POKEMON_FIELDS = (
    "id_raw",
    "form_name",
    "name_zh",
    "name_en",
    "generation",
    "types",
    "category",
    "height",
    "weight",
    "abilities",
    "hidden_abilities",
    "stats",
    "weak_to",
    "resist_to",
    "immune_to",
    "evolution",
    "dex_entries",
)

WISHMAKER_NATURES = (
    "勤奋 怕寂寞 勇敢 固执 顽皮 大胆 坦率 悠闲 淘气 乐天 "
    "胆小 急躁 认真 爽朗 天真 内敛 慢吞吞 温和 害羞 马虎 "
    "沉着 温柔 自大 慎重 浮躁"
)


@register(
    "astrbot_plugin_pokedex",
    "ca1e",
    "宝可梦图鉴：本地中文数据 + 图片卡片 + PokéAPI 补充",
    "v0.1.0",
)
class PokedexPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        images_dir = self.config.get("images_dir") or ""
        self.db = PokedexDB(Path(__file__).resolve().parent / "data")
        self.renderer = CardRenderer(images_dir or None)
        self.api = PokeAPIClient(timeout=int(self.config.get("pokeapi_timeout", 15)))
        # 冠军赛季数据：只读 champions.db（由独立脚本 champions_fetch.py 抓取）
        self.champions = ChampionsDB(self.config.get("champions_db_path") or None)

    async def terminate(self):
        await self.api.close()

    # ---------- 内部工具 ----------
    async def _artwork_bytes(self, row: dict) -> bytes | None:
        """本地立绘缺失时按配置回退到 PokéAPI 官方图。

        网络官方图只有默认形态，形态行不用它，避免张冠李戴。
        """
        if self.renderer.images_dir and self.db.find_image(
            row, self.renderer.images_dir
        ):
            return None  # 渲染器可直接读本地文件
        if row.get("form_name") or not self.config.get("fallback_web_image", True):
            return None
        try:
            url = (
                f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/"
                f"pokemon/other/official-artwork/{int(row.get('id', 0))}.png"
            )
            if self.api._session is None or self.api._session.closed:
                self.api._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.api.timeout)
                )
            async with self.api._session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception as e:
            logger.warning(f"网络立绘获取失败: {e}")
        return None

    def _champ_card_info(self, row: dict) -> dict | None:
        """图鉴卡片头部的冠军赛名次条数据；无数据/形态行返回 None。"""
        if not self.champions.exists or row.get("form_name"):
            return None
        ranks = self.champions.pokemon_ranks(row.get("name_zh", ""))
        if not ranks:
            return None
        return {
            "season": ranks[0]["season"],
            "total": max(r["total"] for r in ranks),
            "single": next((r["rank"] for r in ranks if r["format"] == "single"), None),
            "double": next((r["rank"] for r in ranks if r["format"] == "double"), None),
        }

    async def _send_pokemon(
        self, event: AstrMessageEvent, row: dict, card: bool, hint: str = ""
    ):
        text = self.db.format_row(row, fields=POKEMON_FIELDS) + hint
        if card and self.config.get("enable_image_card", True):
            img_bytes = await self._artwork_bytes(row)
            png = await self.renderer.render_pokemon(
                row, self.db, img_bytes, champion=self._champ_card_info(row)
            )
            if png:
                comps = [Comp.Image.fromBytes(png)]
                if hint:
                    comps.append(Comp.Plain(hint))
                yield event.chain_result(comps)
                return
            logger.warning("卡片渲染失败，回退文本")
        yield event.plain_result(text)

    def _pokemon_related_hint(self, row: dict) -> str:
        """图鉴卡片尾部的关联指令反查提示（冠军赛名次 + 常用后续查询）。"""
        name = row.get("name_zh", "")
        if not name:
            return ""
        parts = []
        champ = self._champ_rank_short(name)
        if champ:
            parts.append(f"冠军赛 {champ}（宝冠军 {name} 看配置）")
        if not row.get("form_name"):
            parts.append(f"宝可梦 {name} 招式表")
            parts.append(f"宝可梦 {name} 能力值 50")
        parts.append(f"克制 {name}")
        return "\n———\n相关：" + "｜".join(parts)

    # ---------- 指令 ----------
    @filter.command("宝可梦")
    async def cmd_pokemon(self, event: AstrMessageEvent, keyword: str = ""):
        """宝可梦图鉴卡；宝可梦 <名> 招式表 [等级]｜宝可梦 <名> 能力值 [等级 个体 性格]"""
        tokens = (keyword or "").split()
        if tokens and tokens[0] in {"招式表", "能力值"}:
            yield event.plain_result(
                "用法：宝可梦 <名称> 招式表 [等级]\n"
                "　　宝可梦 <名称> 能力值 [等级 个体 性格]\n"
                "如：宝可梦 皮卡丘 招式表 50 / 宝可梦 皮卡丘 能力值 50 31 固执"
            )
            return
        name = tokens[0] if tokens else ""
        if not name:
            yield event.plain_result(self._help_text())
            return
        sub = tokens[1] if len(tokens) > 1 else ""
        if sub == "招式表":
            level = tokens[2] if len(tokens) > 2 else ""
            async for r in self._learnset_view(event, name, level):
                yield r
            return
        if sub == "能力值":
            level = tokens[2] if len(tokens) > 2 else "50"
            iv = tokens[3] if len(tokens) > 3 else "31"
            nature = tokens[4] if len(tokens) > 4 else ""
            async for r in self._stats_view(event, name, level, iv, nature):
                yield r
            return
        rows = self.db.query("pokemon", name)
        if not rows:
            yield event.plain_result(f"没有查到「{keyword}」的相关信息")
            return
        row = self.db.pick_best(rows, keyword)
        others = sorted(
            {r["form_name"] for r in rows if r.get("form_name") and r is not row}
        )
        hint = self._pokemon_related_hint(row)
        if others:
            hint += (
                f"\n\n———\n还匹配到其他形态：{'、'.join(others)}\n"
                f"发送「宝可梦 {others[0]}」查看对应形态"
            )
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
        text = (
            f"特性 {row.get('name_zh', '')}（{row.get('name_en', '')}）\n"
            f"介绍：{row.get('introduction', '')}\n"
            f"效果：{row.get('effect', '')[:400]}\n"
            f"生效条件：{row.get('basic_info', '')}\n"
            f"持有宝可梦：{(row.get('holders', '') or '')[:300]}"
        )
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
                rows = [
                    {
                        k: detail[0].get(k, "")
                        for k in (
                            "id",
                            "name_zh",
                            "name_en",
                            "type",
                            "category",
                            "power",
                            "accuracy",
                            "pp",
                            "range",
                            "description",
                            "additional_effect",
                        )
                    }
                ]
        if not rows:
            yield event.plain_result(f"没有查到招式「{keyword}」的相关信息")
            return
        r = rows[0]
        text = (
            f"招式 {r.get('name_zh', '')}（{r.get('name_en', '')}）\n"
            f"{r.get('type', '')}属性·{r.get('category', '')} | 威力 {r.get('power', '')} | "
            f"命中 {r.get('accuracy', '')} | PP {r.get('pp', '')}\n"
            f"说明：{r.get('description', '')}"
        )
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
        yield event.plain_result(
            f"道具 {r.get('name_zh', '')}（{r.get('name_en', '')}）\n"
            f"类别：{r.get('category_path', '')}\n说明：{r.get('description', '')}"
        )

    @staticmethod
    def _ability_correction_text(result: dict) -> str:
        """把 matchups 的 ability_corrections 压成一行提示，无修正返回空串。"""
        parts = []
        for c in result.get("ability_corrections", []):
            abil = "/".join(c["abilities"])
            cur = "" if c["current_mult"] == 1 else f"（现×{c['current_mult']:g}）"
            parts.append(f"持「{abil}」免疫 {c['type']}{cur}")
        return "特性修正：" + "；".join(parts) if parts else ""

    @filter.command("相性")
    async def cmd_matchups(
        self, event: AstrMessageEvent, type1: str = "", type2: str = ""
    ):
        """属性相性（本地属性表）：相性 火 飞行 = 防守；相性 打 火 = 进攻"""
        offense = False
        if type1 in {"打", "进攻", "攻击"}:  # 相性 打 火
            offense = True
            type1, type2 = type2, ""
        if not type1:
            yield event.plain_result(
                "用法：相性 <属性1> [属性2] = 防守方向；相性 打 <属性> = 进攻方向\n"
                "如：相性 火 飞行 / 相性 打 火"
            )
            return
        if offense:
            async for r in self._offense_text(event, type1):
                yield r
            return
        result = self.db.matchups([t for t in (type1, type2) if t])
        if result is None:
            yield event.plain_result(
                f"没有查到属性「{type1}{'/' + type2 if type2 else ''}」"
            )
            return
        combo = "/".join(x for x in (type1, type2) if x)
        lines = [f"【{combo}】属性相性"]
        lines += (
            ["弱点：" + "、".join(result["weak"])] if result["weak"] else ["弱点：无"]
        )
        lines += (
            ["抗性：" + "、".join(result["resist"])]
            if result["resist"]
            else ["抗性：无"]
        )
        lines += ["免疫：" + "、".join(result["immune"])] if result["immune"] else []
        corr = self._ability_correction_text(result)
        if corr:
            lines.append(corr)
        yield event.plain_result("\n".join(lines))

    async def _offense_text(self, event: AstrMessageEvent, type_name: str):
        """进攻方向相性（原「克制」指令体）。"""
        result = self.db.attack_matchups(type_name)
        if result is None:
            yield event.plain_result(f"没有查到属性「{type_name}」")
            return
        lines = [f"【{type_name}属性攻击】"]
        lines.append("效果拔群：" + ("、".join(result["super"]) or "无"))
        lines.append("收效甚微：" + ("、".join(result["weak"]) or "无"))
        if result["none"]:
            lines.append("无效：" + "、".join(result["none"]))
        yield event.plain_result("\n".join(lines))

    # ---------- Phase 1：能力值 / 进攻相性 / 对比 / 怎么打 ----------
    def _stats_base(self, row: dict) -> dict | None:
        """从种族值文本解析 {能力项: 种族值}。"""
        parsed = self.db.first_stats(row)
        return dict(parsed[0]) if parsed else None

    def _calc_stats_text(self, row: dict, level: int, iv: int, nature: str) -> str:
        base = self._stats_base(row)
        if not base:
            return "该宝可梦缺少种族值数据，无法计算"
        stats = calc_stats(base, level, iv=iv, nature=nature or None)
        head = (
            f"{row.get('name_zh', '')}（{row.get('types', '')}）Lv.{level}"
            f"｜个体 {iv}、努力 0" + (f"、性格 {nature}" if nature else "")
        )
        return (
            head
            + "\n"
            + "、".join(f"{k} {stats[k]}" for k in STAT_KEYS)
            + f"\n总和 {stats['总和']}"
        )

    @staticmethod
    def _parse_clamped_int(text: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(text)))
        except (TypeError, ValueError):
            return default

    async def _stats_view(
        self, event: AstrMessageEvent, name: str, level: str, iv: str, nature: str
    ):
        """「宝可梦 <名> 能力值 …」子视图：等级能力值计算。"""
        row = self.db.pokemon(name)
        if not row:
            yield event.plain_result(f"没有查到「{name}」的相关信息")
            return
        if nature and nature not in NATURES:
            yield event.plain_result(
                f"没有性格「{nature}」，可选：{'、'.join(NATURES)}"
            )
            return
        level_i = self._parse_clamped_int(level, 50, 1, 100)
        iv_i = self._parse_clamped_int(iv, 31, 0, 31)
        yield event.plain_result(self._calc_stats_text(row, level_i, iv_i, nature))

    @staticmethod
    def _stat_compare_line(label: str, v1: int, v2: int, n1: str, n2: str) -> str:
        if v1 > v2:
            win = n1
        elif v2 > v1:
            win = n2
        else:
            win = "平"
        return f"{label} {v1} vs {v2} → {win}"

    def _compare_text(self, row1: dict, row2: dict) -> str:
        n1, n2 = row1.get("name_zh", ""), row2.get("name_zh", "")
        parsed1, parsed2 = self.db.first_stats(row1), self.db.first_stats(row2)
        if not parsed1 or not parsed2:
            return "缺少种族值数据，无法对比"
        t1 = [t for t in (row1.get("types") or "").split("/") if t]
        t2 = [t for t in (row2.get("types") or "").split("/") if t]
        m12 = self.db.matchup_multiplier(t1, t2)
        m21 = self.db.matchup_multiplier(t2, t1)
        lines = [
            f"【克制对比：{n1} vs {n2}】",
            f"属性：{row1.get('types', '')} vs {row2.get('types', '')}",
        ]
        for (label, v1), (_, v2) in zip(parsed1[0], parsed2[0]):
            lines.append(self._stat_compare_line(label, v1, v2, n1, n2))
        lines.append(self._stat_compare_line("总和", parsed1[1], parsed2[1], n1, n2))
        lines.append(f"互克：{n1} 打 {n2} ×{m12:g}｜{n2} 打 {n1} ×{m21:g}")
        ab1 = row1.get("abilities", "") or ""
        ab2 = row2.get("abilities", "") or ""
        lines.append(f"特性：{n1} {ab1}｜{n2} {ab2}")
        lines.append(self._compare_verdict(n1, n2, parsed1[1], parsed2[1], m12, m21))
        return "\n".join(lines)

    @staticmethod
    def _compare_verdict(
        n1: str, n2: str, total1: int, total2: int, m12: float, m21: float
    ) -> str:
        """多维度小结：面板强弱 + 属性克制关系（统一视角，不分攻防）。"""
        if m12 >= 2 and m21 <= 1:
            rel = f"{n1} 属性克制 {n2}"
        elif m21 >= 2 and m12 <= 1:
            rel = f"{n2} 属性克制 {n1}"
        elif m12 >= 2 and m21 >= 2:
            rel = "双方互为效果拔群（对攻）"
        else:
            rel = "属性互不克制"
        if total1 == total2:
            panel = "面板持平"
        else:
            win = n1 if total1 > total2 else n2
            panel = f"{win} 面板更高（{max(total1, total2)} vs {min(total1, total2)}）"
        return f"小结：{panel}，{rel}"

    @filter.command("克制")
    async def cmd_counter(
        self, event: AstrMessageEvent, name1: str = "", name2: str = ""
    ):
        """克制关系：克制 <宝可梦> = 推荐克制它的；克制 <A> <B> = 多维度对比"""
        if not name1:
            yield event.plain_result(
                "用法：克制 <宝可梦> = 推荐克制它的候选\n"
                "　　克制 <A> <B> = 两只多维度对比（面板/属性互克/特性）\n"
                "如：克制 妙蛙花 / 克制 皮卡丘 喷火龙"
            )
            return
        if not name2:
            result = self.db.find_counters(name1)
            if not result:
                yield event.plain_result(
                    f"没有查到「{name1}」的相关信息（查属性相性用 相性 <属性>）"
                )
                return
            yield event.plain_result(self._counters_text(result))
            return
        row1, row2 = self.db.pokemon(name1), self.db.pokemon(name2)
        if not row1 or not row2:
            miss = name1 if not row1 else name2
            yield event.plain_result(f"没有查到「{miss}」的相关信息")
            return
        yield event.plain_result(self._compare_text(row1, row2))

    @staticmethod
    def _counters_text(result: dict) -> str:
        """「克制 X」文本：双向验证的克制候选。"""
        lines = [
            f"【克制 {result['target']}（{result['target_types']}）】",
            "筛选条件：打它 ≥2× 且它打你 ≤1×，按打击倍率与种族值排序",
        ]
        for i, c in enumerate(result["counters"], 1):
            lines.append(
                f"{i}. {c['name']}（{c['types']}）"
                f"打它×{c['atk_mult']:g}｜它打你×{c['def_mult']:g}｜种族{c['total']}"
            )
        if len(lines) == 2:
            lines.append("没有同时满足条件的宝可梦，可放宽要求参考属性相性")
        return "\n".join(lines)

    # ---------- Phase 2：招式表 / 配招 ----------
    def _learnset_text(self, row: dict, level: int | None) -> str:
        learn = self.db.learnset(row)
        name = row.get("name_zh", "")
        lines = [f"【{name} 可学招式】" + (f"（等级≤{level}）" if level else "")]
        ups = learn["level_up"]
        if level:
            ups = [(n, lv) for n, lv in ups if lv is None or lv <= level]
        if ups:
            body = "、".join(f"{n}({lv})" if lv is not None else n for n, lv in ups)
            lines.append(f"升级（{len(ups)}）：{body}")
        if learn["machine"]:
            lines.append(
                f"招式学习器（{len(learn['machine'])}）："
                + "、".join(n for n, _ in learn["machine"])
            )
        if learn["egg"]:
            lines.append(
                f"遗传（{len(learn['egg'])}）：" + "、".join(n for n, _ in learn["egg"])
            )
        if not (ups or learn["machine"] or learn["egg"]):
            lines.append("未收录学习招式表")
        rec, from_learnset = self.db.recommended_moveset(row)
        tag = "" if from_learnset else "（演示配招：学习表未收录，按属性推荐）"
        lines.append(f"推荐配招：{'、'.join(rec)}{tag}")
        return "\n".join(lines)

    async def _learnset_view(self, event: AstrMessageEvent, name: str, level: str):
        """「宝可梦 <名> 招式表 …」子视图：可学招式与推荐配招。"""
        row = self.db.pokemon(name)
        if not row:
            yield event.plain_result(f"没有查到「{name}」的相关信息")
            return
        level_i = self._parse_clamped_int(level, 0, 0, 100) or None
        yield event.plain_result(self._learnset_text(row, level_i))

    # ---------- Phase 3：对战模拟 ----------
    def _battle_pokemon(self, row: dict, level: int) -> BattlePokemon:
        base = self._stats_base(row)
        stats = calc_stats(base or {}, level)
        moves, _from_learnset = self.db.recommended_moveset(row)
        battle_moves = [
            make_move(self.db.move_index()[n], self.db.STATUS_MOVES.get(n))
            for n in moves
            if n in self.db.move_index()
        ]
        return BattlePokemon(
            name=row.get("name_zh", ""),
            types=[t for t in (row.get("types") or "").split("/") if t],
            stats=stats,
            moves=battle_moves,
            level=level,
        )

    def _champ_ranks_line(self, rows: list[dict]) -> str:
        """ "冠军赛排名（M-7）：A 单#1/双#1｜B 单#7/双#5"，无数据返回空串。"""
        if not self.champions.exists:
            return ""
        season = self.champions.latest_season("single")
        if not season:
            return ""
        parts = []
        for row in rows:
            name = row.get("name_zh", "")
            if not name:
                continue
            ranks = {
                r["format"]: r["rank"]
                for r in self.champions.pokemon_ranks(name, season)
            }
            s = f"单#{ranks['single']}" if ranks.get("single") else "单—"
            d = f"双#{ranks['double']}" if ranks.get("double") else "双—"
            parts.append(f"{name} {s}/{d}")
        if not parts:
            return ""
        return f"冠军赛排名（{season}）：" + "｜".join(parts)

    def _battle_text(self, row1: dict, row2: dict, level: int) -> str:
        sim = BattleSimulator(self.db.type_chart())
        report = sim.simulate(
            self._battle_pokemon(row1, level), self._battle_pokemon(row2, level)
        )
        lines = [
            f"【{row1.get('name_zh', '')} vs {row2.get('name_zh', '')}】Lv.{level} 对战",
            "规则：简化规则（无特性/道具/天气/招式附加效果），配招为自动推荐",
        ]
        champ_line = self._champ_ranks_line([row1, row2])
        if champ_line:
            lines.append(champ_line)
        if report["winner"] == "平局":
            lines.append(f"结果：平局（{report['turns']} 回合）")
        else:
            lines.append(f"胜者：{report['winner']}（{report['turns']} 回合）")
        if report["key_moments"]:
            lines.append("关键事件：")
            lines += [f"{i}. {m}" for i, m in enumerate(report["key_moments"], 1)]
        finals = []
        for name, st in report["final"].items():
            if st["fainted"]:
                finals.append(f"{name} 倒下")
            else:
                tail = f"（{st['status']}）" if st["status"] else ""
                finals.append(f"{name} HP {st['hp']}/{st['max']}（{st['pct']}%）{tail}")
        lines.append("最终：" + "｜".join(finals))
        return "\n".join(lines)

    @filter.command("对战")
    async def cmd_battle(
        self,
        event: AstrMessageEvent,
        name1: str = "",
        name2: str = "",
        level: str = "50",
    ):
        """模拟两只宝可梦对战，如：对战 皮卡丘 喷火龙 50"""
        if not name1 or not name2:
            yield event.plain_result(
                "用法：对战 <名称1> <名称2> [等级]，如：对战 皮卡丘 喷火龙 50"
            )
            return
        row1, row2 = self.db.pokemon(name1), self.db.pokemon(name2)
        if not row1 or not row2:
            miss = name1 if not row1 else name2
            yield event.plain_result(f"没有查到「{miss}」的相关信息")
            return
        level_i = self._parse_clamped_int(level, 50, 1, 100)
        yield event.plain_result(self._battle_text(row1, row2, level_i))

    # ---------- Phase 4：队伍分析 / 种族值筛选 ----------
    def _champ_rank_short(self, name: str) -> str:
        """成员名次短标注「单#3/双#82」；查不到或无数据返回空串。"""
        if not self.champions.exists:
            return ""
        ranks = {r["format"]: r["rank"] for r in self.champions.pokemon_ranks(name)}
        if not ranks:
            return ""
        s = f"单#{ranks['single']}" if ranks.get("single") else "单—"
        d = f"双#{ranks['double']}" if ranks.get("double") else "双—"
        return f"{s}/{d}"

    def _team_text(self, result: dict) -> str:
        lines = [
            "【队伍分析】"
            + "、".join(m.get("name") or m.get("keyword") for m in result["members"])
        ]
        for m in result["members"]:
            if "error" in m:
                lines.append(f"? {m['keyword']}：{m['error']}")
                continue
            weak = "、".join(f"{a}×{v:g}" for a, v in m["weak"]) or "无"
            champ = self._champ_rank_short(m["name"])
            tag = f"［冠军赛 {champ}］" if champ else ""
            lines.append(f"{m['name']}{tag}（{m['types']}）：怕 {weak}")
        if result["common_weaknesses"]:
            lines.append("⚠ 共同弱点（队伍整体需防范）：")
            lines += [
                f"· {a}（{len(ns)} 只怕：{'、'.join(ns)}）"
                for a, ns in result["common_weaknesses"]
            ]
        else:
            lines.append("✅ 没有出现在过半成员身上的共同弱点")
        if result["team_resists"]:
            lines.append(
                "✅ 全队都不怕的进攻属性：" + "、".join(result["team_resists"])
            )
        if result["attack_coverage"]:
            lines.append("本队属性覆盖：" + "、".join(result["attack_coverage"]))
        return "\n".join(lines)

    @filter.command("队伍分析")
    async def cmd_team(self, event: AstrMessageEvent, keyword: str = ""):
        """分析队伍弱点配置，如：队伍分析 皮卡丘、喷火龙、卡比兽"""
        names = [x for x in keyword.replace("，", " ").replace("、", " ").split() if x]
        if len(names) < 2:
            yield event.plain_result(
                "用法：队伍分析 <名称1> <名称2> [最多6只]，如：队伍分析 皮卡丘 喷火龙 卡比兽"
            )
            return
        result = self.db.analyze_team(names[:6])
        yield event.plain_result(self._team_text(result))

    @filter.command("筛选")
    async def cmd_by_stats(self, event: AstrMessageEvent, keyword: str = ""):
        """按种族值筛选宝可梦，如：筛选 速度>100 特攻>=120"""
        conds = parse_stat_conditions(keyword)
        if not conds:
            yield event.plain_result(
                "用法：筛选 <条件>（可多条件空格分隔），如：筛选 速度>100 特攻>=120 总和<=500\n"
                "条件：HP/攻击/防御/特攻/特防/速度/总和 + >=/<=/>/</="
            )
            return
        hits = self.db.by_stats(conds)
        if not hits:
            yield event.plain_result("没有符合条件的宝可梦")
            return
        cond_text = " ".join(f"{l}{o}{v}" for l, o, v in conds)
        lines = [f"【筛选 {cond_text}】命中 {len(hits)} 只（按种族总和降序）："]
        lines += [
            f"{i}. {r['name']}（{r['types']}）总和{r['total']}"
            for i, r in enumerate(hits, 1)
        ]
        yield event.plain_result("\n".join(lines))

    # ---------- 许愿星基拉祈（WishMaker） ----------
    @filter.command("许愿星")
    async def cmd_wishmaker(
        self,
        event: AstrMessageEvent,
        nature: str = "",
        min_iv: str = "",
        tid: str = "",
        sid: str = "",
    ):
        """许愿星基拉祈目标筛选（红/蓝宝石），如：许愿星 固执 25 20040 0"""
        if not nature:
            yield event.plain_result(
                "用法：许愿星 <性格> [个体下限] [TID SID]\n"
                "性格：\n" + WISHMAKER_NATURES + "\n"
                "如：许愿星 固执 25 = 六项个体≥25 的固执；末尾附 TID SID 可标注异色"
            )
            return
        try:
            iv_floor = max(0, min(31, int(min_iv))) if min_iv else 0
            tsv = (int(tid) ^ int(sid)) if tid and sid else None
        except ValueError:
            yield event.plain_result("个体下限 / TID / SID 需要是数字")
            return
        count, rows = self.db.wishmaker_query(nature=nature, min_iv=iv_floor, tsv=tsv)
        if not count:
            yield event.plain_result(
                f"没有符合的目标（性格请从：{WISHMAKER_NATURES} 中选择）"
            )
            return
        # 默认图片卡片；wishmaker_style=markdown 时走 markdown（QQ 官方 / 微信个人号等支持渲染的平台）
        if self.config.get("wishmaker_style", "image") == "markdown":
            result = event.plain_result(
                build_wishmaker_markdown(nature, iv_floor, count, rows, tsv)
            )
            result.use_markdown(True)  # 仅支持的平台消费该标记，其他平台忽略
            yield result
            return
        try:
            png = self.renderer.render_wishmaker_card(
                nature, iv_floor, count, rows, tsv
            )
            yield event.chain_result([Comp.Image.fromBytes(png)])
        except Exception as e:
            logger.warning(f"许愿星卡片渲染失败: {e}")
            yield event.plain_result("许愿星卡片渲染失败，请稍后再试")

    @staticmethod
    def _format_wishmaker(
        nature: str, iv_floor: int, count: int, rows: list[dict]
    ) -> str:
        lines = [
            f"【许愿星基拉祈】性格 {nature or '任意'} · 个体≥{iv_floor}（命中 {count} 个种子）"
        ]
        for i, r in enumerate(rows, 1):
            ivs = "/".join(r[k] for k in ("hp", "atk", "def", "spa", "spd", "spe"))
            mark = " ✨异色" if r.get("shiny") else ""
            lines.append(
                f"{i}. 种子 {r['seed']} | PID {r['pid']} | 个体 {ivs}（总和{r['total']}）"
                f"| 觉醒力量 {r['hp_type']}{r['hp_power']}{mark}"
            )
        lines.append(
            "提示：种子 = 红/蓝宝石接收许愿星基拉祈时的 WishMaker 种子（16位十六进制）"
        )
        return "\n".join(lines)

    # ---------- 冠军赛季数据（champions.db，只读查询） ----------
    # 一个入口搞定全部查询，参数按 token 自动分类：
    #   宝冠军                         → 数据状态 + 用法
    #   宝冠军 单打 / 宝冠军 双打       → Top10（可再加赛季：M-6 / 6 / 上一轮）
    #   宝冠军 3 / 宝冠军 双打 3        → 第3名是谁（附配置摘要）
    #   宝冠军 烈咬陆鲨                 → 名次（单+双）+ 配置使用率
    #   宝冠军 烈咬陆鲨 趋势            → 跨赛季名次变化
    #   宝冠军 烈咬陆鲨 M-6            → 指定赛季（数字与名称同现时视为赛季）
    #   宝冠军 生命宝珠                 → 道具反查（谁在带它）
    #   宝冠军 数据                     → 本地库状态
    CHAMP_FORMATS: ClassVar[dict[str, str]] = {
        "单打": "single",
        "双打": "double",
        "single": "single",
        "double": "double",
    }
    CHAMP_LATEST_WORDS: ClassVar[set[str]] = {
        "最新",
        "这赛季",
        "本赛季",
        "latest",
        "this",
    }
    CHAMP_PREV_WORDS: ClassVar[set[str]] = {
        "上一轮",
        "上一赛季",
        "上赛季",
        "上个赛季",
        "上季",
        "prev",
        "previous",
    }
    CHAMP_SEASON_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:m-?|第)?(\d{1,3})(?:赛季?)?", re.IGNORECASE
    )

    @classmethod
    def _parse_champ_args(cls, text: str) -> dict:
        """把指令参数按 token 分类。

        数字语义消歧：与名称词同现时视为赛季编号（宝冠军 烈咬陆鲨 6 → M-6），
        否则首个数字是名次（宝冠军 3 → 第3名）、第二个数字是赛季。
        返回 {fmt, rank, season, view, names}；season 取值 None(最新)/"prev"/"M-N"。
        """
        fmt = rank = view = None
        season: str | None = None
        names: list[str] = []
        digits: list[int] = []
        for raw in (text or "").split():
            t = raw.strip()
            low = t.lower()
            if low in cls.CHAMP_FORMATS and fmt is None:
                fmt = cls.CHAMP_FORMATS[low]
            elif low in {"趋势", "trend"}:
                view = "trend"
            elif low in {"帮助", "help", "用法"}:
                view = "help"
            elif low in {"数据", "状态", "status"}:
                view = "status"
            elif low in cls.CHAMP_LATEST_WORDS:
                season = None
            elif low in cls.CHAMP_PREV_WORDS:
                season = "prev"
            elif low.startswith(("m", "第")) or low.endswith("赛季"):
                m = cls.CHAMP_SEASON_RE.fullmatch(low)
                if m:
                    season = f"M-{m.group(1)}"
                else:
                    names.append(t)
            elif t.lstrip("#").isdigit() and len(t.lstrip("#")) <= 3:
                digits.append(int(t.lstrip("#")))
            else:
                names.append(t)
        if names:  # 有名称词：数字是赛季
            if digits:
                season = f"M-{digits.pop(0)}"
        elif digits:  # 无名称词：首个数字是名次，其余是赛季
            rank = digits.pop(0)
            if digits:
                season = f"M-{digits.pop(0)}"
        return {
            "fmt": fmt,
            "rank": rank,
            "season": season,
            "view": view,
            "names": names,
        }

    def _champ_season_real(self, season: str | None, fmt: str) -> str | None:
        """把 None/"prev"/"M-N"/"6" 解析为库里真实赛季；prev 只有一个赛季时 None。"""
        if season == "prev":
            seasons = self.champions.seasons(fmt)
            return seasons[1] if len(seasons) > 1 else None
        s = (season or "").strip()
        if s.isdigit():
            s = f"M-{s}"
        return self.champions.resolve_season(s or None, fmt)

    @staticmethod
    def _champ_format_arg(fmt: str) -> str:
        """LLM 工具的 fmt 参数（single/double）→ 内部格式。"""
        return "double" if (fmt or "").strip().lower() == "double" else "single"

    def _champ_updated_line(
        self, season: str | None, source: str = "pokechamdb"
    ) -> str:
        """ "本地数据更新：…（站点更新：…）" 行，跨格式查询复用。"""
        st = self.champions.status(source=source, season=season)
        if not st:
            return ""
        local = ""
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(st["run_at"])
            local = dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            pass
        site = st.get("source_updated_at") or "站点时间未知"
        return f"本地数据更新：{local or st.get('run_at', '?')}（站点更新：{site}）"

    def _champ_header(
        self, fmt: str, season: str | None, source: str = "pokechamdb"
    ) -> str:
        """查询结果头部：赛季/格式 + 数据来源 + 本地数据更新日期。"""
        season = self.champions.resolve_season(season, fmt, source)
        lines = [
            (
                f"【宝可梦 Champions {season or '?'} {fmt_label(fmt)}】"
                f"来源：{source_label(source)}"
            )
        ]
        updated = self._champ_updated_line(season, source)
        if updated:
            lines.append(updated)
        return "\n".join(lines)

    @staticmethod
    def _champ_ev_text(value: str) -> str:
        """ "2-32-0-0-0-32" → "HP2/攻32/防0/特攻0/特防0/速32"。"""
        keys = ("HP", "攻", "防", "特攻", "特防", "速")
        parts = value.split("-")
        if len(parts) != 6:
            return value
        return "/".join(f"{k}{v}" for k, v in zip(keys, parts))

    def _champ_no_data(self) -> str:
        return (
            "本地还没有冠军赛数据。\n"
            "请在插件目录运行独立抓取脚本：uv run python champions_fetch.py"
            "（详见 README「冠军赛季数据」一节）"
        )

    def _champ_option_line(
        self, pokemon_id: str, fmt: str, season: str | None, otype: str, top_n: int
    ) -> str | None:
        """某类配置的一行摘要（名称 使用率%），无数据返回 None。"""
        opts = self.champions.pokemon_options(
            pokemon_id, fmt, season, option_type=otype, top_n=top_n
        )
        if not opts:
            return None
        parts = []
        for o in opts:
            if otype == "teammate":
                parts.append(f"{o['option_value']}(#{o['rank']})")
            elif otype == "ev_spread":
                pct = f" {o['usage_pct']:g}%" if o.get("usage_pct") is not None else ""
                parts.append(f"{self._champ_ev_text(o['option_value'])}{pct}")
            else:
                pct = f" {o['usage_pct']:g}%" if o.get("usage_pct") is not None else ""
                parts.append(f"{o['option_value']}{pct}")
        return f"{OPTION_LABELS.get(otype, otype)}：{'、'.join(parts)}"

    def _champ_ranking_text(
        self, fmt: str, season: str | None, limit: int | None = None
    ) -> str:
        """排名 TopN 文本。"""
        limit = limit or max(1, min(50, int(self.config.get("champions_top_n", 10))))
        rows = self.champions.top_usage(fmt, season, limit=limit)
        if not rows:
            season_real = self.champions.resolve_season(season, fmt)
            return (
                f"本地没有 {fmt_label(fmt)} {season_real or ''} 的排名数据，"
                f"可用赛季：{'、'.join(self.champions.seasons(fmt)) or '无'}"
            )
        lines = [self._champ_header(fmt, season), ""]
        for r in rows:
            pct = f"{r['usage_pct']:g}%" if r.get("usage_pct") is not None else ""
            lines.append(
                f"#{r['rank']:<3} {r['name_zh'] or r['pokemon_id']} {pct}".rstrip()
            )
        if rows[0].get("usage_pct") is None:
            lines.append("（该来源仅提供名次，无使用率百分比）")
        lines.append("提示：宝冠军 3 = 第3名是谁｜宝冠军 <宝可梦> = 名次+配置")
        return "\n".join(lines)

    def _champ_rank_row_text(self, fmt: str, row: dict) -> str:
        """「第 N 名是谁」：名次 + 宝可梦 + 招式/道具摘要 + 反查提示。"""
        season = row.get("season") or self.champions.resolve_season(None, fmt)
        lines = [
            (
                f"【宝可梦 Champions {season or '?'} {fmt_label(fmt)} "
                f"第 {row['rank']} 名】来源：{source_label('pokechamdb')}"
            ),
            self._champ_updated_line(season),
            (
                f"#{row['rank']} {row['name_zh'] or row['pokemon_id']}"
                f"（共 {row.get('total', '?')} 只）"
            ),
        ]
        for otype, top_n in (("move", 3), ("item", 2)):
            line = self._champ_option_line(row["pokemon_id"], fmt, season, otype, top_n)
            if line:
                lines.append(line)
        lines.append(
            f"提示：宝冠军 {row['name_zh'] or row['pokemon_id']} = 完整配置与单双打名次"
        )
        return "\n".join(line for line in lines if line)

    def _champ_pokemon_card_text(
        self, prow: dict, fmt_req: str | None, season_real: str | None
    ) -> str:
        """宝可梦卡片：名次（未指定格式则单双都给）+ 配置使用率 + 反查提示。"""
        pid = prow["pokemon_id"]
        name = prow.get("name_zh") or pid
        fmt_show = fmt_req or "single"
        fmts = [fmt_req] if fmt_req else ["single", "double"]
        lines = [
            (
                f"【{name} 冠军赛配置】赛季 {season_real or '?'}｜"
                f"来源：{source_label('pokechamdb')}"
            )
        ]
        updated = self._champ_updated_line(season_real)
        if updated:
            lines.append(updated)
        for fmt in fmts:
            row = (
                prow
                if fmt == fmt_show
                else self.champions.resolve_pokemon(pid, fmt, season_real)
            )
            if not row:
                lines.append(f"{fmt_label(fmt)}：榜单未收录")
                continue
            pct = (
                f"，使用率 {row['usage_pct']:g}%"
                if row.get("usage_pct") is not None
                else ""
            )
            lines.append(
                f"{fmt_label(fmt)}：#{row['rank']}（共 "
                f"{self.champions.count(fmt, row['season'])} 只）{pct}"
            )
            for otype, top_n in (
                ("move", 4),
                ("item", 3),
                ("ability", 2),
                ("nature", 2),
                ("ev_spread", 2),
                ("teammate", 3),
                ("tera", 2),
            ):
                line = self._champ_option_line(pid, fmt, row["season"], otype, top_n)
                if line:
                    lines.append(("　" if len(fmts) > 1 else "") + line)
        other = next(f for f in ("single", "double") if f != fmt_show)
        lines.append(
            f"提示：宝冠军 {name} 趋势 = 跨赛季｜宝冠军 {name} "
            f"{fmt_label(other)} = 另一格式"
        )
        return "\n".join(line for line in lines if line)

    def _champ_trend_text(self, prow: dict, fmt_req: str | None) -> str:
        """跨赛季名次（未指定格式则单双并列）。"""
        pid = prow["pokemon_id"]
        name = prow.get("name_zh") or pid
        fmts = [fmt_req] if fmt_req else ["single", "double"]
        lines = [f"【{name} 跨赛季名次】来源：{source_label('pokechamdb')}"]
        any_row = False
        for fmt in fmts:
            rows = self.champions.trend(pid, fmt)
            if not rows:
                continue
            any_row = True
            body = "｜".join(f"{r['season']} #{r['rank']}" for r in rows)
            lines.append(f"{fmt_label(fmt)}：{body}")
        if not any_row:
            lines.append("本地只有一个赛季的数据，暂无趋势（多抓几个赛季后再试）")
        else:
            lines.append("提示：宝冠军 <宝可梦> = 当前赛季完整配置")
        return "\n".join(lines)

    def _champ_item_text(
        self, item: str, rows: list[dict], fmt: str, season_real: str | None
    ) -> str:
        """道具反查：谁在带它。"""
        lines = [
            (
                f"【{fmt_label(fmt)} {season_real or '?'} 携带「{rows[0]['item']}」】"
                f"来源：{source_label('pokechamdb')}"
            ),
            self._champ_updated_line(season_real),
        ]
        for r in rows[:10]:
            pct = (
                f" {r['item_usage_pct']:g}%"
                if r.get("item_usage_pct") is not None
                else ""
            )
            lines.append(f"#{r['rank']} {r['name_zh'] or r['pokemon_id']}{pct}")
        lines.append("提示：宝冠军 <宝可梦名> = 它的完整配置")
        return "\n".join(line for line in lines if line)

    def _champ_status_text(self) -> str:
        """本地库状态。"""
        info = self.champions.database_summary()
        if not info.get("exists"):
            return self._champ_no_data()
        lines = ["【冠军赛本地数据】"]
        for fmt in ("single", "double"):
            seasons = self.champions.seasons(fmt)
            if not seasons:
                continue
            latest = seasons[0]
            n = len(self.champions.top_usage(fmt, latest, limit=1000))
            st = self.champions.status(fmt=fmt, season=latest)
            when = ""
            if st:
                try:
                    from datetime import datetime

                    when = (
                        datetime.fromisoformat(st["run_at"])
                        .astimezone()
                        .strftime("%Y-%m-%d %H:%M")
                    )
                except (ValueError, TypeError):
                    pass
            src = f"，来源 {source_label(st['source'])}" if st else ""
            lines.append(
                f"{fmt_label(fmt)} {'、'.join(seasons[:3])}：{n} 只"
                + (f"，本地更新 {when}{src}" if when else "")
            )
        if len(lines) == 1:
            lines.append("数据库为空，请先运行抓取脚本")
        lines.append(f"库文件：{info['path']}")
        return "\n".join(lines)

    def _champ_help_text(self) -> str:
        st_line = ""
        latest = self.champions.latest_season("single")
        if latest:
            st_line = f"当前本地赛季：{latest}（宝冠军 数据 看详情）\n"
        return (
            "【冠军赛查询】\n" + st_line + "宝冠军 单打 / 宝冠军 双打 — Top10\n"
            "宝冠军 3 / 宝冠军 双打 3 — 第3名是谁\n"
            "宝冠军 <宝可梦> — 名次(单双) + 配置使用率\n"
            "宝冠军 <宝可梦> 趋势 — 跨赛季名次\n"
            "宝冠军 <道具名> — 谁在带这个道具\n"
            "赛季：默认最新，可加 M-6 / 6 / 上一轮\n"
            "对战 <A> <B> 结果里也会带双方名次"
        )

    @filter.command("宝冠军")
    async def cmd_champion(self, event: AstrMessageEvent, keyword: str = ""):
        """冠军赛一站式查询：宝冠军 帮助 看用法"""
        if not self.champions.exists:
            yield event.plain_result(self._champ_no_data())
            return
        args = self._parse_champ_args(keyword)
        fmt, rank, season, view, names = (
            args["fmt"],
            args["rank"],
            args["season"],
            args["view"],
            args["names"],
        )
        if view == "status":
            yield event.plain_result(self._champ_status_text())
            return
        if view == "help" or (
            not names
            and rank is None
            and fmt is None
            and view is None
            and season is None
        ):
            yield event.plain_result(self._champ_help_text())
            return
        fmt_eff = fmt or self.config.get("champions_default_format", "single")
        season_real = self._champ_season_real(season, fmt_eff)
        if season == "prev" and season_real is None:
            latest = self.champions.latest_season(fmt_eff)
            yield event.plain_result(f"本地只有 {latest or '?'} 一个赛季，无法查上一轮")
            return

        # ---- 无名称：排名 / 按名次 ----
        if not names:
            if rank is not None:
                row = self.champions.by_rank(rank, fmt_eff, season_real)
                if not row:
                    yield event.plain_result(
                        f"{fmt_label(fmt_eff)} {season_real or ''} 榜单没有第 {rank} 名"
                    )
                else:
                    yield event.plain_result(self._champ_rank_row_text(fmt_eff, row))
            else:
                yield event.plain_result(self._champ_ranking_text(fmt_eff, season_real))
            return

        # ---- 有名称：趋势 / 宝可梦卡片 / 道具反查 ----
        name = names[0]
        if view == "trend":
            prow = self.champions.resolve_pokemon(name, fmt_eff, season_real)
            if not prow:
                yield event.plain_result(
                    f"{fmt_label(fmt_eff)} {season_real or ''} 榜单中没有「{name}」"
                )
            else:
                yield event.plain_result(self._champ_trend_text(prow, fmt))
            return
        prow = self.champions.resolve_pokemon(name, fmt_eff, season_real)
        if prow:
            yield event.plain_result(
                self._champ_pokemon_card_text(prow, fmt, season_real)
            )
            return
        rows = self.champions.find_pokemon_by_item(name, fmt_eff, season_real)
        if rows:
            yield event.plain_result(
                self._champ_item_text(name, rows, fmt_eff, season_real)
            )
            return
        yield event.plain_result(
            f"「{name}」既不在 {fmt_label(fmt_eff)} 榜单也没有道具匹配；"
            f"发「宝冠军 帮助」看用法"
        )

    def _help_text(self) -> str:
        return (
            "🎮 宝可梦图鉴插件\n"
            "宝可梦 <编号/名称> — 图鉴卡片（头部带冠军赛名次）\n"
            "宝可梦 <名称> 招式表 [等级] — 可学招式与推荐配招\n"
            "宝可梦 <名称> 能力值 [等级 个体 性格] — 等级能力值计算\n"
            "特性 <名称> / 招式 <名称> / 道具 <名称> — 数据查询\n"
            "相性 <属性1> [属性2] / 相性 打 <属性> — 属性相性防守/进攻\n"
            "克制 <宝可梦> / 克制 <A> <B> — 克制推荐与多维度对比\n"
            "对战 <名称1> <名称2> [等级] — 模拟对战（含双方冠军赛名次）\n"
            "队伍分析 <名称...> — 队伍弱点配置分析\n"
            "筛选 <条件> — 按种族值筛选，如 速度>100 特攻>=120\n"
            "许愿星 <性格> [个体下限] [TID SID] — 许愿星基拉祈目标筛选\n"
            "宝冠军 <关键词> — 冠军赛一站式查询，宝冠军 帮助 看用法\n"
            "群聊 @我 直接提问时也会自动调用图鉴工具（需模型支持函数调用）"
        )

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
        others = sorted(
            {r["form_name"] for r in rows if r.get("form_name") and r is not row}
        )
        text = self.db.format_row(row, fields=POKEMON_FIELDS)
        if others:
            text += (
                f"\n其他形态（可用 query_pokemon 按形态名查询）：{'、'.join(others)}"
            )
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
                if table == "pokedex":
                    if r.get("dex") == "national":
                        out.append(
                            f"[地区图鉴] 全国 {r.get('id_raw', '')}："
                            f"{r.get('name', '')}（{r.get('types', '')}）"
                        )
                    else:
                        out.append(
                            f"[{r.get('dex', '')}图鉴] {r.get('id_raw', '')}"
                            f"（全国 {r.get('national_id', '')}）：{r.get('name', '')}"
                        )
                    continue
                parts = [
                    f"{r.get(c, '')}"
                    for c in ("id_raw", "national_id", "name_zh", "name_en")
                    if r.get(c)
                ]
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
        return (
            f"特性 {r.get('name_zh', '')}（{r.get('name_en', '')}）\n"
            f"介绍：{r.get('introduction', '')}\n效果：{r.get('effect', '')[:600]}\n"
            f"持有宝可梦：{(r.get('holders', '') or '')[:400]}"
        )

    @filter.llm_tool(name="query_move")
    async def tool_query_move(self, event: AstrMessageEvent, keyword: str):
        """查询招式（move）的属性、威力、命中、PP 与说明。返回中文数据。
        Args:
            keyword(str): 招式名称（中文或英文）
        """
        rows = self.db.query("move", keyword)
        if rows:
            r = rows[0]
            fields = (
                "id",
                "name_zh",
                "name_en",
                "type",
                "category",
                "power",
                "accuracy",
                "pp",
                "generation",
                "description",
            )
            return self.db.format_row(r, fields=fields)
        detail = self.db.query("moves", keyword)
        if not detail:
            return f"没有查到招式「{keyword}」的相关信息"
        r = detail[0]
        return self.db.format_row(
            r,
            fields=(
                "name_zh",
                "name_en",
                "type",
                "category",
                "power",
                "accuracy",
                "pp",
                "range",
                "description",
                "additional_effect",
            ),
        )

    @filter.llm_tool(name="query_item")
    async def tool_query_item(self, event: AstrMessageEvent, keyword: str):
        """查询道具（携带物品）的类别与说明。返回中文数据，直接引用即可。
        Args:
            keyword(str): 道具名称（中文或英文，支持片段）
        """
        rows = self.db.query("item", keyword)
        if not rows:
            return f"没有查到道具「{keyword}」的相关信息"
        r = rows[0]
        return (
            f"道具 {r.get('name_zh', '')}（{r.get('name_en', '')}）\n"
            f"类别：{r.get('category_path', '')}\n说明：{r.get('description', '')}"
        )

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
                out.append(
                    f"全国图鉴 {r['id_raw']}：{r.get('name', '')}（{r.get('types', '')}，"
                    f"第 {r.get('generation', '?')} 世代）"
                )
            else:
                out.append(
                    f"{r.get('dex', '')}图鉴 {r.get('id_raw', '')}"
                    f"（全国 {r.get('national_id', '')}）：{r.get('name', '')}"
                )
        return "\n".join(out)

    @filter.llm_tool(name="query_type_matchups")
    async def tool_query_type_matchups(
        self, event: AstrMessageEvent, type1: str, type2: str = ""
    ):
        """查询属性相性倍率（本地属性表）。适用于询问某属性（或双属性组合）怕什么、抗什么、免疫什么。
        Args:
            type1(str): 第一个属性（中文，如 火）
            type2(str, optional): 第二个属性（可选，如 飞行）
        """
        result = self.db.matchups([t for t in (type1, type2) if t])
        if result is None:
            return f"没有查到属性「{type1}{'/' + type2 if type2 else ''}」"
        combo = "/".join(x for x in (type1, type2) if x)
        text = (
            f"【{combo}】\n"
            f"弱点：{'、'.join(result['weak']) or '无'}\n"
            f"抗性：{'、'.join(result['resist']) or '无'}\n"
            f"免疫：{'、'.join(result['immune']) or '无'}"
        )
        corr = self._ability_correction_text(result)
        if corr:
            text += f"\n{corr}（若防守方宝可梦持有对应特性则生效）"
        return text

    @filter.llm_tool(name="query_wishmaker_jirachi")
    async def tool_query_wishmaker(
        self,
        event: AstrMessageEvent,
        nature: str = "",
        min_iv: int = 0,
        hp_type: str = "",
        tid: int = -1,
        sid: int = -1,
        shiny_only: bool = False,
    ):
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
        count, rows = self.db.wishmaker_query(
            nature=nature or None,
            min_iv=iv_floor,
            hp_type=hp_type or None,
            tsv=tsv,
            shiny_only=shiny_only,
            limit=10,
        )
        if not count:
            return "没有符合条件的目标，请放宽个体值下限或核对性格/属性名称"
        return self._format_wishmaker(nature, iv_floor, count, rows)

    # ---------- 冠军赛季 LLM 工具 ----------
    @filter.llm_tool(name="query_champions_ranking")
    async def tool_champ_ranking(
        self,
        event: AstrMessageEvent,
        fmt: str = "single",
        season: str = "",
        limit: int = 10,
    ):
        """查询宝可梦 Champions 冠军赛使用率排名（本地离线数据）。
        返回前 N 名及数据来源与本地更新日期。适用于"当前单打使用率排名""双打什么强"。
        Args:
            fmt(str): 格式，single=单打 / double=双打，默认 single
            season(str): 赛季，M-7、6 或 "上一轮" 均可，留空用本地最新赛季
            limit(int): 返回数量（1-20），默认 10
        """
        if not self.champions.exists:
            return self._champ_no_data()
        f = self._champ_format_arg(fmt)
        rows = self.champions.top_usage(
            f, self._champ_season_real(season, f), limit=max(1, min(20, int(limit)))
        )
        if not rows:
            return (
                f"本地没有 {fmt_label(f)} 的排名数据"
                f"（可用赛季：{'、'.join(self.champions.seasons(f)) or '无'}）"
            )
        lines = [self._champ_header(f, self._champ_season_real(season, f))]
        for r in rows:
            pct = f"{r['usage_pct']:g}%" if r.get("usage_pct") is not None else ""
            lines.append(
                f"#{r['rank']} {r['name_zh'] or r['pokemon_id']} {pct}".rstrip()
            )
        return "\n".join(lines)

    @filter.llm_tool(name="query_rank_pokemon")
    async def tool_rank_pokemon(
        self,
        event: AstrMessageEvent,
        rank: int,
        fmt: str = "single",
        season: str = "",
    ):
        """按名次反查冠军赛宝可梦：返回该名次的宝可梦与配置摘要（本地离线数据）。
        适用于"单打第3名是谁""双打第5名用什么"；查某宝可梦的名次用 query_pokemon_rank。
        Args:
            rank(int): 名次（如 3 表示第 3 名）
            fmt(str): single=单打 / double=双打，默认 single
            season(str): 赛季，M-7、6 或 "上一轮" 均可，留空用本地最新赛季
        """
        if not self.champions.exists:
            return self._champ_no_data()
        f = self._champ_format_arg(fmt)
        season_real = self._champ_season_real(season, f)
        row = self.champions.by_rank(int(rank), f, season_real)
        if not row:
            return f"{fmt_label(f)} {season_real or ''} 榜单没有第 {rank} 名"
        return self._champ_rank_row_text(f, row)

    @filter.llm_tool(name="query_pokemon_rank")
    async def tool_champ_lookup(
        self, event: AstrMessageEvent, keyword: str, season: str = ""
    ):
        """查询某只宝可梦在冠军赛单打和双打的排名（本地离线数据）。
        适用于"烈咬陆鲨排第几""谜拟Ｑ双打排名多少"；按名次反查宝可梦用 query_rank_pokemon。
        Args:
            keyword(str): 宝可梦名称（如 烈咬陆鲨）
            season(str): 赛季，M-7、6 或 "上一轮" 均可，留空用本地最新赛季
        """
        if not self.champions.exists:
            return self._champ_no_data()
        kw = (keyword or "").strip().lstrip("#")
        rows = self.champions.pokemon_ranks(
            kw, self._champ_season_real(season, "single")
        )
        if not rows:
            season_real = self._champ_season_real(season, "single")
            return f"{season_real or ''} 冠军赛榜单中没有「{keyword}」"
        lines = [
            (
                f"【{rows[0]['name_zh'] or rows[0]['pokemon_id']} 冠军赛排名】"
                f"赛季 {rows[0]['season']}｜来源：{source_label('pokechamdb')}"
            ),
            self._champ_updated_line(rows[0]["season"]),
        ]
        for r in rows:
            pct = (
                f"，使用率 {r['usage_pct']:g}%"
                if r.get("usage_pct") is not None
                else ""
            )
            lines.append(
                f"{fmt_label(r['format'])}：#{r['rank']}（共 {r['total']} 只）{pct}"
            )
        missing = {"single", "double"} - {r["format"] for r in rows}
        if missing:
            lines.append(
                f"{'、'.join(fmt_label(m) for m in sorted(missing))}榜单中未收录该宝可梦"
            )
        return "\n".join(line for line in lines if line)

    @filter.llm_tool(name="query_champions_build")
    async def tool_champ_build(
        self,
        event: AstrMessageEvent,
        keyword: str,
        fmt: str = "single",
        season: str = "",
    ):
        """查询宝可梦在冠军赛的主流配置使用率：招式、道具、特性、性格、能力点、队友
        （本地离线数据，仅单打有配置明细）。适用于"烈咬陆鲨冠军赛怎么配""耿鬼带什么招"。
        Args:
            keyword(str): 宝可梦名称（中文名优先，如 烈咬陆鲨）
            fmt(str): 格式，single=单打 / double=双打，默认 single
            season(str): 赛季，M-7、6 或 "上一轮" 均可，留空用本地最新赛季
        """
        if not self.champions.exists:
            return self._champ_no_data()
        f = self._champ_format_arg(fmt)
        row = self.champions.resolve_pokemon(
            keyword, f, self._champ_season_real(season, f)
        )
        if not row:
            return (
                f"{fmt_label(f)} {self._champ_season_real(season, f) or ''} "
                f"榜单中没有「{keyword}」"
            )
        lines = [self._champ_header(f, self._champ_season_real(season, f))]
        rank_pct = f"（#{row['rank']}" + (
            f"，使用率 {row['usage_pct']:g}%）"
            if row.get("usage_pct") is not None
            else "）"
        )
        lines.append(f"{row['name_zh'] or row['pokemon_id']} {rank_pct}")
        for otype in self.champions.option_types(
            row["pokemon_id"], f, self._champ_season_real(season, f)
        ):
            opts = self.champions.pokemon_options(
                row["pokemon_id"],
                f,
                self._champ_season_real(season, f),
                option_type=otype,
                top_n=3 if otype == "teammate" else 5,
            )
            label = OPTION_LABELS.get(otype, otype)
            parts = []
            for o in opts:
                pct = (
                    f"{o['usage_pct']:g}%"
                    if o.get("usage_pct") is not None
                    else (f"(#{o['rank']})" if o.get("rank") is not None else "")
                )
                if otype == "ev_spread":
                    parts.append(
                        f"{self._champ_ev_text(o['option_value'])} {pct}".strip()
                    )
                else:
                    parts.append(f"{o['option_value']}{pct}")
            if parts:
                lines.append(f"{label}：{'、'.join(parts)}")
        if len(lines) <= 2:
            lines.append("（该宝可梦暂无配置明细；双打格式站点不提供明细）")
        return "\n".join(lines)

    @filter.llm_tool(name="find_champions_by_item")
    async def tool_champ_item(
        self,
        event: AstrMessageEvent,
        item_name: str,
        fmt: str = "single",
        season: str = "",
    ):
        """反查冠军赛中携带某道具的宝可梦及携带率（本地离线数据）。
        适用于"谁带讲究围巾""生命宝珠使用者排名"。
        Args:
            item_name(str): 道具名称（支持片段，如 围巾、宝珠）
            fmt(str): 格式，single=单打 / double=双打，默认 single
            season(str): 赛季，M-7、6 或 "上一轮" 均可，留空用本地最新赛季
        """
        if not self.champions.exists:
            return self._champ_no_data()
        f = self._champ_format_arg(fmt)
        rows = self.champions.find_pokemon_by_item(
            item_name, f, self._champ_season_real(season, f)
        )
        if not rows:
            return f"{fmt_label(f)} 没有宝可梦携带含「{item_name}」的道具"
        lines = [
            self._champ_header(f, self._champ_season_real(season, f)),
            f"携带「{rows[0]['item']}」的宝可梦：",
        ]
        for r in rows:
            pct = (
                f"{r['item_usage_pct']:g}%"
                if r.get("item_usage_pct") is not None
                else ""
            )
            lines.append(
                f"#{r['rank']} {r['name_zh'] or r['pokemon_id']}（道具 {pct}）".rstrip()
            )
        return "\n".join(lines)

    @filter.llm_tool(name="query_champions_trend")
    async def tool_champ_trend(
        self,
        event: AstrMessageEvent,
        keyword: str,
        fmt: str = "single",
    ):
        """查询宝可梦在冠军赛的跨赛季排名变化（本地离线数据）。
        适用于"烈咬陆鲨这几赛季排名怎么样""某宝可梦热度变化"。
        Args:
            keyword(str): 宝可梦名称（中文名优先）
            fmt(str): 格式，single=单打 / double=双打，默认 single
        """
        if not self.champions.exists:
            return self._champ_no_data()
        f = self._champ_format_arg(fmt)
        row = self.champions.resolve_pokemon(keyword, f)
        if not row:
            return f"冠军赛数据中没有「{keyword}」"
        rows = self.champions.trend(row["pokemon_id"], f)
        if not rows:
            return "本地只有一个赛季的数据，暂无趋势可查（运行抓取脚本时多抓几个赛季）"
        lines = [f"【{row['name_zh'] or row['pokemon_id']} 跨赛季排名】{fmt_label(f)}"]
        for r in rows:
            pct = (
                f"，使用率 {r['usage_pct']:g}%"
                if r.get("usage_pct") is not None
                else ""
            )
            lines.append(f"{r['season']}：#{r['rank']}{pct}")
        return "\n".join(lines)

    @filter.llm_tool(name="champions_data_status")
    async def tool_champ_status(self, event: AstrMessageEvent):
        """查看本地冠军赛数据库状态：覆盖的赛季/格式、条目数与最后更新时间。
        用户询问冠军赛数据是否最新、数据来自哪里时使用。
        """
        info = self.champions.database_summary()
        if not info.get("exists"):
            return self._champ_no_data()
        lines = ["【冠军赛本地数据】"]
        for fmt in ("single", "double"):
            seasons = self.champions.seasons(fmt)
            if not seasons:
                continue
            latest = seasons[0]
            n = len(self.champions.top_usage(fmt, latest, limit=1000))
            st = self.champions.status(fmt=fmt, season=latest)
            if st:
                lines.append(
                    f"{fmt_label(fmt)} {latest}：{n} 只，"
                    f"来源 {source_label(st['source'])}，"
                    f"站点更新 {st.get('source_updated_at') or '?'}"
                )
            else:
                lines.append(f"{fmt_label(fmt)} {latest}：{n} 只")
        if len(lines) == 1:
            lines.append("数据库为空")
        return "\n".join(lines)

    @filter.llm_tool(name="find_pokemon_by_type")
    async def tool_find_by_type(self, event: AstrMessageEvent, type_name: str):
        """列出某属性的宝可梦（默认形态）。适用于"电属性宝可梦有哪些"这类问题。
        Args:
            type_name(str): 属性中文名（如 电、水）
        """
        hits = self.db.by_type(type_name)
        if not hits:
            return f"没有查到「{type_name}」属性的宝可梦"
        return f"{type_name}属性宝可梦（共 {len(hits)} 只，列出前 40）：\n" + "、".join(
            f"{r['name_zh']}({r['id_raw']})" for r in hits
        )

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
        abilities = "、".join(
            f"{a['name_zh'] or a['name_en']}({'隐藏特性，' if a['hidden'] else ''}{a['effect']})"
            for a in data["abilities"]
        )
        return (
            f"{data['name_zh']}（{data['name_en']}）#{data['id']}，第 {data['generation']} 世代，"
            f"{data['genus']}\n属性：{data['types']} | 身高 {data['height_m']}m | 体重 {data['weight_kg']}kg\n"
            f"种族值：{stats}\n特性：{abilities}\n图鉴：{data['flavor']}"
        )

    @filter.llm_tool(name="pokeapi_type_matchups")
    async def tool_pokeapi_matchups(
        self, event: AstrMessageEvent, type1: str, type2: str = ""
    ):
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
        return (
            f"【{combo}】\n"
            f"弱点：{'、'.join(result['weak']) or '无'}\n"
            f"抗性：{'、'.join(result['resist']) or '无'}\n"
            f"免疫：{'、'.join(result['immune']) or '无'}"
        )

    # ---------- Phase 1 LLM 工具：能力值 / 进攻相性 / 对比 / 克制推荐 ----------
    @filter.llm_tool(name="calc_pokemon_stats")
    async def tool_calc_stats(
        self,
        event: AstrMessageEvent,
        keyword: str,
        level: int = 50,
        iv: int = 31,
        nature: str = "",
    ):
        """计算宝可梦在指定等级的能力值（官方公式，含个体值与性格修正，努力值按 0）。
        适用于"皮卡丘50级能力值多少""固执性格影响什么数值"这类问题。
        Args:
            keyword(str): 宝可梦的全国图鉴编号或名称（中/日/英均可）
            level(int): 等级（1-100），默认 50
            iv(int): 个体值（0-31，六项统一），默认 31
            nature(str): 性格中文名（如 固执/内敛/胆小），可留空表示无修正
        """
        row = self.db.pokemon(keyword)
        if not row:
            return f"没有查到「{keyword}」的相关信息"
        if nature and nature not in NATURES:
            return f"没有性格「{nature}」，可选：{'、'.join(NATURES)}"
        return self._calc_stats_text(
            row, max(1, min(100, int(level))), max(0, min(31, int(iv))), nature
        )

    @filter.llm_tool(name="query_type_offense")
    async def tool_type_offense(self, event: AstrMessageEvent, type_name: str):
        """查询某属性进攻方向的相性：打哪些属性效果拔群、哪些收效甚微、哪些无效。
        适用于"火属性打什么效果拔群"这类攻击方向的问题（防守方向用 query_type_matchups）。
        Args:
            type_name(str): 属性中文名（如 火、龙）
        """
        result = self.db.attack_matchups(type_name)
        if result is None:
            return f"没有查到属性「{type_name}」"
        return (
            f"【{type_name}属性攻击】\n"
            f"效果拔群：{'、'.join(result['super']) or '无'}\n"
            f"收效甚微：{'、'.join(result['weak']) or '无'}\n"
            f"无效：{'、'.join(result['none']) or '无'}"
        )

    @filter.llm_tool(name="compare_pokemon")
    async def tool_compare(self, event: AstrMessageEvent, name1: str, name2: str):
        """对比两只宝可梦：逐项种族值（含每项更强的一方）、属性互克倍率、特性。
        适用于"皮卡丘和喷火龙谁更强""两只宝可梦对比"这类问题。
        Args:
            name1(str): 第一只宝可梦的编号或名称
            name2(str): 第二只宝可梦的编号或名称
        """
        row1, row2 = self.db.pokemon(name1), self.db.pokemon(name2)
        if not row1 or not row2:
            return f"没有查到「{name1 if not row1 else name2}」的相关信息"
        return self._compare_text(row1, row2)

    @filter.llm_tool(name="find_pokemon_counters")
    async def tool_counters(self, event: AstrMessageEvent, keyword: str):
        """推荐克制某宝可梦的候选列表（双向验证：候选打目标 ≥2倍 且目标打候选 ≤1倍，
        按打击倍率与种族值排序）。适用于"用什么打妙蛙花""妙蛙花的克制是谁"。
        Args:
            keyword(str): 目标宝可梦的编号或名称
        """
        result = self.db.find_counters(keyword)
        if not result:
            return f"没有查到「{keyword}」的相关信息"
        lines = [f"克制 {result['target']}（{result['target_types']}）的推荐："]
        for i, c in enumerate(result["counters"], 1):
            lines.append(
                f"{i}. {c['name']}（{c['types']}）"
                f"打它×{c['atk_mult']:g}｜它打你×{c['def_mult']:g}｜种族{c['total']}"
            )
        if len(lines) == 1:
            lines.append("没有同时满足双向条件的宝可梦")
        return "\n".join(lines)

    # ---------- Phase 2 LLM 工具：招式表 / 配招校验 ----------
    @filter.llm_tool(name="query_learnset")
    async def tool_learnset(
        self, event: AstrMessageEvent, keyword: str, max_level: int = 0
    ):
        """查询宝可梦可学的招式表（升级/招式学习器/遗传分类）与推荐配招。
        适用于"皮卡丘能学什么招""皮卡丘50级学什么""给皮卡丘推荐配招"。
        Args:
            keyword(str): 宝可梦的全国图鉴编号或名称（中/日/英均可）
            max_level(int): 只看该等级及以下可学的升级招式（1-100），0 表示不限等级
        """
        row = self.db.pokemon(keyword)
        if not row:
            return f"没有查到「{keyword}」的相关信息"
        return self._learnset_text(row, max(0, min(100, int(max_level))) or None)

    @filter.llm_tool(name="validate_pokemon_moveset")
    async def tool_validate_moveset(
        self, event: AstrMessageEvent, keyword: str, moves: list[str], level: int = 50
    ):
        """校验宝可梦能否学会给定招式（最多 4 个）。非法招式会给出相近名称建议。
        组队或讨论配招前先调用本工具核实，避免编造招式。
        Args:
            keyword(str): 宝可梦的全国图鉴编号或名称
            moves(list[str]): 招式中文名列表，如 ["十万伏特", "电磁波"]
            level(int): 宝可梦等级（1-100），默认 50
        """
        result = self.db.validate_moveset(
            keyword, list(moves), max(1, min(100, int(level)))
        )
        if "error" in result:
            return result["error"]
        lines = [f"【{result['pokemon']} Lv.{result['level']} 配招校验】"]
        if result["valid"]:
            lines.append("合法：" + "、".join(result["valid"]))
        if result["egg_only"]:
            lines.append("合法但需遗传获得：" + "、".join(result["egg_only"]))
        if result["invalid"]:
            lines.append("学不会：" + "、".join(result["invalid"]))
            if result.get("suggestions"):
                lines.append("相近招式：" + "、".join(result["suggestions"]))
        lines.append(
            "结论："
            + ("配招全部合法" if result["is_valid"] else "配招包含学不会的招式")
        )
        return "\n".join(lines)

    # ---------- Phase 3 LLM 工具：对战模拟 ----------
    @filter.llm_tool(name="simulate_battle")
    async def tool_battle(
        self, event: AstrMessageEvent, name1: str, name2: str, level: int = 50
    ):
        """模拟两只宝可梦的对战并返回战报（简化规则：无特性/道具/天气/招式附加效果，
        双方使用自动推荐的合法配招）。适用于"皮卡丘和喷火龙谁会赢""模拟一场对战"。
        Args:
            name1(str): 第一只宝可梦的编号或名称
            name2(str): 第二只宝可梦的编号或名称
            level(int): 双方等级（1-100），默认 50
        """
        row1, row2 = self.db.pokemon(name1), self.db.pokemon(name2)
        if not row1 or not row2:
            return f"没有查到「{name1 if not row1 else name2}」的相关信息"
        return self._battle_text(row1, row2, max(1, min(100, int(level))))

    # ---------- Phase 4 LLM 工具：队伍分析 / 种族值筛选 ----------
    @filter.llm_tool(name="analyze_team")
    async def tool_team(self, event: AstrMessageEvent, members: list[str]):
        """分析宝可梦队伍（2-6 只）：各成员弱点、共同弱点、全队都不怕的属性、属性覆盖。
        适用于"帮我看看这支队伍有什么问题""这几只组队怕什么"。
        Args:
            members(list[str]): 队伍成员名称列表（2-6 个），如 ["皮卡丘", "喷火龙"]
        """
        names = [m.strip() for m in members if m.strip()][:6]
        if len(names) < 2:
            return "请提供至少 2 只宝可梦"
        return self._team_text(self.db.analyze_team(names))

    @filter.llm_tool(name="find_pokemon_by_stats")
    async def tool_by_stats(
        self,
        event: AstrMessageEvent,
        min_total: int = 0,
        hp_min: int = 0,
        atk_min: int = 0,
        def_min: int = 0,
        spa_min: int = 0,
        spd_min: int = 0,
        spe_min: int = 0,
        type_name: str = "",
        limit: int = 10,
    ):
        """按种族值条件筛选宝可梦（默认形态，按种族总和降序）。适用于组队时找
        "速度超过100的宝可梦""特攻高且耐打的宝可梦"这类需求。
        Args:
            min_total(int): 种族值总和下限，0 表示不限
            hp_min(int): HP 种族值下限，0 表示不限
            atk_min(int): 攻击种族值下限，0 表示不限
            def_min(int): 防御种族值下限，0 表示不限
            spa_min(int): 特攻种族值下限，0 表示不限
            spd_min(int): 特防种族值下限，0 表示不限
            spe_min(int): 速度种族值下限，0 表示不限
            type_name(str): 限定属性（中文，如 龙），可留空
            limit(int): 返回数量上限（1-20），默认 10
        """
        conds = []
        if int(min_total) > 0:
            conds.append(("总和", ">=", int(min_total)))
        for label, val in (
            ("HP", hp_min),
            ("攻击", atk_min),
            ("防御", def_min),
            ("特攻", spa_min),
            ("特防", spd_min),
            ("速度", spe_min),
        ):
            if int(val) > 0:
                conds.append((label, ">=", int(val)))
        if not conds:
            return "请至少提供一个数值条件（如 min_total 或 spe_min）"
        hits = self.db.by_stats(conds, limit=max(1, min(20, int(limit))))
        if type_name:
            hits = [h for h in hits if type_name in (h["types"] or "")]
        if not hits:
            return "没有符合条件的宝可梦"
        cond_text = " ".join(f"{l}>={v}" for l, _, v in conds)
        lines = [f"符合条件的宝可梦（{cond_text}，按总和降序）："]
        lines += [
            f"{i}. {r['name']}（{r['types']}）总和{r['total']}"
            for i, r in enumerate(hits, 1)
        ]
        return "\n".join(lines)
