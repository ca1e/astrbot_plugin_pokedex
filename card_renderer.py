"""Pillow 图片卡片渲染（参考 astrbot_plugin_bazaar 的实现方式：纯 Pillow 绘制，
不依赖浏览器渲染服务）。"""

import io
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    from astrbot.api import logger
except ImportError:  # 脱离 AstrBot 环境本地测试时使用
    import logging

    logger = logging.getLogger(__name__)

FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",  # macOS
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",  # Debian/Ubuntu
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",  # Arch/Fedora
    "C:/Windows/Fonts/msyh.ttc",  # Windows
    "C:/Windows/Fonts/simhei.ttf",
]

TYPE_COLORS = {
    "一般": (154, 161, 159),
    "火": (230, 40, 40),
    "水": (41, 128, 239),
    "草": (63, 161, 41),
    "电": (250, 192, 0),
    "冰": (61, 206, 243),
    "格斗": (255, 128, 0),
    "毒": (145, 65, 203),
    "地面": (145, 81, 33),
    "飞行": (129, 185, 239),
    "超能力": (239, 65, 121),
    "虫": (145, 161, 25),
    "岩石": (175, 169, 129),
    "幽灵": (112, 65, 112),
    "龙": (80, 96, 225),
    "恶": (98, 77, 78),
    "钢": (161, 161, 161),
    "妖精": (239, 112, 239),
}
STAT_COLORS = {
    "HP": (255, 89, 94),
    "攻击": (255, 144, 60),
    "防御": (255, 202, 58),
    "特攻": (74, 144, 226),
    "特防": (67, 200, 144),
    "速度": (180, 120, 255),
}
BG = (250, 250, 252)
CARD = (255, 255, 255)
TEXT = (45, 45, 48)
SUBTEXT = (120, 122, 128)
LINE = (228, 230, 235)

# 进化链节点几何：立绘缩放边长 / 节点宽（名字在图下） / 箭头区宽（含进化条件）
EVO_IMG, EVO_NODE_W, EVO_ARROW_W = 96, 124, 116
# 闪光立绘缩放边长（贴在主立绘下方）
SHINY = 224
# 图鉴说明：世代徽章配色（对应各世代游戏主色调，装饰用）
DEX_GEN_ORDER = {c: i for i, c in enumerate("一二三四五六七八九", 1)}
DEX_GEN_COLORS = [
    (196, 57, 43),
    (188, 142, 0),
    (32, 143, 120),
    (47, 107, 190),
    (86, 88, 96),
    (117, 103, 204),
    (235, 125, 0),
    (0, 118, 150),
    (214, 60, 92),
]

# 从 stats 文本中提取各形态种族值总和，如 "超级喷火龙Ｘ形态：HP78/…（总和634）"
STAT_FORM_RE = re.compile(
    r"(?:([\w]+?)形态：)?HP\d+/攻击\d+/防御\d+/特攻\d+/特防\d+/速度\d+（总和(\d+)）"
)


def _split_top_level(text: str, sep: str) -> list[str]:
    """按分隔符切分文本，忽略（）括号内部的分隔符。

    进化条件里有嵌套括号甚至内嵌"；"（如"（等级20以上（夜晚） ；等级20以上（Let's Go!…））"），
    不能直接 str.split。
    """
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "（":
            depth += 1
        elif ch == "）":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def _parse_dex_entries(text: str) -> list:
    """dex_entries -> [(世代标签, [条目, ...]), ...]，按"第X世代："分组。

    世代内条目以"；"分隔；无世代标记时返回空列表（调用方回退整段文本）。
    """
    parts = re.split(r"(第[一二三四五六七八九]世代：)", text)
    gens = []
    for i in range(1, len(parts), 2):
        entries = [e.strip() for e in parts[i + 1].split("；") if e.strip()]
        if entries:
            gens.append((parts[i][:-1], entries))
    return gens


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    logger.warning("未找到中文字体，卡片将使用默认字体（可能显示为方框）")
    return ImageFont.load_default()


class CardRenderer:
    def __init__(self, images_dir=None):
        self.images_dir = Path(images_dir) if images_dir else None
        self._fonts = {}
        self._evo_imgs = {}

    def _font(self, size):
        if size not in self._fonts:
            self._fonts[size] = load_font(size)
        return self._fonts[size]

    # ---------- 基础绘制 ----------
    @staticmethod
    def _wrap(draw, text, font, max_width):
        lines, cur = [], ""
        for ch in str(text):
            if ch == "\n":
                lines.append(cur)
                cur = ""
                continue
            if draw.textlength(cur + ch, font=font) > max_width:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
        return lines or [""]

    def _badge(self, draw, xy, text, fill, font):
        x, y = xy
        tw = draw.textlength(text, font=font)
        w, h = tw + 28, 44
        draw.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=fill)
        draw.text((x + 14, y + 5), text, font=font, fill=(255, 255, 255))
        return x + w + 12

    def _section_title(self, draw, x, y, text):
        draw.text((x, y), text, font=self._font(30), fill=SUBTEXT)
        return y + 46

    # ---------- 进化链 ----------
    def _evolution_chain(self, row: dict, db) -> list | None:
        """解析 evolution 字段为分支段列表，每段是 (条件, 名称) 步骤序列。

        分支以顶层"；"分隔（条件内嵌的分号不算），完全重复的段去重（上游存在
        整段重复的脏数据，如结草儿三段全同）。
        """
        text = (row.get("evolution") or "").strip()
        if not text or text == "无":
            return None
        segs: list = []
        for seg in _split_top_level(text, "；"):
            steps = []
            for st in _split_top_level(seg, "→"):
                st = st.strip()
                cond, name = "", st
                if st.startswith("（"):
                    depth = 0
                    for j, ch in enumerate(st):
                        if ch == "（":
                            depth += 1
                        elif ch == "）":
                            depth -= 1
                            if depth == 0:
                                cond, name = st[1:j], st[j + 1 :].strip()
                                break
                steps.append((cond.strip(), name))
            if steps and steps not in segs:
                segs.append(steps)
        return segs or None

    def _resolve_evo_images(self, chain, db):
        """为进化链节点查本地立绘；找不到图的节点立绘为 None（画占位框）。"""
        out = []
        for seg in chain:
            nodes = []
            for cond, name in seg:
                img = None
                hits = [
                    r for r in db.table("pokemon") if (r.get("name_zh") or "") == name
                ]
                if not hits:
                    hits = db.query("pokemon", name, mode="name")
                if hits:
                    path = db.find_image(db.pick_best(hits, name), self.images_dir)
                    if path:
                        img = self._evo_image(path)
                nodes.append((cond, name, img))
            out.append(nodes)
        return out

    def _evo_image(self, path):
        if path not in self._evo_imgs:
            try:
                img = Image.open(path).convert("RGBA")
                img.thumbnail((EVO_IMG, EVO_IMG))
                self._evo_imgs[path] = img
            except Exception:
                self._evo_imgs[path] = None
            if len(self._evo_imgs) > 512:
                self._evo_imgs.clear()
        return self._evo_imgs[path]

    def _fit_lines(self, meas, text, font, width, max_lines) -> list[str]:
        """换行并限制行数，超出时末行以省略号收尾。"""
        lines = self._wrap(meas, text, font, width)
        if len(lines) > max_lines:
            last = lines[max_lines - 1]
            while last and meas.textlength(last + "…", font=font) > width:
                last = last[:-1]
            lines = lines[: max_lines - 1] + [last + "…"]
        return lines or [""]

    def _layout_evolution(self, meas, chain, x0, x1):
        """进化链流式排版：立绘节点（名字在图下）+ 箭头（下方标进化条件）。

        每个分支段优先整段一行；段放不下时以"箭头+节点"为整体换行，箭头不悬空。
        返回（每行 (元素列表, 行高), 总高度），元素为 ("node", x, 名字行, 立绘)
        或 ("arrow", x, 条件行, None)。
        """
        small, cond_font = self._font(27), self._font(18)
        rows, cur, x = [], [], x0
        max_name, max_cond = 1, 0

        def flush():
            nonlocal cur, x, max_name, max_cond
            h = max(
                EVO_IMG + 6 + 32 * max_name,
                (EVO_IMG + 30) // 2 + 30 + 6 + 24 * max_cond if max_cond else 0,
            )
            rows.append((cur, h))
            cur, x, max_name, max_cond = [], x0, 1, 0

        for seg in chain:
            w = EVO_NODE_W * len(seg) + EVO_ARROW_W * (len(seg) - 1)
            if x > x0 and w <= x1 - x0 and x + w > x1:
                flush()
            for ni, (cond, name, img) in enumerate(seg):
                if ni == 0:
                    nl = self._fit_lines(meas, name, small, EVO_NODE_W, 2)
                    cur.append(("node", x, nl, img))
                    x += EVO_NODE_W
                    max_name = max(max_name, len(nl))
                else:
                    cl = (
                        self._fit_lines(meas, cond, cond_font, EVO_ARROW_W, 4)
                        if cond
                        else [""]
                    )
                    nl = self._fit_lines(meas, name, small, EVO_NODE_W, 2)
                    if x + EVO_ARROW_W + EVO_NODE_W > x1 and cur:
                        flush()
                    cur.append(("arrow", x, cl, None))
                    x += EVO_ARROW_W
                    cur.append(("node", x, nl, img))
                    x += EVO_NODE_W
                    max_name = max(max_name, len(nl))
                    max_cond = max(max_cond, len(cl))
        if cur:
            flush()
        total = sum(h for _, h in rows) + 16 * (len(rows) - 1) + 12  # 末行留底部空隙
        return rows, total

    def _draw_evolution(self, base, draw, rows, y, x_label) -> int:
        """绘制排版好的进化链，返回新的 y。base 是卡片底图（贴立绘用）。"""
        small, arrow_font, cond_font = self._font(27), self._font(30), self._font(18)
        draw.text((x_label, y + (EVO_IMG - 27) // 2), "进化", font=small, fill=SUBTEXT)
        x0 = x_label + 90
        for ri, (items, rh) in enumerate(rows):
            for kind, x, a, b in items:
                if kind == "node":
                    nl, img = a, b
                    if img is not None:
                        base.paste(
                            img,
                            (
                                x + (EVO_NODE_W - img.width) // 2,
                                y + (EVO_IMG - img.height) // 2,
                            ),
                            img,
                        )
                    else:
                        draw.rounded_rectangle(
                            [x + 8, y + 8, x + EVO_NODE_W - 8, y + EVO_IMG - 8],
                            radius=10,
                            fill=(238, 240, 244),
                        )
                        q = self._font(40)
                        tw = draw.textlength("?", font=q)
                        draw.text(
                            (x + (EVO_NODE_W - tw) / 2, y + (EVO_IMG - 40) // 2),
                            "?",
                            font=q,
                            fill=SUBTEXT,
                        )
                    for k, ln in enumerate(nl):
                        tw = draw.textlength(ln, font=small)
                        draw.text(
                            (x + (EVO_NODE_W - tw) / 2, y + EVO_IMG + 6 + k * 32),
                            ln,
                            font=small,
                            fill=TEXT,
                        )
                else:
                    cl = a
                    tw = draw.textlength("→", font=arrow_font)
                    draw.text(
                        (x + (EVO_ARROW_W - tw) / 2, y + (EVO_IMG - 30) / 2),
                        "→",
                        font=arrow_font,
                        fill=SUBTEXT,
                    )
                    for k, ln in enumerate(cl):
                        tw = draw.textlength(ln, font=cond_font)
                        draw.text(
                            (
                                x + (EVO_ARROW_W - tw) / 2,
                                y + (EVO_IMG + 30) // 2 + 6 + k * 24,
                            ),
                            ln,
                            font=cond_font,
                            fill=SUBTEXT,
                        )
            y += rh + (16 if ri < len(rows) - 1 else 0)
        return y + 12

    def _layout_dex(self, meas, gens, x0, x_end, max_lines: int = 16):
        """世代图鉴排版（参考图鉴类 App：左侧世代徽章、右侧条目文本）。

        每世代最多取 2 条（更多以"……"收尾），全块超过 max_lines 行截断。
        返回 [(世代标签, 徽章色, 文本行, 徽章宽), ...] 与总高。
        """
        bfont, tfont = self._font(22), self._font(24)
        badge_w = int(meas.textlength("第九世代", font=bfont)) + 24
        tw_x = x0 + badge_w + 12
        rows, used = [], 0
        for label, entries in gens:
            if used >= max_lines:
                break
            text = "；".join(entries[:2])
            if len(entries) > 2:
                text += "……"
            room = max_lines - used
            full = self._wrap(meas, text, tfont, x_end - tw_x)
            lines = (
                self._fit_lines(meas, text, tfont, x_end - tw_x, room)
                if len(full) > room
                else full
            )
            rows.append(
                (
                    label,
                    DEX_GEN_COLORS[DEX_GEN_ORDER.get(label[1], 1) - 1],
                    lines,
                    badge_w,
                )
            )
            used += len(lines)
        total = (
            sum(max(36, len(lines) * 34) for _, _, lines, _ in rows)
            + 10 * (len(rows) - 1)
            + 4
        )
        return rows, total

    def _draw_dex(self, draw, rows, y, x0) -> int:
        """绘制世代图鉴排版结果，返回新的 y。"""
        bfont, tfont = self._font(22), self._font(24)
        for ri, (label, color, lines, badge_w) in enumerate(rows):
            draw.rounded_rectangle(
                [x0, y + 1, x0 + badge_w, y + 35], radius=9, fill=color
            )
            tw = draw.textlength(label, font=bfont)
            draw.text(
                (x0 + (badge_w - tw) / 2, y + 6),
                label,
                font=bfont,
                fill=(255, 255, 255),
            )
            for k, ln in enumerate(lines):
                draw.text((x0 + badge_w + 12, y + k * 34), ln, font=tfont, fill=TEXT)
            y += max(36, len(lines) * 34) + (10 if ri < len(rows) - 1 else 0)
        return y + 4

    # ---------- 许愿星基拉祈 ----------
    WISHMAKER_COLS = (
        ("#", 44, 44),
        ("种子", 96, 88),
        ("PID", 192, 152),
        ("HP", 352, 58),
        ("攻", 410, 58),
        ("防", 468, 58),
        ("特攻", 526, 58),
        ("特防", 584, 58),
        ("速", 642, 58),
        ("总和", 712, 76),
        ("觉醒", 800, 128),
        ("异色", 936, 100),
    )

    def render_wishmaker_card(
        self,
        nature: str,
        iv_floor: int,
        count: int,
        rows: list[dict],
        tsv: int | None = None,
    ) -> bytes:
        """许愿星基拉祈筛选结果表格卡（命中的种子列表，最多 10 行）。"""
        W, PAD = 1080, 44
        title_f, sf = self._font(34), self._font(24)
        meas = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        IV_KEYS = ("hp", "atk", "def", "spa", "spd", "spe")
        n = len(rows)
        head_h, row_h = 46, 56
        H = PAD + 70 + head_h + 6 + row_h * n + 52 + PAD

        img = Image.new("RGBA", (W, H), BG)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([16, 16, W - 16, H - 16], radius=28, fill=CARD)

        draw.text(
            (PAD, PAD),
            f"许愿星基拉祈 · {nature or '任意'} · 个体≥{iv_floor}",
            font=title_f,
            fill=TEXT,
        )
        sub = f"命中 {count} 个种子 · 显示前 {n}"
        draw.text(
            (W - PAD - meas.textlength(sub, font=sf), PAD + 12),
            sub,
            font=sf,
            fill=SUBTEXT,
        )

        ty = PAD + 70
        draw.rounded_rectangle(
            [PAD, ty, W - PAD, ty + head_h], radius=10, fill=(238, 240, 244)
        )
        for lbl, x, w in self.WISHMAKER_COLS:
            tw = meas.textlength(lbl, font=sf)
            draw.text((x + (w - tw) / 2, ty + 11), lbl, font=sf, fill=SUBTEXT)

        def cell(x, w, text, color=TEXT):
            tw = meas.textlength(str(text), font=sf)
            draw.text((x + (w - tw) / 2, y + 15), str(text), font=sf, fill=color)

        cxw = [(x, w) for _, x, w in self.WISHMAKER_COLS]
        iv_cxw = cxw[3:9]
        y = ty + head_h + 6
        for i, r in enumerate(rows):
            if i % 2 == 1:
                draw.rectangle([PAD, y, W - PAD, y + row_h], fill=(246, 248, 251))
            cell(*cxw[0], i + 1)
            cell(*cxw[1], r["seed"])
            cell(*cxw[2], r["pid"])
            for (key, _, _, _), (cx, cw) in zip(
                [(k, None, None, None) for k in IV_KEYS], iv_cxw
            ):
                v = int(r[key])
                color = (
                    (226, 88, 88)
                    if v == 31
                    else (86, 168, 108)
                    if v >= 25
                    else (170, 170, 178)
                    if v < 10
                    else TEXT
                )
                cell(cx, cw, v, color)
            cell(*cxw[9], r["total"])
            cell(*cxw[10], f"{r['hp_type']} {r['hp_power']}")
            if tsv is None:
                cell(*cxw[11], "—", (190, 190, 196))
            elif r.get("shiny"):
                cell(*cxw[11], "异色", (226, 88, 88))
            else:
                cell(*cxw[11], "—", (190, 190, 196))
            y += row_h

        note = "种子 = 红/蓝宝石接收许愿星基拉祈时的 WishMaker 种子（16位十六进制）"
        if tsv is not None:
            note += f" · 异色按 TSV 0x{tsv:04X} 判定"
        draw.text((PAD, y + 14), note, font=sf, fill=SUBTEXT)

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True, compress_level=6)
        return buf.getvalue()

    # ---------- 卡片主体 ----------
    def render_pokemon_card(
        self,
        row: dict,
        stats: list[tuple[str, int]],
        total: int,
        image: Image.Image | None = None,
        evo_chain: list | None = None,
        shiny_image: Image.Image | None = None,
        ability_immune: list[tuple[str, str]] | None = None,
        champion: dict | None = None,
    ) -> bytes:
        W, PAD = 1080, 44
        name_font, mid_font, small_font, badge_font = (
            self._font(s) for s in (56, 32, 27, 26)
        )
        meas = ImageDraw.Draw(Image.new("RGB", (8, 8)))

        types = (row.get("types") or "").split("/")
        abilities = (
            (row.get("abilities") or "").split("、") if row.get("abilities") else []
        )
        hidden = (
            (row.get("hidden_abilities") or "").split("、")
            if row.get("hidden_abilities")
            else []
        )
        # 各形态种族值总和摘要（stats 字段含全部形态）
        stat_forms = [
            (m.group(1) or "基础", m.group(2))
            for m in (STAT_FORM_RE.finditer(row.get("stats") or ""))
        ]

        # 预排版：进化链（有立绘时用图片节点排版，否则退回文本行）
        evo_rows = None
        evo_h = 0
        if evo_chain and any(img for seg in evo_chain for _, _, img in seg):
            evo_rows, evo_h = self._layout_evolution(meas, evo_chain, PAD + 90, W - PAD)
        evo_lines = (
            []
            if evo_rows
            else self._wrap(
                meas, "进化：" + (row.get("evolution") or "无"), small_font, W - PAD * 2
            )
        )
        # 世代图鉴：按"第X世代"分组渲染（参考图鉴类 App 版式），无世代结构时回退整段文本
        dex_gens = _parse_dex_entries(row.get("dex_entries") or "")
        dex_rows, dex_h = (
            self._layout_dex(meas, dex_gens, PAD, W - PAD) if dex_gens else (None, 0)
        )
        if dex_rows:
            dex_lines = []
        else:
            dex_text = row.get("dex_entries") or ""
            if len(dex_text) > 70:
                dex_text = dex_text[:70] + "…"
            dex_lines = (
                self._wrap(meas, "说明：" + dex_text, small_font, W - PAD * 2)
                if row.get("dex_entries")
                else []
            )

        # 双栏几何：左列立绘/闪光/相性，右列基础信息/种族值
        ART = 380  # 立绘边长上限
        LW = ART  # 左列宽
        RW = W - PAD * 2 - LW - 24  # 右列宽
        X_L, X_R = PAD, PAD + LW + 24

        # 立绘/闪光预缩放（双栏高度预算与绘制共用）
        art_img = image.convert("RGBA") if image is not None else None
        if art_img is not None:
            art_img.thumbnail((ART, ART))
        shine_img = shiny_image.convert("RGBA") if shiny_image is not None else None
        if shine_img is not None:
            shine_img.thumbnail((SHINY, SHINY))

        # 预排版：弱点/抗性/免疫徽章（全宽，标签在左，超出宽度换行；
        # 每个徽章底色用对应属性色，条目形如「飞行×2」「地面」（免疫无倍率））
        matchup_rows = []
        for label, key in (
            ("弱点", "weak_to"),
            ("抗性", "resist_to"),
            ("免疫", "immune_to"),
        ):
            vals = [v for v in (row.get(key) or "").split("、") if v]
            if key == "immune_to" and ability_immune:
                # 受击类特性带来的条件免疫，如「电【避雷针】」
                vals += [f"{t}【{ab}】" for t, ab in ability_immune]
            if not vals:
                continue
            badges = [
                (
                    v,
                    TYPE_COLORS.get(
                        v.split("×", 1)[0].split("【", 1)[0], (120, 120, 120)
                    ),
                )
                for v in vals
            ]
            lines, cur, bx = [], [], PAD + 90
            for item in badges:
                bw = (
                    meas.textlength(item[0], font=badge_font) + 28 + 12
                )  # 徽章宽 + 间距
                if cur and bx + bw - 12 > W - PAD:
                    lines.append(cur)
                    cur, bx = [], PAD + 90
                cur.append(item)
                bx += bw
            if cur:
                lines.append(cur)
            matchup_rows.append((label, lines))

        # 预排版：右列基础信息行（特性等长文本最多 2 行）
        gender = (
            "无性别"
            if (row.get("gender_ratio") or "") == "♂0% ♀0%"
            else (row.get("gender_ratio") or "")
        )
        info_rows = []
        for label, val in (
            ("分类", row.get("category")),
            ("身高", row.get("height")),
            ("体重", row.get("weight")),
            ("捕获度", row.get("catch_rate")),
            ("性别比例", gender),
        ):
            if val:
                info_rows.append(
                    self._fit_lines(meas, f"{label}：{val}", small_font, RW, 2)
                )
        for label, vals in (("特性", abilities), ("隐藏特性", hidden)):
            vals = [v for v in vals if v]
            if vals:
                info_rows.append(
                    self._fit_lines(
                        meas, f"{label}：{'、'.join(vals)}", small_font, RW, 2
                    )
                )

        # 双栏高度与整卡高度预算（冠军赛名次条占 52px）
        champ_h = 52 if champion else 0
        left_h = art_img.height if art_img is not None else 0
        if shine_img is not None:
            left_h += (12 if left_h else 0) + shine_img.height + 26  # 含"闪光"标注
        right_h = sum(46 + 34 * (len(ls) - 1) for ls in info_rows) + 12
        if len(stat_forms) > 1:
            right_h += 40  # 形态总和摘要行
        right_h += len(stats) * 52 + (52 if total else 0) + 8

        y = PAD + 120 + champ_h  # 头部（名字/副标题/属性徽章）+ 冠军赛条
        y += max(left_h, right_h)  # 双栏内容
        y += 60  # 分隔线
        y += 18 + sum(58 * len(lines) for _, lines in matchup_rows)  # 弱点/抗性（全宽）
        if evo_rows:
            y += 46 + evo_h  # 进化链（图片节点，按排版后行数）
        elif evo_lines:
            y += 46 + len(evo_lines) * 38
        if dex_rows:
            y += 16 + dex_h  # 世代图鉴（按排版后实际高度）
        elif dex_lines:
            y += 16 + 46 + len(dex_lines) * 38
        H = y + PAD

        img = Image.new("RGBA", (W, H), BG)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([16, 16, W - 16, H - 16], radius=28, fill=CARD)

        # 头部：名字在左，属性徽章右置（放不下时落到副标题行右侧）
        x = PAD
        gen = row.get("generation") or ""
        disp = row.get("name_zh", "")
        if row.get("form_name"):
            disp += f"（{row['form_name']}）"
        name_txt = f"#{row.get('id_raw', '')}  {disp}"
        draw.text((x, PAD), name_txt, font=name_font, fill=TEXT)
        sub = ""
        if gen:
            sub = f"第{gen}世代 · {row.get('name_en', '')}"
            if row.get("name_ja"):
                sub += f" · {row['name_ja']}"
            draw.text((x + 6, PAD + 76), sub, font=small_font, fill=SUBTEXT)
        # 属性徽章：紧挨名字右侧（名字过长时落到副标题行右侧）
        bl = [t.strip() for t in types if t.strip()]
        if bl:
            tw_sum = sum(meas.textlength(t, font=badge_font) + 28 + 12 for t in bl) - 12
            name_w = meas.textlength(name_txt, font=name_font)
            by = PAD + 14 if tw_sum <= W - PAD * 2 - name_w - 30 else PAD + 74
            bx = W - PAD - tw_sum
            if PAD + name_w + 24 + tw_sum <= W - PAD:
                bx, by = PAD + name_w + 24, PAD + 14
            else:
                bx = (
                    PAD + 6 + (meas.textlength(sub, font=small_font) if sub else 0) + 24
                )
                by = PAD + 74
            for t in bl:
                bx = self._badge(
                    draw, (bx, by), t, TYPE_COLORS.get(t, (120, 120, 120)), badge_font
                )
        y = PAD + 120
        # 冠军赛名次条：标签 + 单/双打名次徽章 + 赛季与榜单规模
        if champion:
            cy = PAD + 116
            draw.text((PAD, cy + 6), "冠军赛", font=small_font, fill=SUBTEXT)
            bx = PAD + 90
            for label, key, color in (
                ("单打", "single", (79, 70, 229)),
                ("双打", "double", (219, 39, 119)),
            ):
                rank = champion.get(key)
                if not rank:
                    continue
                bx = self._badge(draw, (bx, cy), f"{label} #{rank}", color, badge_font)
            note = f"{champion.get('season', '')} · 共 {champion.get('total', '?')} 只"
            nw = draw.textlength(note, font=small_font)
            draw.text((W - PAD - nw, cy + 6), note, font=small_font, fill=SUBTEXT)
        y += champ_h  # 名次条占位，双栏从其下开始
        cols_top = y

        # 左列：立绘 → 闪光
        yl = cols_top
        if art_img is not None:
            img.paste(art_img, (X_L, yl), art_img)
            yl += art_img.height
        if shine_img is not None:
            if yl > cols_top:
                yl += 12
            sx = X_L + (LW - shine_img.width) // 2
            img.paste(shine_img, (sx, yl), shine_img)
            cap_font = self._font(20)
            tw = draw.textlength("闪光", font=cap_font)
            draw.text(
                (sx + (shine_img.width - tw) / 2, yl + shine_img.height + 2),
                "闪光",
                font=cap_font,
                fill=SUBTEXT,
            )
            yl += shine_img.height + 26

        # 右列：基础信息 → 种族值条（含总和行）
        yr = cols_top
        for lines in info_rows:
            for i, ln in enumerate(lines):
                draw.text((X_R, yr + i * 34), ln, font=small_font, fill=TEXT)
            yr += 46 + 34 * (len(lines) - 1)
        yr += 12
        if len(stat_forms) > 1:
            summary = "种族值形态：" + " · ".join(f"{n} {t}" for n, t in stat_forms)
            draw.text((X_R, yr), summary, font=small_font, fill=SUBTEXT)
            yr += 40
        bar_x, bar_w = X_R + 124, RW - 280
        for label, val in stats:
            draw.text((X_R, yr), label, font=small_font, fill=TEXT)
            draw.text((X_R + 64, yr), str(val), font=small_font, fill=TEXT)
            draw.rounded_rectangle(
                [bar_x, yr + 4, bar_x + bar_w, yr + 30], radius=8, fill=(238, 240, 244)
            )
            ratio = min(val / 180, 1.0)
            color = STAT_COLORS.get(label, (120, 120, 120))
            if ratio > 0:
                draw.rounded_rectangle(
                    [bar_x, yr + 4, bar_x + max(int(bar_w * ratio), 16), yr + 30],
                    radius=8,
                    fill=color,
                )
            yr += 52
        if total:  # 与属性行同构：标签+数值+条形，总和按 720（种族值上限）缩放
            draw.text((X_R, yr), "总和", font=small_font, fill=TEXT)
            draw.text((X_R + 64, yr), str(total), font=small_font, fill=TEXT)
            draw.rounded_rectangle(
                [bar_x, yr + 4, bar_x + bar_w, yr + 30], radius=8, fill=(238, 240, 244)
            )
            ratio = min(total / 720, 1.0)
            if ratio > 0:
                draw.rounded_rectangle(
                    [bar_x, yr + 4, bar_x + max(int(bar_w * ratio), 16), yr + 30],
                    radius=8,
                    fill=(120, 120, 120),
                )
            yr += 52
        yr += 8

        y = cols_top + max(left_h, right_h)

        # 弱点 / 抗性（全宽：标签在左，徽章自 PAD+90 起排、整卡宽度换行，底色为属性色）
        draw.line([PAD, y, W - PAD, y], fill=LINE, width=2)
        y += 18
        for label, lines in matchup_rows:
            for i, line in enumerate(lines):
                if i == 0:
                    draw.text((PAD, y), label, font=small_font, fill=SUBTEXT)
                bx = PAD + 90
                for v, fill in line:
                    bx = self._badge(draw, (bx, y - 4), v, fill, badge_font)
                y += 58

        # 进化 / 图鉴文本
        def block(lines, title_skip=True):
            nonlocal y
            for ln in lines:
                draw.text(
                    (PAD, y), ln, font=small_font, fill=SUBTEXT if title_skip else TEXT
                )
                title_skip = False
                y += 38

        if evo_rows:
            draw.line([PAD, y, W - PAD, y], fill=LINE, width=2)
            y += 16
            y = self._draw_evolution(img, draw, evo_rows, y, PAD)
        elif evo_lines:
            draw.line([PAD, y, W - PAD, y], fill=LINE, width=2)
            y += 16
            block(evo_lines)
        if dex_rows:
            draw.line([PAD, y, W - PAD, y], fill=LINE, width=2)
            y += 16
            y = self._draw_dex(draw, dex_rows, y, PAD)
        elif dex_lines:
            draw.line([PAD, y, W - PAD, y], fill=LINE, width=2)
            y += 16
            block(dex_lines)

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True, compress_level=6)
        return buf.getvalue()

    async def render_pokemon(
        self,
        row: dict,
        db,
        image_bytes: bytes | None = None,
        champion: dict | None = None,
    ) -> bytes | None:
        """组装种族值与进化链立绘并渲染卡片；失败返回 None（调用方回退文本）。"""
        try:
            parsed = db.first_stats(row)
            stats, total = parsed if parsed else ([], 0)
            art = None
            if image_bytes:
                import io as _io

                art = Image.open(_io.BytesIO(image_bytes))
            elif self.images_dir:
                path = db.find_image(row, self.images_dir)
                if path:
                    art = Image.open(path)
            evo_chain = None
            shiny = None
            if self.images_dir:
                spath = db.find_image(row, self.images_dir, shiny=True)
                if spath:
                    shiny = Image.open(spath)
                chain = self._evolution_chain(row, db)
                if chain:
                    chain = self._resolve_evo_images(chain, db)
                    if any(img for seg in chain for _, _, img in seg):
                        evo_chain = chain
            return self.render_pokemon_card(
                row,
                stats,
                total,
                art,
                evo_chain,
                shiny,
                db.ability_immunities(row),
                champion,
            )
        except Exception as e:
            logger.warning(f"卡片渲染失败: {e}")
            return None
