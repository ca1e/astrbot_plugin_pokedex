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

import re
from pathlib import Path

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

    async def _send_pokemon(
        self, event: AstrMessageEvent, row: dict, card: bool, hint: str = ""
    ):
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
            yield event.plain_result(
                "用法：宝可梦 <编号或名称>，如：宝可梦 999 / 宝可梦 索财灵"
            )
            return
        rows = self.db.query("pokemon", keyword)
        if not rows:
            yield event.plain_result(f"没有查到「{keyword}」的相关信息")
            return
        row = self.db.pick_best(rows, keyword)
        others = sorted(
            {r["form_name"] for r in rows if r.get("form_name") and r is not row}
        )
        hint = ""
        if others:
            hint = (
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

    @filter.command("图鉴")
    async def cmd_dex(self, event: AstrMessageEvent, keyword: str = ""):
        """在 24 个地区图鉴中定位（编号或地区名）"""
        if not keyword:
            yield event.plain_result(
                "用法：图鉴 <编号> 或 图鉴 <地区名>，如：图鉴 999 / 图鉴 丰缘"
            )
            return
        rows = self.db.query("pokedex", keyword)
        if not rows:
            yield event.plain_result(f"没有查到「{keyword}」的图鉴记录")
            return
        lines = []
        for r in rows[:10]:
            if r.get("dex") == "national":
                lines.append(
                    f"全国图鉴 {r['id_raw']}：{r.get('name', '')}（{r.get('types', '')}）"
                )
            else:
                lines.append(
                    f"{r.get('dex', '')}图鉴 {r.get('id_raw', '')}"
                    f"（全国 {r.get('national_id', '')}）：{r.get('name', '')}（{r.get('types', '')}）"
                )
        yield event.plain_result("\n".join(lines))

    @filter.command("相性")
    async def cmd_matchups(
        self, event: AstrMessageEvent, type1: str = "", type2: str = ""
    ):
        """属性相性（本地属性表），如：相性 火 飞行"""
        if not type1:
            yield event.plain_result("用法：相性 <属性1> [属性2]，如：相性 火 飞行")
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

    @filter.command("能力值")
    async def cmd_stats(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
        level: str = "50",
        iv: str = "31",
        nature: str = "",
    ):
        """计算等级能力值，如：能力值 皮卡丘 50 31 固执"""
        if not keyword:
            yield event.plain_result(
                "用法：能力值 <名称> [等级] [个体] [性格]\n"
                "如：能力值 皮卡丘 50 31 固执（等级默认50，个体默认31，性格可省略）"
            )
            return
        row = self.db.pokemon(keyword)
        if not row:
            yield event.plain_result(f"没有查到「{keyword}」的相关信息")
            return
        if nature and nature not in NATURES:
            yield event.plain_result(
                f"没有性格「{nature}」，可选：{'、'.join(NATURES)}"
            )
            return
        level_i = self._parse_clamped_int(level, 50, 1, 100)
        iv_i = self._parse_clamped_int(iv, 31, 0, 31)
        yield event.plain_result(self._calc_stats_text(row, level_i, iv_i, nature))

    @filter.command("克制")
    async def cmd_offense(self, event: AstrMessageEvent, type_name: str = ""):
        """属性进攻相性，如：克制 火"""
        if not type_name:
            yield event.plain_result(
                "用法：克制 <属性>，如：克制 火（查「打谁效果拔群」）"
            )
            return
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
            f"【{n1} vs {n2}】",
            f"属性：{row1.get('types', '')} vs {row2.get('types', '')}",
        ]
        for (label, v1), (_, v2) in zip(parsed1[0], parsed2[0]):
            lines.append(self._stat_compare_line(label, v1, v2, n1, n2))
        lines.append(self._stat_compare_line("总和", parsed1[1], parsed2[1], n1, n2))
        lines.append(f"互克：{n1} 打 {n2} ×{m12:g}｜{n2} 打 {n1} ×{m21:g}")
        ab1 = row1.get("abilities", "") or ""
        ab2 = row2.get("abilities", "") or ""
        lines.append(f"特性：{n1} {ab1}｜{n2} {ab2}")
        return "\n".join(lines)

    @filter.command("对比")
    async def cmd_compare(
        self, event: AstrMessageEvent, name1: str = "", name2: str = ""
    ):
        """对比两只宝可梦的种族值/属性互克/特性，如：对比 皮卡丘 喷火龙"""
        if not name1 or not name2:
            yield event.plain_result(
                "用法：对比 <名称1> <名称2>，如：对比 皮卡丘 喷火龙"
            )
            return
        row1, row2 = self.db.pokemon(name1), self.db.pokemon(name2)
        if not row1 or not row2:
            miss = name1 if not row1 else name2
            yield event.plain_result(f"没有查到「{miss}」的相关信息")
            return
        yield event.plain_result(self._compare_text(row1, row2))

    @filter.command("怎么打")
    async def cmd_counters(self, event: AstrMessageEvent, keyword: str = ""):
        """推荐克制某宝可梦的候选，如：怎么打 妙蛙花"""
        if not keyword:
            yield event.plain_result("用法：怎么打 <名称>，如：怎么打 妙蛙花")
            return
        result = self.db.find_counters(keyword)
        if not result:
            yield event.plain_result(f"没有查到「{keyword}」的相关信息")
            return
        lines = [
            f"【怎么打 {result['target']}（{result['target_types']}）】",
            "筛选条件：打它 ≥2× 且它打你 ≤1×，按打击倍率与种族值排序",
        ]
        for i, c in enumerate(result["counters"], 1):
            lines.append(
                f"{i}. {c['name']}（{c['types']}）"
                f"打它×{c['atk_mult']:g}｜它打你×{c['def_mult']:g}｜种族{c['total']}"
            )
        if len(lines) == 2:
            lines.append("没有同时满足条件的宝可梦，可放宽要求参考属性相性")
        yield event.plain_result("\n".join(lines))

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

    @filter.command("招式表")
    async def cmd_learnset(
        self, event: AstrMessageEvent, keyword: str = "", level: str = ""
    ):
        """查询宝可梦可学招式与推荐配招，如：招式表 皮卡丘 50"""
        if not keyword:
            yield event.plain_result("用法：招式表 <名称> [等级]，如：招式表 皮卡丘 50")
            return
        row = self.db.pokemon(keyword)
        if not row:
            yield event.plain_result(f"没有查到「{keyword}」的相关信息")
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

    def _battle_text(self, row1: dict, row2: dict, level: int) -> str:
        sim = BattleSimulator(self.db.type_chart())
        report = sim.simulate(
            self._battle_pokemon(row1, level), self._battle_pokemon(row2, level)
        )
        lines = [
            f"【{row1.get('name_zh', '')} vs {row2.get('name_zh', '')}】Lv.{level} 对战",
            "规则：简化规则（无特性/道具/天气/招式附加效果），配招为自动推荐",
        ]
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
            lines.append(f"{m['name']}（{m['types']}）：怕 {weak}")
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
                parts = [
                    f"{r.get(c, '')}"
                    for c in ("id_raw", "national_id", "name_zh", "name_en")
                    if r.get(c)
                ]
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
            "相性 <属性1> [属性2] — 属性相性（防守方向）\n"
            "克制 <属性> — 属性进攻相性（打谁效果拔群）\n"
            "能力值 <名称> [等级] [个体] [性格] — 等级能力值计算\n"
            "对比 <名称1> <名称2> — 两只宝可梦对比\n"
            "怎么打 <名称> — 推荐克制它的宝可梦\n"
            "招式表 <名称> [等级] — 可学招式与推荐配招\n"
            "对战 <名称1> <名称2> [等级] — 模拟对战（简化规则）\n"
            "队伍分析 <名称...> — 队伍弱点配置分析\n"
            "筛选 <条件> — 按种族值筛选，如 速度>100 特攻>=120\n"
            "许愿星 <性格> [个体下限] [TID SID] — 许愿星基拉祈目标筛选\n"
            "搜索 <关键词> — 跨表搜索\n"
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
        return (
            f"【{combo}】\n"
            f"弱点：{'、'.join(result['weak']) or '无'}\n"
            f"抗性：{'、'.join(result['resist']) or '无'}\n"
            f"免疫：{'、'.join(result['immune']) or '无'}"
        )

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
