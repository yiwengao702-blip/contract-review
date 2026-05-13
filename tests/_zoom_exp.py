# 文件位置: contract-review/tests/_zoom_exp.py
# 功能说明: 临时实验脚本——验证「放大倍率」对真实手机拍合同的识别影响。
#          背景：现行策略为「短边 < 1200 则放大到 1200，但上限 1.5x」，
#          该上限是在 1500px 宽、字号 34 的合成图上测出来的（那批图放大反而更差）。
#          真实手机照片是 539x740 的整页小字，1.5x 只有 808px，恐严重不足。
#          本脚本用同一张真实照片跑「缩放 x psm」矩阵，用关键片段命中率量化。
#          跑完即删。
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytesseract  # noqa: E402
from PIL import Image, ImageDraw, ImageFont, ImageOps  # noqa: E402

from services.ocr_service import TesseractProvider  # noqa: E402

NL = chr(10)
EXE = TesseractProvider._resolve_exe()
CFG = TesseractProvider._tess_config()
pytesseract.pytesseract.tesseract_cmd = EXE

SRC = pathlib.Path(
    r"D:\weixinwenjian\xwechat_files\wxid_y8jdybkvb8hs22_ca65\temp\RWTemp\2026-09"
    r"\439e93957619b7871e3e3cebc5e8f402\00a487997de11037d0b0d0146eb40f79.png"
)
WORK = ROOT / "data" / "uploads" / "_zoom_exp.png"
WORK.parent.mkdir(parents=True, exist_ok=True)
with Image.open(SRC) as im:
    ImageOps.exif_transpose(im).convert("RGB").save(WORK)

# 这张真实合同上「必然存在」的关键片段——命中越多说明识别越准
KEYS_CN = ["委托", "受托", "合同", "服务", "费用", "奥迪", "汽车"]
# 闸门判定用的结构词（过了这两关才可能进入条款审查）
KEYS_GATE = ["委托方", "受托方", "甲方", "乙方"]


def run(img: Image.Image, psm: str, lang: str = "chi_sim") -> tuple[int, int, str, float]:
    t0 = time.time()
    txt = pytesseract.image_to_string(
        img, lang=lang, config=f"{CFG} --psm {psm}".strip()
    ).strip()
    dt = time.time() - t0
    return (sum(1 for k in KEYS_CN if k in txt),
            sum(1 for k in KEYS_GATE if k in txt), txt, dt)


def grey(im: Image.Image) -> Image.Image:
    return im.convert("L")


def scaled(im: Image.Image, factor: float) -> Image.Image:
    if factor == 1.0:
        return im
    return im.resize((int(im.width * factor), int(im.height * factor)), Image.LANCZOS)


with Image.open(WORK) as im:
    base = im.convert("RGB")
    W0, H0 = base.size
    print(f"原图 {W0}x{H0}（短边 {min(W0, H0)}）")
    print(f"tessdata: {CFG!r}")
    print()

    print("=== 矩阵：缩放倍率 x psm（lang=chi_sim）===")
    print(f"{'缩放':>6} {'psm':>5} {'尺寸':>12} {'CN命中':>7} {'闸门词':>7} {'用时':>7}")
    print("-" * 56)
    best = None
    for psm in ["6", "3", "4"]:
        for factor in [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]:
            img = grey(scaled(base, factor))
            cn, gate, txt, dt = run(img, psm)
            print(f"{factor:>6.1f} {psm:>5} {img.width:>5}x{img.height:<6} {cn:>5}/7 {gate:>5}/4 {dt:>6.1f}s")
            score = (cn, gate)
            if best is None or score > best[0]:
                best = (score, factor, psm, txt)
        print()

    print("=== 最佳组合 ===")
    (cn, gate), factor, psm, txt = best
    print(f"缩放 {factor}x + psm {psm} -> CN {cn}/7, 闸门词 {gate}/4")
    print("--- 输出 ---")
    for ln in txt.split(NL):
        if ln.strip():
            print("  ", repr(ln))
