"""宝可梦计算引擎：等级能力值计算与回合制对战模拟（简化规则）。

能力值公式与官方一致；对战为简化规则——不含特性、道具、天气、场地与
攻击招式附加效果。属性倍率读取插件本地 types.tsv 基准属性表
（由 PokedexDB.type_chart 注入，引擎自身不读文件）。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

# 25 种性格中文名 -> (提升项 +10%, 降低项 -10%)，中性性格为 None
NATURES: dict[str, tuple[str, str] | None] = {
    "勤奋": None,
    "怕寂寞": ("攻击", "防御"),
    "勇敢": ("攻击", "速度"),
    "固执": ("攻击", "特攻"),
    "顽皮": ("攻击", "特防"),
    "大胆": ("防御", "攻击"),
    "坦率": None,
    "悠闲": ("防御", "速度"),
    "淘气": ("防御", "特攻"),
    "乐天": ("防御", "特防"),
    "胆小": ("速度", "攻击"),
    "急躁": ("速度", "防御"),
    "认真": None,
    "爽朗": ("速度", "特攻"),
    "天真": ("速度", "特防"),
    "内敛": ("特攻", "攻击"),
    "慢吞吞": ("特攻", "防御"),
    "温和": ("特攻", "速度"),
    "害羞": None,
    "马虎": ("特攻", "特防"),
    "沉着": ("特防", "攻击"),
    "温柔": ("特防", "防御"),
    "自大": ("特防", "速度"),
    "慎重": ("特防", "特攻"),
    "浮躁": None,
}

STAT_KEYS = ("HP", "攻击", "防御", "特攻", "特防", "速度")

# 异常状态的属性免疫（官方规则：电免麻痹、火免灼伤、毒/钢免中毒）
STATUS_IMMUNE_TYPES: dict[str, set[str]] = {
    "麻痹": {"电"},
    "灼伤": {"火"},
    "中毒": {"毒", "钢"},
}

# 受击类特性 → 免疫属性：持有该特性时对应属性招式对自身无效（特性覆盖属性表）。
# 仅收录纯免疫特性；干燥皮肤（水免疫但火弱点×1.25）等附带修正的特性暂不收录。
# 特性名与本地特性总表一致（该数据集作「飘浮」）。
DEFENSIVE_ABILITY_IMMUNITY: dict[str, str] = {
    "避雷针": "电",
    "蓄电": "电",
    "引火": "火",
    "储水": "水",
    "飘浮": "地面",
}


def _first_int(text) -> int | None:
    m = re.search(r"\d+", str(text or ""))
    return int(m.group()) if m else None


def _per_stat(value, key: str) -> int:
    """个体/努力参数支持单一数值或 {能力项: 数值}。"""
    if isinstance(value, dict):
        return int(value.get(key, 0) or 0)
    return int(value or 0)


def calc_stats(
    base: dict,
    level: int,
    iv: int | dict = 31,
    ev: int | dict | None = None,
    nature: str | None = None,
) -> dict:
    """按官方公式计算等级能力值。

    base 为 {HP/攻击/防御/特攻/特防/速度: 种族值}；iv/ev 可传单一数值或
    分项 dict；nature 传中文名（见 NATURES），未识别按无修正计算。
    HP 种族值为 1 时保持 1（脱壳忍者）。返回同键 dict（附"总和"）。
    """
    level = max(1, min(100, int(level)))
    boost, reduce = NATURES.get(nature or "") or (None, None)
    out = {}
    for key in STAT_KEYS:
        b = int(base.get(key, 1) or 1)
        i = _per_stat(iv, key)
        e = _per_stat(ev, key)
        if key == "HP":
            out[key] = 1 if b == 1 else (2 * b + i + e // 4) * level // 100 + level + 10
        else:
            v = (2 * b + i + e // 4) * level // 100 + 5
            if key == boost:
                v = int(v * 1.1)
            elif key == reduce:
                v = int(v * 0.9)
            out[key] = v
    out["总和"] = sum(out[k] for k in STAT_KEYS)
    return out


# ---------------- 回合制对战模拟（简化规则） ----------------
@dataclass
class BattleMove:
    """参战招式（由招式总表数据构建）。type=None 表示无属性（挣扎）。"""

    name: str
    type: str | None
    category: str  # 物理 / 特殊 / 变化
    power: int | None
    accuracy: int | None
    pp: int
    status: str | None = None  # 命中后施加的异常状态（仅状态招）


def make_move(info: dict, status: str | None = None) -> BattleMove:
    """从 move_list.tsv 行构建参战招式；power/accuracy/pp 取首个数字，缺失取安全默认。"""
    acc = _first_int(info.get("accuracy"))
    return BattleMove(
        name=info.get("name_zh", ""),
        type=info.get("type") or None,
        category=info.get("category") or "变化",
        power=_first_int(info.get("power")),
        accuracy=acc if acc is not None else 100,
        pp=_first_int(info.get("pp")) or 10,
        status=status,
    )


STRUGGLE = BattleMove(
    name="挣扎", type=None, category="物理", power=50, accuracy=100, pp=0
)


@dataclass
class BattlePokemon:
    """参战宝可梦：等级能力值 + 招式 + 当前 HP/异常状态。"""

    name: str
    types: list[str]
    stats: dict  # calc_stats 输出（含"总和"）
    moves: list[BattleMove]
    level: int = 50
    current_hp: int = 0
    status: str | None = None  # 麻痹 / 灼伤 / 中毒

    def __post_init__(self):
        if self.current_hp <= 0:
            self.current_hp = self.stats["HP"]

    @property
    def max_hp(self) -> int:
        return self.stats["HP"]

    @property
    def hp_pct(self) -> float:
        return max(0.0, self.current_hp / self.max_hp)

    @property
    def fainted(self) -> bool:
        return self.current_hp <= 0

    @property
    def effective_speed(self) -> int:
        return (
            int(self.stats["速度"] * 0.5)
            if self.status == "麻痹"
            else self.stats["速度"]
        )

    def take_damage(self, dmg: int) -> int:
        actual = max(0, min(dmg, self.current_hp))
        self.current_hp -= actual
        return actual


class BattleSimulator:
    """回合制对战：速度先手、PP/挣扎、STAB、属性克制、暴击、异常状态。

    chart 为 {(防守属性, 进攻属性): 倍率}；rng 可注入以获得可复现对局。
    简化项：无特性/道具/天气/场地/攻击招式附加效果/命中等级修正。
    """

    def __init__(
        self,
        chart: dict[tuple[str, str], float],
        rng: random.Random | None = None,
        max_turns: int = 100,
    ):
        self.chart = chart
        self.rng = rng or random.Random()
        self.max_turns = max_turns
        self.log: list[str] = []  # (供测试与调试的完整事件流)

    def effectiveness(self, atk_type: str | None, def_types: list[str]) -> float:
        if not atk_type:
            return 1.0
        m = 1.0
        for d in def_types:
            m *= self.chart.get((d, atk_type), 1.0)
        return m

    def simulate(self, p1: BattlePokemon, p2: BattlePokemon) -> dict:
        """模拟整场对战，返回精简战报（胜者/回合数/最终血量/关键事件）。"""
        self.log = []
        pokemon = (p1, p2)
        self.log.append(
            f"{p1.name}（{'/'.join(p1.types)}）vs {p2.name}（{'/'.join(p2.types)}），Lv.{p1.level}"
        )
        turns = 0
        while not (p1.fainted or p2.fainted) and turns < self.max_turns:
            turns += 1
            if p1.effective_speed == p2.effective_speed:
                order = (pokemon, pokemon[::-1])[self.rng.random() < 0.5]
            else:
                order = (
                    (p1, p2) if p1.effective_speed > p2.effective_speed else (p2, p1)
                )
            for attacker, defender in (order, order[::-1]):
                if attacker.fainted or defender.fainted:
                    continue
                self._take_turn(attacker, defender)
            for p in pokemon:  # 回合结束结算异常状态
                if p.fainted:
                    continue
                if p.status == "灼伤":
                    self._status_damage(p, max(1, p.max_hp // 16), "灼伤")
                elif p.status == "中毒":
                    self._status_damage(p, max(1, p.max_hp // 8), "中毒")

        if p1.fainted and p2.fainted:
            winner = loser = "平局"
        elif p2.fainted:
            winner, loser = p1.name, p2.name
        elif p1.fainted:
            winner, loser = p2.name, p1.name
        else:  # 回合上限：按剩余 HP 百分比判定
            winner, loser = (
                (p1.name, p2.name)
                if p1.hp_pct > p2.hp_pct
                else (p2.name, p1.name)
                if p2.hp_pct > p1.hp_pct
                else ("平局", "平局")
            )
        moments = self._key_moments()
        return {
            "winner": winner,
            "loser": loser,
            "turns": turns,
            "final": {
                p.name: {
                    "hp": max(0, p.current_hp),
                    "max": p.max_hp,
                    "pct": round(p.hp_pct * 100),
                    "status": p.status,
                    "fainted": p.fainted,
                }
                for p in pokemon
            },
            "key_moments": moments,
        }

    # ---------- 单回合 ----------
    def _take_turn(self, attacker: BattlePokemon, defender: BattlePokemon):
        if attacker.status == "麻痹" and self.rng.random() < 0.25:
            self.log.append(f"{attacker.name} 因麻痹无法行动！")
            return
        move = self._select_move(attacker)
        if move.pp > 0:
            move.pp -= 1
        if not self._hit(move):
            self.log.append(f"{attacker.name} 的{move.name}没打中！")
            return
        if move.category == "变化":
            self._apply_status(move, attacker, defender)
            return
        self._deal_damage(attacker, defender, move, struggle=move is STRUGGLE)

    def _select_move(self, p: BattlePokemon) -> BattleMove:
        usable = [m for m in p.moves if m.pp > 0]
        if not usable:
            self.log.append(f"{p.name} 没有可用招式，只能使用挣扎！")
            return STRUGGLE
        return self.rng.choice(usable)

    def _hit(self, move: BattleMove) -> bool:
        return move.accuracy is None or self.rng.random() * 100 < move.accuracy

    def _apply_status(
        self, move: BattleMove, attacker: BattlePokemon, defender: BattlePokemon
    ):
        if not move.status:
            self.log.append(f"{attacker.name} 使用了{move.name}（模拟中无效果）。")
            return
        if defender.status:
            self.log.append(
                f"{defender.name} 已处于{defender.status}状态，{move.name}没有效果。"
            )
            return
        immune = STATUS_IMMUNE_TYPES.get(move.status, set()) & set(defender.types)
        if immune:
            self.log.append(
                f"{defender.name}（{'/'.join(defender.types)}）免受{move.status}，{move.name}没有效果。"
            )
            return
        defender.status = move.status
        self.log.append(f"{defender.name} 陷入{move.status}状态！")

    def _deal_damage(
        self,
        attacker: BattlePokemon,
        defender: BattlePokemon,
        move: BattleMove,
        struggle: bool = False,
    ):
        atk = attacker.stats["攻击" if move.category == "物理" else "特攻"]
        if attacker.status == "灼伤" and move.category == "物理":
            atk = int(atk * 0.5)
        dfn = defender.stats["防御" if move.category == "物理" else "特防"]
        eff = self.effectiveness(move.type, defender.types)
        if eff == 0:
            self.log.append(f"{defender.name} 对{move.type}属性攻击免疫，不受伤害！")
            return
        crit = self.rng.random() < 0.0625
        stab = 1.5 if move.type and move.type in attacker.types else 1.0
        dmg = (2 * attacker.level / 5 + 2) * (move.power or 0) * atk / dfn / 50 + 2
        dmg *= stab * eff * (1.5 if crit else 1.0) * self.rng.uniform(0.85, 1.0)
        final = max(1, int(dmg))
        actual = defender.take_damage(final)
        note = ("，暴击！" if crit else "") + (
            "，效果拔群！" if eff > 1 else "，收效甚微…" if eff < 1 else ""
        )
        self.log.append(
            f"{attacker.name} 使用{move.name}，{defender.name} 受到 {actual} 点伤害{note}"
        )
        if defender.fainted:
            self.log.append(f"{defender.name} 倒下了！")
        if struggle and not defender.fainted:
            recoil = max(1, actual // 4)
            attacker.take_damage(recoil)
            self.log.append(f"{attacker.name} 受到挣扎的反作用力 {recoil} 点伤害！")

    def _status_damage(self, p: BattlePokemon, dmg: int, label: str):
        actual = p.take_damage(dmg)
        self.log.append(f"{p.name} 受到{label}伤害 {actual} 点！")
        if p.fainted:
            self.log.append(f"{p.name} 倒下了！")

    def _key_moments(self, limit: int = 5) -> list[str]:
        marks = ("陷入", "倒下", "暴击", "效果拔群", "免疫", "挣扎", "无法行动")
        picks = [line for line in self.log if any(m in line for m in marks)]
        if len(picks) > limit:
            picks = picks[: limit - 1] + [picks[-1]]
        return picks
