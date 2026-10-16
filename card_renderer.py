"""Pillow 图片卡片渲染（参考 astrbot_plugin_bazaar 的实现方式：纯 Pillow 绘制，
不依赖浏览器渲染服务）。"""
import io
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    from astrbot.api import logger
except ImportError:  # 脱离 AstrBot 环境本地测试时使用
    import logging
    logger = logging.getLogger(__name__)

FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",                            # macOS
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",                # Debian/Ubuntu
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",             # Arch/Fedora
    "C:/Windows/Fonts/msyh.ttc",                                     # Windows
    "C:/Windows/Fonts/simhei.ttf",
]

TYPE_COLORS = {
    "一般": (154, 161, 159), "火": (230, 40, 40), "水": (41, 128, 239),
    "草": (63, 161, 41), "电": (250, 192, 0), "冰": (61, 206, 243),
    "格斗": (255, 128, 0), "毒": (145, 65, 203), "地面": (145, 81, 33),
    "飞行": (129, 185, 239), "超能力": (239, 65, 121), "虫": (145, 161, 25),
    "岩石": (175, 169, 129), "幽灵": (112, 65, 112), "龙": (80, 96, 225),
    "恶": (98, 77, 78), "钢": (161, 161, 161), "妖精": (239, 112, 239),
}
STAT_COLORS = {
    "HP": (255, 89, 94), "攻击": (255, 144, 60), "防御": (255, 202, 58),
    "特攻": (74, 144, 226), "特防": (67, 200, 144), "速度": (180, 120, 255),
}
BG = (250, 250, 252)
CARD = (255, 255, 255)
TEXT = (45, 45, 48)
SUBTEXT = (120, 122, 128)
LINE = (228, 230, 235)

# 从 stats 文本中提取各形态种族值总和，如 "超级喷火龙Ｘ形态：HP78/…（总和634）"
STAT_FORM_RE = re.compile(r"(?:([\w]+?)形态：)?HP\d+/攻击\d+/防御\d+/特攻\d+/特防\d+/速度\d+（总和(\d+)）")


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

    # ---------- 卡片主体 ----------
    def render_pokemon_card(self, row: dict, stats: list[tuple[str, int]],
                            total: int, image: Image.Image | None = None) -> bytes:
        W, PAD = 1080, 44
        name_font, mid_font, small_font, badge_font = (self._font(s) for s in (56, 32, 27, 26))
        meas = ImageDraw.Draw(Image.new("RGB", (8, 8)))

        types = (row.get("types") or "").split("/")
        abilities = (row.get("abilities") or "").split("、") if row.get("abilities") else []
        hidden = (row.get("hidden_abilities") or "").split("、") if row.get("hidden_abilities") else []
        # 各形态种族值总和摘要（stats 字段含全部形态）
        stat_forms = [(m.group(1) or "基础", m.group(2)) for m in
                      (STAT_FORM_RE.finditer(row.get("stats") or ""))]

        # 预排版：换行文本
        evo_lines = self._wrap(meas, "进化：" + (row.get("evolution") or "无"), small_font, W - PAD * 2)
        intro = (row.get("description") or "").strip()
        if len(intro) > 90:
            intro = intro[:90] + "…"
        intro_lines = self._wrap(meas, "图鉴：" + intro, small_font, W - PAD * 2)
        dex_text = (row.get("dex_entries") or "")
        if len(dex_text) > 70:
            dex_text = dex_text[:70] + "…"
        dex_lines = self._wrap(meas, "说明：" + dex_text,
                               small_font, W - PAD * 2) if row.get("dex_entries") else []

        # 高度预算
        ART = 380
        y = PAD + 84 + 56                     # 头部（编号+名字+世代）
        y += 56                               # 属性徽章
        y += 24 + 46                          # 分类/身高/体重
        y += (46 * len(abilities)) + (46 if hidden else 0) + 12
        if len(stat_forms) > 1:
            y += 40                           # 形态总和摘要行
        y += len(stats) * 52 + 16             # 种族值
        y += 60                               # 分隔线
        y += 46 + 58                          # 弱点/抗性
        if evo_lines:
            y += 46 + len(evo_lines) * 38
        if intro_lines:
            y += 16 + 46 + len(intro_lines) * 38
        if dex_lines:
            y += 16 + 46 + len(dex_lines) * 38
        H = y + PAD
        left_w = W - PAD * 2 - ART - 24

        img = Image.new("RGBA", (W, H), BG)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([16, 16, W - 16, H - 16], radius=28, fill=CARD)

        # 立绘（右侧）
        if image is not None:
            art = image.convert("RGBA")
            art.thumbnail((ART, ART))
            img.paste(art, (W - PAD - art.width, PAD + 20), art)

        # 头部
        x = PAD
        gen = row.get("generation") or ""
        disp = row.get("name_zh", "")
        if row.get("form_name"):
            disp += f"（{row['form_name']}）"
        draw.text((x, PAD), f"#{row.get('id_raw', '')}  {disp}",
                  font=name_font, fill=TEXT)
        if gen:
            draw.text((x + 6, PAD + 76), f"第{gen}世代 · {row.get('name_en', '')}",
                      font=small_font, fill=SUBTEXT)
        y = PAD + 116

        # 属性徽章
        x = PAD
        for t in types:
            if t.strip():
                x = self._badge(draw, (x, y), t.strip(), TYPE_COLORS.get(t.strip(), (120, 120, 120)), badge_font)
        y += 56

        # 基础信息
        for label, val in (("分类", row.get("category")), ("身高", row.get("height")),
                           ("体重", row.get("weight"))):
            if val:
                draw.text((x, y), f"{label}：{val}", font=small_font, fill=TEXT)
                y += 46
        for label, vals in (("特性", abilities), ("隐藏特性", hidden)):
            vals = [v for v in vals if v]
            if vals:
                draw.text((x, y), f"{label}：{'、'.join(vals)}", font=small_font, fill=TEXT)
                y += 46
        y += 12

        # 种族值条
        if len(stat_forms) > 1:
            summary = "种族值形态：" + " · ".join(f"{n} {t}" for n, t in stat_forms)
            draw.text((x, y), summary, font=small_font, fill=SUBTEXT)
            y += 40
        bar_x, bar_w = x + 124, left_w - 280
        for label, val in stats:
            draw.text((x, y), label, font=small_font, fill=TEXT)
            draw.text((x + 64, y), str(val), font=small_font, fill=TEXT)
            draw.rounded_rectangle([bar_x, y + 4, bar_x + bar_w, y + 30], radius=8, fill=(238, 240, 244))
            ratio = min(val / 180, 1.0)
            color = STAT_COLORS.get(label, (120, 120, 120))
            if ratio > 0:
                draw.rounded_rectangle([bar_x, y + 4, bar_x + max(int(bar_w * ratio), 16), y + 30],
                                       radius=8, fill=color)
            y += 52
        if total:
            draw.text((bar_x + bar_w + 12, y - 52 + 4), f"总和 {total}", font=small_font, fill=SUBTEXT)
        y += 8

        # 弱点 / 抗性
        draw.line([PAD, y, W - PAD, y], fill=LINE, width=2)
        y += 18
        for label, key, color in (("弱点", "weak_to", (226, 88, 88)),
                                  ("抗性", "resist_to", (86, 168, 108)),
                                  ("免疫", "immune_to", (130, 130, 138))):
            vals = [v for v in (row.get(key) or "").split("、") if v]
            if not vals:
                continue
            draw.text((PAD, y), label, font=small_font, fill=SUBTEXT)
            bx = PAD + 90
            for v in vals[:8]:
                bx = self._badge(draw, (bx, y - 4), v, color, badge_font)
            y += 58

        # 进化 / 图鉴文本
        def block(lines, title_skip=True):
            nonlocal y
            for ln in lines:
                draw.text((PAD, y), ln, font=small_font,
                          fill=SUBTEXT if title_skip else TEXT)
                title_skip = False
                y += 38

        if evo_lines:
            draw.line([PAD, y, W - PAD, y], fill=LINE, width=2)
            y += 16
            block(evo_lines)
        if intro_lines:
            draw.line([PAD, y, W - PAD, y], fill=LINE, width=2)
            y += 16
            block(intro_lines)
        if dex_lines:
            draw.line([PAD, y, W - PAD, y], fill=LINE, width=2)
            y += 16
            block(dex_lines)

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True, compress_level=6)
        return buf.getvalue()

    async def render_pokemon(self, row: dict, db, image_bytes: bytes | None = None) -> bytes | None:
        """组装种族值并渲染卡片；失败返回 None（调用方回退文本）。"""
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
            return self.render_pokemon_card(row, stats, total, art)
        except Exception as e:
            logger.warning(f"卡片渲染失败: {e}")
            return None
