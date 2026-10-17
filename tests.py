#!/usr/bin/env python3
"""astrbot_plugin_pokedex 功能测试：uv run python tests.py

核心模块（pokedex_db / battle_engine）直接测试；main.py 通过 stub astrbot
依赖做导入与 llm_tool 注册检查（docstring Args 类型为插件加载硬要求）。
"""

import inspect
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"（{detail}）" if detail else ""))


def section(title: str):
    print(f"\n== {title} ==")


# ---------------- Phase 1 ----------------
def test_phase1_engine():
    from battle_engine import NATURES, calc_stats

    section("Phase 1 / 能力值计算")
    pikachu = {"HP": 35, "攻击": 55, "防御": 40, "特攻": 50, "特防": 50, "速度": 90}
    s = calc_stats(pikachu, 50, iv=0)
    check(
        "L50 个体0 无修正",
        (s["HP"], s["攻击"], s["防御"], s["特攻"], s["特防"], s["速度"])
        == (95, 60, 45, 55, 55, 95),
        str(s),
    )
    s = calc_stats(pikachu, 50, iv=31)
    check(
        "L50 个体31",
        (s["HP"], s["攻击"], s["防御"], s["特攻"], s["特防"], s["速度"])
        == (110, 75, 60, 70, 70, 110),
        str(s),
    )
    s = calc_stats(pikachu, 50, iv=0, nature="固执")
    check(
        "固执 +攻击-特攻（60→66 / 55→49）", (s["攻击"], s["特攻"]) == (66, 49), str(s)
    )
    s = calc_stats(pikachu, 50, iv=0, nature="内敛")
    check("内敛 +特攻-攻击", (s["特攻"], s["攻击"]) == (60, 54), str(s))
    s = calc_stats(pikachu, 50, iv=0, nature="胆小")
    check("胆小 +速度-攻击", (s["速度"], s["攻击"]) == (104, 54), str(s))
    s = calc_stats(pikachu, 50, iv={"HP": 31, "攻击": 0}, nature="浮躁")
    check("分项个体值", s["HP"] == 110 and s["攻击"] == 60, str(s))
    s = calc_stats(pikachu, 50, iv=31, ev={"攻击": 252})
    check("努力值 252（等级缩放后 +32）", s["攻击"] == 107, str(s))
    check(
        "种族HP=1 保持 1（脱壳忍者）",
        calc_stats(
            {"HP": 1, "攻击": 90, "防御": 45, "特攻": 30, "特防": 30, "速度": 40}, 50
        )["HP"]
        == 1,
    )
    check(
        "未知性格按无修正",
        calc_stats(pikachu, 50, iv=0, nature="不存在的性格")["攻击"] == 60,
    )
    check("性格表完整 25 种", len(NATURES) == 25)
    check(
        "等级越界收敛 1-100",
        calc_stats(pikachu, 999)["HP"] == calc_stats(pikachu, 100)["HP"],
    )


def test_phase1_matchups():
    section("Phase 1 / 进攻相性与互克倍率")
    db = PokedexDB()
    r = db.attack_matchups("火")
    check(
        "火攻效果拔群 草/冰/虫/钢×2",
        sorted(r["super"]) == sorted(["草×2", "冰×2", "虫×2", "钢×2"]),
        str(r["super"]),
    )
    check(
        "火攻收效甚微 火/水/岩石/龙×0.5",
        sorted(r["weak"]) == sorted(["火×0.5", "水×0.5", "岩石×0.5", "龙×0.5"]),
        str(r["weak"]),
    )
    check("火攻无无效属性", r["none"] == [])
    check("未知属性返回 None", db.attack_matchups("光") is None)
    check("电攻打地面无效", "地面" in db.attack_matchups("电")["none"])
    check("水打火/岩石 = 4×", db.matchup_multiplier(["水"], ["火", "岩石"]) == 4.0)
    check("一般打幽灵 = 0×", db.matchup_multiplier(["一般"], ["幽灵"]) == 0.0)
    check("电打水/飞行 = 4×", db.matchup_multiplier(["电"], ["水", "飞行"]) == 4.0)
    check(
        "电打火/飞行 = 2×（克飞行不克火）",
        db.matchup_multiplier(["电"], ["火", "飞行"]) == 2.0,
    )


def test_phase1_counters():
    section("Phase 1 / 克制推荐（双向验证）")
    db = PokedexDB()
    r = db.find_counters("妙蛙种子")
    check(
        "妙蛙种子有候选",
        r and len(r["counters"]) >= 3,
        str(len(r["counters"]) if r else 0),
    )
    check("目标信息正确", r["target"] == "妙蛙种子" and r["target_types"] == "草/毒")
    ok = all(c["atk_mult"] >= 2 and c["def_mult"] <= 1 for c in r["counters"])
    check("全部候选满足 双向≥2×/≤1×", ok)
    check(
        "排序：打击倍率降序",
        all(
            r["counters"][i]["atk_mult"] >= r["counters"][i + 1]["atk_mult"]
            for i in range(len(r["counters"]) - 1)
        ),
    )
    names = [c["name"] for c in r["counters"]]
    check(
        "不含目标自身/形态行",
        "妙蛙种子" not in names and "妙蛙花" not in names,
        str(names[:5]),
    )
    check("未知宝可梦返回 None", db.find_counters("皮卡梦") is None)


def test_phase1_compare_data():
    section("Phase 1 / 对比数据")
    db = PokedexDB()
    p1, p2 = db.pokemon("皮卡丘"), db.pokemon("喷火龙")
    st1, st2 = db.first_stats(p1), db.first_stats(p2)
    check("皮卡丘种族总和 320", st1[1] == 320, str(st1))
    check("喷火龙种族总和 534", st2[1] == 534, str(st2))
    check("电打火/飞行 ×2", db.matchup_multiplier(["电"], ["火", "飞行"]) == 2.0)
    check(
        "火/飞行打电 ×0.5（电抗飞行）",
        db.matchup_multiplier(["火", "飞行"], ["电"]) == 0.5,
    )


# ---------------- main.py 导入与注册检查 ----------------
def test_main_imports():
    section("main.py 导入与 llm_tool 注册（stub astrbot）")

    registered = {"commands": [], "tools": [], "tool_funcs": []}

    def _mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    class _Dummy:
        pass

    def _command(name, **kw):
        def deco(fn):
            registered["commands"].append(name)
            return fn

        return deco

    def _llm_tool(name=None, **kw):
        def deco(fn):
            registered["tools"].append(name or fn.__name__)
            registered["tool_funcs"].append(fn)
            return fn

        return deco

    _mod("astrbot")
    _mod(
        "astrbot.api",
        logger=types.SimpleNamespace(
            info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None
        ),
        AstrBotConfig=dict,
    )
    _mod(
        "astrbot.api.event",
        filter=types.SimpleNamespace(command=_command, llm_tool=_llm_tool),
        AstrMessageEvent=_Dummy,
    )
    _mod(
        "astrbot.api.star",
        Context=_Dummy,
        Star=type("Star", (), {}),
        register=lambda *a, **kw: lambda cls: cls,
    )
    _mod(
        "astrbot.api.message_components",
        Image=type("Image", (), {"fromBytes": staticmethod(lambda b: None)}),
        Plain=lambda *a, **kw: None,
    )

    import main  # 导入即触发指令/工具装饰器注册（下方有引用，勿删）

    expected_cmds = {
        "宝可梦",
        "特性",
        "招式",
        "道具",
        "图鉴",
        "相性",
        "能力值",
        "克制",
        "对比",
        "怎么打",
        "招式表",
        "对战",
        "队伍分析",
        "筛选",
        "许愿星",
        "搜索",
        "宝可梦帮助",
    }
    check(
        "指令全部注册",
        expected_cmds <= set(registered["commands"]),
        str(expected_cmds - set(registered["commands"])),
    )

    expected_tools = {
        "query_pokemon",
        "search_pokemon",
        "query_ability",
        "query_move",
        "query_dex_region",
        "query_type_matchups",
        "query_wishmaker_jirachi",
        "find_pokemon_by_type",
        "pokeapi_pokemon_detail",
        "pokeapi_type_matchups",
        "calc_pokemon_stats",
        "query_type_offense",
        "compare_pokemon",
        "find_pokemon_counters",
        "query_learnset",
        "validate_pokemon_moveset",
        "simulate_battle",
        "analyze_team",
        "find_pokemon_by_stats",
    }
    check(
        "llm_tool 全部注册",
        expected_tools <= set(registered["tools"]),
        str(expected_tools - set(registered["tools"])),
    )

    bad = []
    for fn in registered["tool_funcs"]:
        doc = inspect.getdoc(fn) or ""
        params = [
            p for p in inspect.signature(fn).parameters if p not in ("self", "event")
        ]
        for p in params:
            if not re.search(rf"^\s*{re.escape(p)}\([^):]+\):", doc, re.MULTILINE):
                bad.append(f"{fn.__name__}:{p}")
    check("llm_tool 参数全部有 docstring 类型标注（Args）", not bad, str(bad))

    conds = main.parse_stat_conditions("速度>100 特攻>=120 总和<=500")
    check(
        "筛选条件解析",
        conds == [("速度", ">", 100), ("特攻", ">=", 120), ("总和", "<=", 500)],
        str(conds),
    )
    check(
        "非法筛选条件返回 None",
        main.parse_stat_conditions("速度>>100") is None
        and main.parse_stat_conditions("") is None,
    )


# ---------------- Phase 2 ----------------
def test_phase2_learnset():
    section("Phase 2 / 招式表与配招")
    db = PokedexDB()
    row = db.pokemon("妙蛙种子")
    ls = db.learnset(row)
    check(
        "新格式解析：首招 撞击(1)",
        ls["level_up"][0] == ("撞击", 1),
        str(ls["level_up"][:3]),
    )
    check("等级内嵌：藤鞭(3)", ("藤鞭", 3) in ls["level_up"])
    check(
        "招式学习器非空且无等级",
        ls["machine"] and all(lv is None for _, lv in ls["machine"]),
    )
    check(
        "形态行回退默认形态行",
        db.learnset(db.pokemon("超级烈咬陆鲨"))["level_up"]
        == db.learnset(db.pokemon("烈咬陆鲨"))["level_up"],
    )

    old_row = {
        "level_up_moves": "撞击、叫声、藤鞭",
        "machine_moves": "守住、剑舞",
        "egg_moves": "",
        "form_name": "",
    }
    old = db.learnset(old_row)
    check(
        "旧格式兼容：等级为 None",
        old["level_up"] == [("撞击", None), ("叫声", None), ("藤鞭", None)]
        and old["machine"] == [("守住", None), ("剑舞", None)],
    )

    check("move_power：十万伏特 90", db.move_power(db.move_index()["十万伏特"]) == 90)
    check("move_power：变化招 None", db.move_power(db.move_index()["剑舞"]) is None)

    rec, from_ls = db.recommended_moveset(row)
    learnable = {n for n, _ in ls["level_up"] + ls["machine"] + ls["egg"]}
    check(
        "推荐配招 4 个且全部来自学习表",
        len(rec) == 4 and set(rec) <= learnable,
        str(rec),
    )
    check("推荐配招标记 from_learnset", from_ls)
    demo_row = {
        "name_zh": "测试",
        "types": "火",
        "form_name": "",
        "level_up_moves": "",
        "machine_moves": "",
        "egg_moves": "",
    }
    demo, demo_ok = db.recommended_moveset(demo_row)
    check(
        "无学习表回退演示配招（火属性全局高威力）",
        len(demo) == 4
        and not demo_ok
        and all(db.move_index()[n]["type"] == "火" for n in demo),
        str(demo),
    )

    v = db.validate_moveset("皮卡丘", ["十万伏特", "电磁波", "十万福特"])
    check("校验：TM 与状态招合法", set(v["valid"]) == {"十万伏特", "电磁波"}, str(v))
    check(
        "校验：错别字招式判非法并给建议",
        v["invalid"] == ["十万福特"] and "十万伏特" in v["suggestions"],
        str(v.get("suggestions")),
    )
    check("校验：is_valid 标记", v["is_valid"] is False)

    learnable_up = [(n, lv) for n, lv in ls["level_up"] if lv and lv >= 5]
    if learnable_up:
        mv, lv = learnable_up[0]
        v_low = db.validate_moveset("妙蛙种子", [mv], level=1)
        v_ok = db.validate_moveset("妙蛙种子", [mv], level=100)
        check(
            f"等级过滤：{mv}({lv}) 1级不可/100级可",
            mv in v_low["invalid"] and mv in v_ok["valid"],
        )
    if ls["egg"]:
        egg_move = ls["egg"][0][0]
        v_egg = db.validate_moveset("妙蛙种子", [egg_move])
        check(f"遗传招单独标注：{egg_move}", egg_move in v_egg["egg_only"], str(v_egg))


def test_phase2_data_quality():
    section("Phase 2 / 数据覆盖抽查")
    db = PokedexDB()
    rows = [r for r in db.table("pokemon") if not r.get("form_name")]
    with_ls = sum(1 for r in rows if (r.get("level_up_moves") or "").strip())
    check(
        "默认形态招式表覆盖 ≥95%", with_ls / len(rows) >= 0.95, f"{with_ls}/{len(rows)}"
    )
    ls = db.learnset(db.pokemon("皮卡丘"))
    idx = db.move_index()
    missing = [n for n, _ in ls["level_up"] + ls["machine"] if n not in idx]
    check("皮卡丘招式表全部可在招式总表命中", not missing, str(missing[:5]))


# ---------------- Phase 3 ----------------
def test_phase3_battle():
    import random

    from battle_engine import (
        BattleMove,
        BattlePokemon,
        BattleSimulator,
        calc_stats,
        make_move,
    )

    section("Phase 3 / 对战模拟")
    db = PokedexDB()
    chart = db.type_chart()

    def mk(row, level):
        stats = calc_stats(dict(db.first_stats(row)[0]), level)
        moves, _ = db.recommended_moveset(row)
        bms = [make_move(db.move_index()[n], db.STATUS_MOVES.get(n)) for n in moves]
        return BattlePokemon(
            name=row["name_zh"],
            types=row["types"].split("/"),
            stats=stats,
            moves=bms,
            level=level,
        )

    pika, charz = db.pokemon("皮卡丘"), db.pokemon("喷火龙")

    winners, turns_ok = set(), True
    for seed in range(20):
        rep = BattleSimulator(chart, rng=random.Random(seed)).simulate(
            mk(pika, 50), mk(charz, 50)
        )
        winners.add(rep["winner"])
        turns_ok = turns_ok and 1 <= rep["turns"] <= 100
    check(
        "20 场对战全部正常结束",
        turns_ok and winners <= {"皮卡丘", "喷火龙"},
        str(winners),
    )
    check("双方都有胜出（随机性生效）", len(winners) == 2, str(winners))

    r1 = BattleSimulator(chart, rng=random.Random(7)).simulate(
        mk(pika, 50), mk(charz, 50)
    )
    r2 = BattleSimulator(chart, rng=random.Random(7)).simulate(
        mk(pika, 50), mk(charz, 50)
    )
    check("同种子结果完全可复现", r1 == r2)

    wins = sum(
        BattleSimulator(chart, rng=random.Random(s)).simulate(
            mk(charz, 100), mk(pika, 1)
        )["winner"]
        == "喷火龙"
        for s in range(20)
    )
    check("等级压制：100级喷火龙 20 战 ≥19 胜 1 级皮卡丘", wins >= 19, f"{wins}/20")

    tank = {"HP": 200, "攻击": 20, "防御": 20, "特攻": 20, "特防": 20, "速度": 20}
    tiny = BattleMove(
        name="撞击", type="一般", category="物理", power=40, accuracy=100, pp=1
    )
    a = BattlePokemon(
        name="甲", types=["一般"], stats=calc_stats(tank, 50), moves=[tiny], level=50
    )
    b = BattlePokemon(
        name="乙",
        types=["一般"],
        stats=calc_stats(tank, 50),
        moves=[
            BattleMove(
                name="叫声",
                type="一般",
                category="变化",
                power=None,
                accuracy=100,
                pp=1,
            )
        ],
        level=50,
    )
    sim = BattleSimulator(chart, rng=random.Random(1))
    rep = sim.simulate(a, b)
    check("PP 耗尽后使用挣扎（含反伤）", any("挣扎" in x for x in sim.log))
    check("挣扎有反伤记录", any("反作用力" in x for x in sim.log))

    status_atk = BattlePokemon(
        name="特",
        types=["一般"],
        stats=calc_stats(tank, 50),
        moves=[make_move(db.move_index()["电磁波"], "麻痹")],
        level=50,
    )
    sim = BattleSimulator(chart, rng=random.Random(3))
    rep = sim.simulate(status_atk, mk(pika, 50))
    check("电属性免疫麻痹（电磁波对皮卡丘无效）", any("免受麻痹" in x for x in sim.log))

    elec = BattleMove(
        name="十万伏特", type="电", category="特殊", power=90, accuracy=100, pp=999
    )
    g1 = BattlePokemon(
        name="地1", types=["地面"], stats=calc_stats(tank, 50), moves=[elec], level=50
    )
    g2 = BattlePokemon(
        name="地2",
        types=["岩石", "地面"],
        stats=calc_stats(tank, 50),
        moves=[elec],
        level=50,
    )
    sim = BattleSimulator(chart, rng=random.Random(5))
    rep = sim.simulate(g1, g2)
    check(
        "电攻打地面完全免疫（0 伤害打满回合上限后平局）",
        rep["turns"] == 100
        and rep["winner"] == "平局"
        and any("免疫" in x for x in sim.log),
        str(rep["turns"]),
    )

    burn_atk = BattlePokemon(
        name="烧",
        types=["一般"],
        stats=calc_stats(tank, 50),
        moves=[make_move(db.move_index()["磷火"], "灼伤")],
        level=50,
    )
    sim = BattleSimulator(chart, rng=random.Random(9))
    rep = sim.simulate(burn_atk, mk(pika, 50))
    check(
        "磷火对非火属性命中灼伤并结算回合伤害",
        any("陷入灼伤" in x for x in sim.log) and any("灼伤伤害" in x for x in sim.log),
    )

    weak = BattleMove(
        name="撞击", type="一般", category="物理", power=40, accuracy=100, pp=999
    )
    t1 = BattlePokemon(
        name="坦1", types=["一般"], stats=calc_stats(tank, 50), moves=[weak], level=50
    )
    t2 = BattlePokemon(
        name="坦2", types=["一般"], stats=calc_stats(tank, 50), moves=[weak], level=50
    )
    sim = BattleSimulator(chart, rng=random.Random(11), max_turns=3)
    rep = sim.simulate(t1, t2)
    check(
        "回合上限生效（3 回合无人倒下）",
        rep["turns"] == 3 and not any(st["fainted"] for st in rep["final"].values()),
        str(rep["turns"]),
    )

    mv = make_move(db.move_index()["十万伏特"])
    check(
        "make_move：十万伏特 威力90/命中100/有PP",
        mv.power == 90 and mv.accuracy == 100 and mv.pp > 0,
    )
    mv = make_move(db.move_index()["剑舞"])
    check("make_move：剑舞 变化招无威力", mv.category == "变化" and mv.power is None)


# ---------------- Phase 4 ----------------
def test_phase4_team_and_stats():
    section("Phase 4 / 队伍分析与种族值筛选")
    db = PokedexDB()
    r = db.analyze_team(["大岩蛇", "隆隆岩"])
    check(
        "双岩队共同弱点含 水（4倍）",
        any(a == "水" and len(ns) == 2 for a, ns in r["common_weaknesses"]),
        str(r["common_weaknesses"][:3]),
    )
    check("共同弱点计数全部 ≥2", all(len(ns) >= 2 for _, ns in r["common_weaknesses"]))
    check(
        "全队不怕的属性含 一般/毒/飞行",
        {"一般", "毒", "飞行"} <= set(r["team_resists"]),
        str(r["team_resists"][:6]),
    )
    check("属性覆盖 岩石+地面", {"岩石", "地面"} <= set(r["attack_coverage"]))
    check("成员弱点列表非空", all(m["weak"] for m in r["members"]))

    r2 = db.analyze_team(["皮卡丘", "不存在的宝可梦"])
    check(
        "未知成员记录错误不崩溃",
        r2["members"][1].get("error") == "未找到" and len(r2["members"]) == 2,
    )
    check("单有效成员无共同弱点（阈值≥2）", r2["common_weaknesses"] == [])

    hits = db.by_stats([("速度", ">", 100)], limit=15)
    from pokedex_db import PokedexDB as _  # noqa: F401

    ok = True
    for h in hits:
        row = db.pokemon(h["name"])
        speed = dict(db.first_stats(row)[0])["速度"]
        ok = ok and speed > 100
    check("速度>100 结果全部达标且非空", hits and ok, f"{len(hits)} 条")
    totals = [h["total"] for h in hits]
    check("结果按种族总和降序", totals == sorted(totals, reverse=True))

    top = db.by_stats([("总和", ">=", 600)], limit=5)
    check(
        "总和≥600 有命中且全部达标",
        top and all(h["total"] >= 600 for h in top),
        str([h["name"] for h in top[:3]]),
    )
    check(
        "多条件 AND：速度>110 且 总和<=450",
        all(
            h["total"] <= 450
            for h in db.by_stats([("速度", ">", 110), ("总和", "<=", 450)])
        ),
    )
    check("无命中返回空列表", db.by_stats([("总和", ">", 9999)]) == [])


# ---------------- 受击类特性免疫 / 相性修正 ----------------
def test_ability_corrections():
    section("受击类特性映射 / 相性修正字段")
    db = PokedexDB()
    check(
        "皮卡丘隐藏特性避雷针 → 免疫 电",
        db.ability_immunities(db.pokemon("皮卡丘")) == [("电", "避雷针")],
    )
    check("无相关特性的宝可梦返回空", db.ability_immunities(db.pokemon("大岩蛇")) == [])

    floats = [
        r
        for r in db.table("pokemon")
        if not r.get("form_name") and "飘浮" in (r.get("abilities") or "")
    ]
    check("数据集中存在飘浮特性持有者", bool(floats))
    if floats:
        check(
            f"飘浮持有者（{floats[0]['name_zh']}）→ 免疫 地面",
            db.ability_immunities(floats[0]) == [("地面", "飘浮")],
        )

    r = db.matchups(["电"])
    corr = {c["type"]: c for c in r["ability_corrections"]}
    check(
        "相性 电：地面×2 可被飘浮免疫",
        corr.get("地面", {}).get("abilities") == ["飘浮"]
        and corr["地面"]["current_mult"] == 2.0,
    )
    check(
        "相性 电：避雷针/蓄电 合并展示（现×0.5）",
        set(corr.get("电", {}).get("abilities", [])) == {"避雷针", "蓄电"}
        and corr["电"]["current_mult"] == 0.5,
    )
    check(
        "修正按当前倍率降序",
        [c["current_mult"] for c in r["ability_corrections"]]
        == sorted((c["current_mult"] for c in r["ability_corrections"]), reverse=True),
    )

    r2 = db.matchups(["一般"])
    corr2 = {c["type"] for c in r2["ability_corrections"]}
    check("已免疫属性不重复修正（幽灵打一般 0× 不出现）", "幽灵" not in corr2)
    check("无对应免疫特性的属性不出现（格斗打一般 2×）", "格斗" not in corr2)
    check("火属性有引火修正", "火" in corr2)


from pokedex_db import PokedexDB

if __name__ == "__main__":
    test_phase1_engine()
    test_phase1_matchups()
    test_phase1_counters()
    test_phase1_compare_data()
    test_phase2_learnset()
    test_phase2_data_quality()
    test_phase3_battle()
    test_phase4_team_and_stats()
    test_ability_corrections()
    test_main_imports()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
