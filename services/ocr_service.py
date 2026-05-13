# 文件位置: contract-review/services/ocr_service.py
# 功能说明: 拍照 / 图片识别服务（可插拔适配层 + 明确降级）。
#
# 设计要点（为什么这样拆）：
#   ① 把"从图片里取文本"与具体引擎解耦：各引擎以 OCRProvider 子类接入，
#      借助 __init_subclass__ 自动注册（与 core/node_base.py 同一套注册约定），
#      按 priority 依次尝试，新增引擎无需改动调用方。
#   ② 引擎缺失不是异常，而是"可解释的降级事件"：
#      probe() 汇报每个引擎的可用性与原因；recognize() 在全不可用时
#      返回带 reason/hint 的失败结果，交由前端引导用户人工录入，
#      绝不静默进入 blocked（旧实现就是静默返回空文本，用户只看到"解析受阻"）。
#   ③ 识别前先做图片体检（分辨率/清晰度/亮度/对比度），
#      把"重拍建议"在识别之前就给出去，而不是等识别失败才说图片有问题。
#
#   ④ 支持一次提交多张照片（一份纸质合同的多页）：
#      逐页识别后按页码顺序合并，页与页之间用 PAGE_SEPARATOR 分隔，
#      使"哪一页没认出来"可精确定位、"第 N 页"的风险定位仍指向真实页码。
#
# 内置适配（按尝试顺序）：
#   tesseract  本地 OCR 引擎（pytesseract + Tesseract-OCR 程序 + chi_sim 中文包）
#   vlm        多模态大模型识图（复用 config 的 OpenAI 兼容配置）
#   cloud      通用云 OCR HTTP 接口（config 的 OCR_API_URL / OCR_API_KEY）
#   manual     人工录入兜底（永远"可用"，只用于给出降级说明，不产出文本）
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Dict, List, Optional, Tuple, Type

from config import cfg

# 支持的图片扩展名（与前端 accept 属性保持一致）
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}

# 多页合并时使用的分页符。
# 为什么偏偏是 "\n\f"：解析器对 .txt 正文就是按这个序列切页的
# （见 doc_parser.parse 的 .txt/.md 分支）。多张照片识别出的正文最终写成 .txt 交给解析器，
# 沿用同一约定就能让"第 3 页的风险"仍然报第 3 页 —— 否则一份 3 页的合同会被整体当成 1 页。
PAGE_SEPARATOR = "\n\f\n"

# Windows 上 Tesseract 的常见安装位置：装了程序但没进 PATH 的情况很常见，主动探测一遍
TESSERACT_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"C:\Users\%USERNAME%\AppData\Local\Tesseract-OCR\tesseract.exe",
]
# 用户级语言包目录：Program Files 下的 tessdata 需要管理员权限才能写，
# 而中文包 chi_sim 常常不在安装包默认选项里、得事后下载 —— 下载了却写不进 Program Files。
# 引擎会优先使用这个目录（若它存在），从而免去管理员权限。
USER_TESSDATA_DIR = Path.home() / ".workbuddy" / "tools" / "tesseract" / "tessdata"

# 各引擎不可用时的修复指引：直接展示给用户，避免出现"不可用但不知怎么办"
INSTALL_HINTS = {
    "tesseract": "pip install pytesseract 并安装 Tesseract-OCR 程序（勾选中文语言包 chi_sim）",
    "vlm": "在 config.py 中设 LLM_PROVIDER=\"openai\" 并填写 LLM_API_KEY / LLM_BASE_URL（模型需支持图片输入）",
    "cloud": "在 config.py 中填写 OCR_API_URL 与 OCR_API_KEY（通用云 OCR 的 HTTP 接口）",
}


# ---------------------------------------------------------------------- #
# 数据结构
# ---------------------------------------------------------------------- #
@dataclass
class ImageQuality:
    """图片体检结果：把"能不能识别"的问题提前可视化为可操作建议。"""

    width: int = 0
    height: int = 0
    sharpness: float = 0.0    # 拉普拉斯方差，越大越清晰
    brightness: float = 0.0   # 灰度均值 0-255
    contrast: float = 0.0     # 灰度标准差
    highlight_ratio: float = 0.0  # 近饱和（>250）像素占比，用于判断真实过曝
    warnings: List[str] = field(default_factory=list)

    @property
    def short_side(self) -> int:
        return min(self.width, self.height) if self.width and self.height else 0

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "short_side": self.short_side,
            "sharpness": round(self.sharpness, 1),
            "brightness": round(self.brightness, 1),
            "contrast": round(self.contrast, 1),
            "highlight_ratio": round(self.highlight_ratio, 3),
            "warnings": self.warnings,
        }


@dataclass
class OCRResult:
    """识别结果：成功与否都返回本结构，用 ok 区分，并带上完整尝试链路。"""

    ok: bool
    text: str = ""
    provider: str = ""
    provider_label: str = ""
    reason: str = ""                                   # 失败/降级原因（面向用户，可直接展示）
    hint: str = ""                                     # 修复指引
    quality: ImageQuality = field(default_factory=ImageQuality)
    attempts: List[dict] = field(default_factory=list)  # 各引擎尝试记录，用于"为什么没识别出来"的可解释性

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "text": self.text,
            "chars": len(self.text),
            "provider": self.provider,
            "provider_label": self.provider_label,
            "reason": self.reason,
            "hint": self.hint,
            "quality": self.quality.to_dict(),
            "attempts": self.attempts,
        }


@dataclass
class PageOCR:
    """单页识别结果：页码 + 文件信息 + OCR 结果，是"哪一页出了问题"的最小定位单元。

    多张照片不是"多份合同"，而是同一份合同的多页，所以页码必须是一等公民：
    只有它才能支撑"第 2 页没认出来，请重拍第 2 页"和"按页码顺序合并正文"。
    """

    index: int          # 页码，从 1 开始
    filename: str
    bytes: int
    result: OCRResult

    @property
    def has_text(self) -> bool:
        return bool(self.result.text.strip())

    @property
    def ok(self) -> bool:
        """该页是否拿到了可用正文（引擎识别成功且非空）。"""
        return self.result.ok and self.has_text

    def to_dict(self) -> dict:
        d = self.result.to_dict()
        d.update({"index": self.index, "filename": self.filename, "bytes": self.bytes})
        return d


def assemble_batch(pages: List[PageOCR]) -> dict:
    """把逐页识别结果汇总成「可直接提交的正文 + 可解释的整批状态」。

    汇总口径：
      · text         —— 按页码顺序用 PAGE_SEPARATOR 连接；没认出来的页留空位
                        （不贡献文字但保留页次），使「第 N 页」= 「第 N 张照片」；
      · failed_pages —— 没拿到文字的页码，前端据此精确提示"第 2 页请人工补录"；
      · 单页时额外把该页的 provider/quality/attempts 等提到顶层，
        使只有一张照片的响应形状与旧版完全一致（老调用方无感）。
    """
    ok_pages = [p for p in pages if p.ok]
    failed_pages = [p.index for p in pages if not p.ok]
    # 页码保位：没认出来的页不直接丢弃，而是留一个空的分页位。
    # 丢弃会让后续页的页码整体前移 —— 用户照着报告里的「第 N 页」去翻
    # 自己拍的第 N 张照片时会对不上号。保留空位后，
    # 「第 N 页」始终等于「第 N 张照片」，空页本身不贡献任何文字。
    text = PAGE_SEPARATOR.join(p.result.text.strip() if p.ok else "" for p in pages)

    payload = {
        "ok": bool(ok_pages) and not failed_pages,
        "text": text,
        "chars": len(text),
        "merged": len(pages) > 1,
        "total_pages": len(pages),
        "ok_pages": len(ok_pages),
        "failed_pages": failed_pages,
        "pages": [p.to_dict() for p in pages],
        "filename": pages[0].filename if len(pages) == 1 else f"{len(pages)} 页照片",
        "bytes": sum(p.bytes for p in pages),
    }

    if len(pages) == 1:
        # 单页：把该页的明细字段原样提到顶层，保持与旧版响应兼容
        detail = pages[0].result.to_dict()
        for key in ("provider", "provider_label", "reason", "hint", "quality", "attempts"):
            payload[key] = detail[key]
    else:
        # 多页：顶层 reason/hint 描述的是"整批"的缺口，而不是某一页的
        payload["reason"], payload["hint"] = _batch_degrade(pages, failed_pages)

    return payload


def _batch_degrade(pages: List[PageOCR], failed_pages: List[int]) -> Tuple[str, str]:
    """把"哪几页没认出来"翻译成一句人话 + 一条出路。"""
    if not failed_pages:
        return "", ""
    whose = "、".join(f"第 {i} 页" for i in failed_pages)
    done = len(pages) - len(failed_pages)
    return (f"{whose}未能识别出文字，其余 {done} 页已识别完成。",
            f"可对{whose}重拍后单独重试，或直接在对应页的校对框里人工录入正文；"
            "确实为空白页的，留空即可，不会参与审查。")


# ---------------------------------------------------------------------- #
# 引擎适配基类
# ---------------------------------------------------------------------- #
class OCRProvider:
    """OCR 引擎适配基类。

    子类约定：
      · provider_name  唯一键，同时是自动注册键；
      · provider_label 面向用户的引擎名；
      · priority       尝试顺序（越小越先）；
      · probe()        类方法，返回 (是否可用, 原因/说明)；
      · recognize()    实例方法，返回识别文本（不抛异常给调用方，异常由服务层记录）。
    """

    _registry: ClassVar[Dict[str, Type["OCRProvider"]]] = {}

    provider_name: str = ""
    provider_label: str = ""
    priority: int = 100

    def __init_subclass__(cls, **kwargs) -> None:
        """子类定义时自动登记，避免维护工厂分支。"""
        super().__init_subclass__(**kwargs)
        name = getattr(cls, "provider_name", "")
        if name:
            OCRProvider._registry[name] = cls

    @classmethod
    def all_providers(cls) -> List[Type["OCRProvider"]]:
        """按优先级返回全部已注册引擎。"""
        return sorted(cls._registry.values(), key=lambda c: (c.priority, c.provider_name))

    @classmethod
    def probe(cls, force: bool = False) -> Tuple[bool, str]:
        """探测本引擎是否可用。

        force=True 时无视缓存重新探测 —— 界面上的「重新检测」按钮走的就是这条路：
        用户刚装完引擎/语言包，应当能立刻看到结果，而不是必须重启服务。
        """
        raise NotImplementedError

    def recognize(self, image_path: Path) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------- #
# 引擎一：本地 Tesseract
# ---------------------------------------------------------------------- #
class TesseractProvider(OCRProvider):
    """本地 Tesseract OCR：离线、无外部调用，适合内网部署场景。"""

    provider_name = "tesseract"
    provider_label = "Tesseract 本地 OCR"
    priority = 10

    # 页面分割模式：6 = 假设整图是"一个统一的文本块"。
    # 实测（同一张合成合同图，三档分辨率共 24 个关键片段）：
    #   psm 6 → 24/24，psm 3（默认）→ 22/24，psm 4 → 22/24，psm 11 → 20/24，psm 12 → 19/24。
    # 合同正文绝大多数是单栏竖排，明确告诉引擎"就一块文字"反而更准。
    # 风险是表格/多栏版面会被当成一整块而漏内容，所以下面配了 psm 3 的回退。
    PSM_PRIMARY = 6
    PSM_FALLBACK = 3
    # 低于这个字数就认为"版面假设不对"，换 psm 3 重跑一次
    FALLBACK_MIN_CHARS = 20

    # 语言包：默认**只用** chi_sim，不加 eng。这条是实测踩出来的，改动前请先看下面的数据。
    #
    # 直觉上"chi_sim+eng 能同时认中英文，应该更好"，实测恰恰相反：
    #   同一张含「甲方：北京星辰科技有限公司」的合同图，
    #   chi_sim+eng 稳定把「甲方」认成 FA / FAA（中文被英文模型抢答），
    #   而甲乙方是元数据抽取与规则判定的基石字段，认错会直接产出
    #   「缺失甲方」的**高风险假阳性**——不是小瑕疵。
    #   纯 chi_sim 在中文 6/6 的同时，对 PURCHASE CONTRACT、USD 1,200,000、
    #   Beijing Xingchen Technology Co., Ltd. 这类英文同样 6/6（只丢了个句点）。
    # 代价只是英文标点略逊，收益是中文关键字段不再被误伤，取舍很清楚。
    #
    # 但纯 chi_sim 遇到**非中文文档**（如纯英文合同）会明显退化，所以保留一次兜底：
    # 当主结果里汉字太少（见 FALLBACK_MIN_HANZI）时，再用 chi_sim+eng 重跑并择优。
    LANG_PRIMARY = "chi_sim"
    LANG_FALLBACK = "chi_sim+eng"
    # 汉字少于这个数就认为"这张图可能不是中文文档"，换混合语言包重试
    FALLBACK_MIN_HANZI = 20

    # ---- 探测结果缓存 ----
    # 缓存形态：(采样时刻 monotonic, 是否可用, 原因)。
    #
    # 为什么"可用"与"不可用"的缓存寿命必须不一样（这是线上踩出来的）：
    #   装了 pytesseract / Tesseract 的**当时**就把服务开着是最常见的情形
    #   （安装要下载几十 MB，没人会先关服务再装）。若把那次失败的探测结果
    #   在进程里永久缓存，则引擎装完之后：页面依旧写着"本机未安装可用的 OCR 引擎"，
    #   连点「重新检测」也没用，只能重启服务 —— 用户看到的是"我明明装好了，你却说没装"。
    #   所以：**可用结果永久缓存**（引擎不会在服务运行期内被卸载，重探只是白起子进程），
    #   **不可用结果只信 PROBE_NEGATIVE_TTL 秒**，过期自动重探，
    #   装完刷新页面/点重检即可自动变绿，无需重启。
    PROBE_NEGATIVE_TTL = 10.0
    _probe_cache: ClassVar[Optional[Tuple[float, bool, str]]] = None
    # 引擎当前实际可见的语言集合（探测结果缓存，避免每次识别都起子进程）
    _langs_cache: ClassVar[Optional[set]] = None

    @classmethod
    def probe(cls, force: bool = False) -> Tuple[bool, str]:
        now = time.monotonic()
        if not force and cls._probe_cache is not None:
            ts, ok, reason = cls._probe_cache
            if ok or (now - ts) < cls.PROBE_NEGATIVE_TTL:
                return ok, reason
        ok, reason = cls._do_probe()
        cls._probe_cache = (now, ok, reason)
        return ok, reason

    @classmethod
    def invalidate(cls) -> None:
        """清掉探测缓存，使下一次 probe 必然重新探测（含语言包列表缓存）。"""
        cls._probe_cache = None
        cls._langs_cache = None

    @classmethod
    def _resolve_exe(cls) -> Optional[str]:
        """定位 tesseract 可执行程序：显式配置 → PATH → 常见安装目录。

        之所以要"显式配置"这一档：Windows 上的 Tesseract 安装包不会改写
        已经打开的终端/进程的 PATH，装完立刻启动的服务仍然找不到它。
        """
        if cfg.TESSERACT_CMD:
            p = Path(cfg.TESSERACT_CMD)
            return str(p) if p.exists() else None
        exe = shutil.which("tesseract")
        if exe:
            return exe
        for cand in TESSERACT_CANDIDATES:
            p = Path(cand.replace("%USERNAME%", Path.home().name))
            if p.exists():
                return str(p)
        return None

    @classmethod
    def _tessdata_dir(cls) -> Optional[str]:
        """确定语言包目录，按「显式配置 → 项目自带 → 用户级」取第一个可用的。

        这个顺序是踩出来的，改动前请看完：
          · 显式配置（cfg.TESSDATA_DIR）优先，部署方最清楚语言包在哪；
          · **项目自带**（data/tessdata）排第二：它与代码同生共死，本地与线上内容一致，
            行为最可预测。必须自带的原因很具体——发布到云端沙箱后，
            沙箱**已装 Tesseract 程序但只有 eng 语言包**，chi_sim 不在其内，
            结果是"引擎探测通过、用户拍照必然失败"。只改提示文案解决不了资源缺失，
            把语言包作为项目资源一起走才是根治。
          · 用户级目录（~/.workbuddy/tools/tesseract/tessdata）最后兜底，
            用于"程序装在 Program Files（不可写）、语言包另处维护"的本机调试场景。

        判定条件是**目录里确实有 chi_sim.traineddata**，而不是目录存在即可：
        语言包不全时若照样切过去，引擎会从"用自带包能跑"退化成
        `Error opening data file .../chi_sim.traineddata`，比不切更糟。
        """
        for d in (cfg.TESSDATA_DIR, cfg.TESSDATA_BUNDLED_DIR, USER_TESSDATA_DIR):
            if not d:
                continue
            p = Path(d)
            if (p / "chi_sim.traineddata").exists():
                return str(p)
        return None

    @classmethod
    def _tess_config(cls) -> str:
        """拼给 tesseract 的额外参数（目前只有语言包目录）。"""
        d = cls._tessdata_dir()
        if not d:
            return ""
        if " " in d:
            # 含空格的路径不能走命令行参数（见 _bind_tessdata 的说明），改由环境变量承担
            return ""
        return f"--tessdata-dir {d}"

    @classmethod
    def _bind_tessdata(cls) -> None:
        """把语言包目录告知 tesseract。

        两种手段的分工（都是踩出来的）：
          · 路径**不含空格** → 走 `--tessdata-dir` 命令行参数，最直观也最好排查；
          · 路径**含空格** → 改用 `TESSDATA_PREFIX` 环境变量。
            原因是 Windows 上 pytesseract 用 `shlex.split(config, posix=False)` 解析 config，
            posix=False 时引号**不会被剥离**，给路径加引号反而让 tesseract 把引号当成路径的一部分
            （实测报错：`Error opening data file "C:\\...\\tessdata"/chi_sim.traineddata`）；
            而不加引号又会被空格拆成两个参数。两条路都不通，只能交给环境变量。
            注意 TESSDATA_PREFIX 要指向 tessdata 的**父目录**，这是 tesseract 的约定。
        """
        d = cls._tessdata_dir()
        if d and " " in d:
            os.environ["TESSDATA_PREFIX"] = str(Path(d).parent)

    @classmethod
    def _list_langs(cls) -> set:
        """枚举引擎**实际可见**的语言包，直接读 `--list-langs` 的 CLI 输出。

        为什么不用 `pytesseract.get_languages()`：实测它在带 `--tessdata-dir` 时不可靠 ——
        同一个目录，`get_languages(config="--tessdata-dir <dir>")` 返回**空列表**，
        而命令行 `tesseract --list-langs --tessdata-dir <dir>` 正常返回 `chi_sim` / `eng`
        （两者在本机与云端脚本环境均可复现）。空列表不会报错，只会让探测里那句
        `if langs and "chi_sim" not in langs` 被**静默短路** —— "缺中文包"这个判断
        就此形同虚设，用户拍到失败才发现，且失败文案会误导成"请重拍"。
        这里绕开这层封装，直接起子进程读原始输出。

        返回空集表示**探不出来**（无引擎 / 调用异常），调用方应视为"未知"，
        而不是"一个语言包都没有" —— 后者会让一个实际可用的部署被误判为不可用。
        """
        exe = cls._resolve_exe()
        if not exe:
            return set()
        args = [exe, "--list-langs"]
        d = cls._tessdata_dir()
        env = dict(os.environ)
        if d:
            if " " in d:
                # 含空格路径不能走命令行参数（同 _bind_tessdata 的说明），改由环境变量承担
                env["TESSDATA_PREFIX"] = str(Path(d).parent)
            else:
                args += ["--tessdata-dir", d]
        try:
            r = subprocess.run(args, capture_output=True, timeout=20, env=env)
        except Exception:
            return set()
        out = (r.stdout or b"").decode("utf-8", "ignore") + (r.stderr or b"").decode("utf-8", "ignore")
        langs = set()
        for line in out.splitlines():
            line = line.strip()
            # 首行固定是 "List of available languages in ...:" 之类的说明，不是语言名
            if not line or line.lower().startswith("list of available"):
                continue
            langs.add(line)
        return langs

    @classmethod
    def _do_probe(cls) -> Tuple[bool, str]:
        try:
            import pytesseract
        except ImportError:
            return False, "未安装 Python 包 pytesseract"
        # 仅有 Python 包不够，必须能找到 tesseract 可执行程序
        exe = cls._resolve_exe()
        if not exe:
            return False, "未找到 tesseract 可执行程序（只装 Python 包不足以运行）"
        # 关键：pytesseract 只认 PATH 里的 tesseract。装完不改 PATH 的情况下，
        # 必须把解析出的绝对路径回写进 tesseract_cmd，否则后面的调用全部失败。
        pytesseract.pytesseract.tesseract_cmd = exe
        cls._bind_tessdata()
        # 探测已经把语言列表拿到手了，顺手喂给缓存，识别时就不必再起一次子进程
        langs = cls._list_langs()
        cls._langs_cache = langs
        if langs and "chi_sim" not in langs:
            # 缺中文包就判为**不可用**，而不是"可用但会退化"。
            # 本系统审的是中文合同，没有 chi_sim 时 tesseract 会 100% 失败
            # （报错 `Error opening data file .../chi_sim.traineddata`）。
            # 若仍算可用：前端能力条亮绿灯 → 用户拍完照才知道失败，
            # 且失败文案会按"照片问题"误导成"请重拍"，用户反复重拍却永远失败。
            # 实测场景：云端沙箱自带 Tesseract 程序但不带中文包。
            return False, (f"已装 Tesseract（{exe}）但缺少中文语言包 chi_sim，无法识别中文合同。"
                           "请安装 chi_sim.traineddata 后重试，或改用人工录入。")
        return True, f"可用（{exe}）"

    # ---- 模糊 / 低质图片：多预处理变体择优 ----
    # 一套预处理不可能同时治好"发灰""运动模糊""分辨率不足"三种退化，
    # 而它们拍出来的照片看起来都是"不清楚"。所以准备几套各有侧重的变体，
    # 让引擎各跑一遍再按分数择优 —— 用实际识别结果选，而不是靠体检指标猜。
    # 只在首轮结果明显偏弱时才启用（见 _weak_result），
    # 清晰照片因此不会白白多付 3~4 倍识别耗时。
    ENHANCE_VARIANTS = ("sharpen", "contrast", "binary", "upscale")

    @staticmethod
    def _score(text: str) -> int:
        """给一版识别结果打分，用于多版择优。

        不单纯比字数是刻意的：OCR 在模糊图上常吐出一堆"形似而非字"的碎片，
        字数不少但全是噪声。所以以**汉字数**为主（中文合同的信息主体），
        再对合同结构性关键词加权——「甲方/乙方/第…条/金额」这些词一旦出现，
        基本可以断定这一版确实读到了合同正文，而不是噪声。
        """
        hanzi = TesseractProvider.count_hanzi(text)
        keys = ("甲方", "乙方", "合同", "第一条", "第二条", "第三条", "金额", "违约", "保密")
        hit = sum(1 for k in keys if k in text)
        return hanzi + hit * 5

    def _weak_result(self, text: str) -> bool:
        """首轮结果是否"明显偏弱"、值得换预处理重跑。"""
        return (len(text) < self.FALLBACK_MIN_CHARS
                or self.count_hanzi(text) < self.FALLBACK_MIN_HANZI)

    def _variant(self, im, kind: str):
        """按名称生成一版预处理图像。任一步失败就原样返回——增强不该让识别崩掉。"""
        from PIL import ImageEnhance, ImageFilter, ImageOps

        try:
            if kind == "sharpen":
                # 运动模糊 / 轻微失焦：锐化把笔画边缘拉回来
                return im.filter(ImageFilter.UnsharpMask(radius=2, percent=170, threshold=3))
            if kind == "contrast":
                # 整体发灰、纸面反光：先自动色阶再加强对比
                return ImageOps.autocontrast(ImageEnhance.Contrast(im).enhance(1.8))
            if kind == "binary":
                # 阴影不均 / 背景脏：以均值为阈值二值化（Otsu 的廉价近似）
                g = ImageOps.autocontrast(im)
                import numpy as np

                th = float(np.asarray(g, dtype=np.float32).mean())
                return g.point(lambda p: 255 if p > th else 0)
            if kind == "upscale":
                # 分辨率不足才是主要矛盾时（整页合同拍得太小），字符高度不够，
                # 这里放宽放大上限到 2.2x —— 与首轮的 1.5x 上限是两种不同赌注。
                s = min(1600 / max(1, min(im.size)), 2.2)
                if s <= 1.05:
                    return im
                return im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
        except Exception:
            return im
        return im

    def _best_of_variants(self, im, cfg: str, base: str) -> str:
        """首轮偏弱时，用各预处理变体各跑一遍，取分数最高的那一版。"""
        import pytesseract

        best, best_score, best_kind = base, self._score(base), "原图"
        for kind in self.ENHANCE_VARIANTS:
            v = self._variant(im, kind)
            if v is im:
                continue  # 该变体对本图无意义（如已经够大还 upscale）
            try:
                t = pytesseract.image_to_string(
                    v, lang=self.LANG_PRIMARY, config=f"{cfg} --psm {self.PSM_PRIMARY}").strip()
            except Exception:
                continue  # 单版失败不影响其它版，也不影响已有的首轮结果
            s = self._score(t)
            if s > best_score:
                best, best_score, best_kind = t, s, kind
        # 记录选中的变体，便于排查"为什么这版更好"（写进日志而非正文）
        self.last_variant = best_kind
        return best

    def recognize(self, image_path: Path) -> str:
        import pytesseract
        from PIL import Image, ImageOps

        exe = self._resolve_exe()
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
        self._bind_tessdata()
        with Image.open(str(image_path)) as im:
            # 手机照片带 EXIF 旋转信息，先摆正再识别，否则整张"躺着"直接影响准确率
            im = ImageOps.exif_transpose(im)
            # 灰度化 + 适度放大短边，对低分辨率照片提升明显
            im = im.convert("L")
            if min(im.size) < 1200:
                # 放大倍数必须设上限（实测取 1.5）：把一张 1500×472 的图按"短边补到 1200"
                # 放大 2.54 倍后，「乙方」被认成 OH、「支付全款」被认成 ANTS.；
                # 同一张图放大 1.5 倍或干脆不放大，反而 8/8 全对。
                # 原因是 Tesseract 对字符高度敏感，过度放大带来的插值失真
                # 会抵消掉分辨率提升的收益 —— 放大不是越大越好。
                scale = min(1200 / max(1, min(im.size)), 1.5)
                if scale > 1.05:
                    im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
            cfg = self._tess_config()
            text = pytesseract.image_to_string(
                im, lang=self.LANG_PRIMARY, config=f"{cfg} --psm {self.PSM_PRIMARY}").strip()
            if len(text) < self.FALLBACK_MIN_CHARS:
                # 单栏假设在表格、多栏或印章密集的版面上可能整块漏掉，
                # 这时退回"自动分页"（psm 3）再试一次，代价只是多一次识别耗时。
                retry = pytesseract.image_to_string(
                    im, lang=self.LANG_PRIMARY, config=f"{cfg} --psm {self.PSM_FALLBACK}").strip()
                if len(retry) > len(text):
                    text = retry
            # 换 psm 仍偏弱 → 换预处理重跑（模糊/发灰/阴影/分辨率不足各有侧重）。
            # 放在语言兜底之前：增强若能把中文读对，就不必再赌"这是份英文合同"。
            if self._weak_result(text):
                text = self._best_of_variants(im, cfg, text)
            if TesseractProvider.count_hanzi(text) < self.FALLBACK_MIN_HANZI:
                # 汉字太少 → 这张图大概率不是中文文档（纯英文合同、纯数字表格等），
                # 纯中文包会退化；补跑一次混合语言包，取汉字更多的那个。
                #
                # 前提是混合包里的 eng 确实可见：`--tessdata-dir` 会把指定目录之外的语言包
                # 全部排除，只带 chi_sim 的部署下直接写 "chi_sim+eng" 会抛错（详见 _visible_langs）。
                # 若 eng 不可见则**整段跳过**——继续用同一个 chi_sim 重跑一遍没有意义，
                # 只会白耗一次识别耗时，还可能把一个成功的结果换成失败。
                if "eng" in self._visible_langs():
                    try:
                        mixed = pytesseract.image_to_string(
                            im, lang=self.LANG_FALLBACK,
                            config=f"{cfg} --psm {self.PSM_FALLBACK}").strip()
                    except Exception:
                        # 兜底是"锦上添花"，失败不该拖垮主结果：主结果已经拿到了
                        mixed = ""
                    if TesseractProvider.count_hanzi(mixed) > TesseractProvider.count_hanzi(text):
                        text = mixed
            return text

    @classmethod
    def _visible_langs(cls) -> set:
        """当前绑定的语言包目录下，引擎**实际能看见**的语言集合。

        为什么必须单独探一次：一旦用 `--tessdata-dir` / `TESSDATA_PREFIX` 指定了目录，
        Tesseract 就**只看这个目录**，引擎自带的其它语言包会被一并排除在外。
        而 `_tessdata_dir()` 的判定条件只要求目录里有 `chi_sim`（中文合同是本系统主场景），
        完全可能拿到一个"只有中文包"的目录 —— 此时 `LANG_FALLBACK="chi_sim+eng"` 里的
        `eng` 并不存在，直接调用会抛 `Error opening data file .../eng.traineddata`。
        这类失败还会被上层归成"引擎已运行但未识别出文字"，文案再引导用户"重拍"，
        把一个部署问题伪装成拍摄问题。所以兜底之前必须先问一句"eng 到底在不在"。

        结果缓存（语言包目录在进程生命周期内不会变，不必每次识别都起子进程）。
        """
        if cls._langs_cache is not None:
            return cls._langs_cache
        # 必须复用 _list_langs（CLI 探测），不能用 pytesseract.get_languages：
        # 后者在带 --tessdata-dir 时返回空集（见 _list_langs 的说明），
        # 会让这里的"eng 在不在"永远判否 —— 兜底分支被静默关掉，且毫无报错。
        cls._bind_tessdata()
        cls._langs_cache = cls._list_langs()
        return cls._langs_cache

    @staticmethod
    def count_hanzi(text: str) -> int:
        """统计常用汉字个数，用于判断"这张图到底是不是中文文档"。"""
        return sum(1 for ch in text or "" if "\u4e00" <= ch <= "\u9fff")


# ---------------------------------------------------------------------- #
# 引擎二：多模态大模型识图
# ---------------------------------------------------------------------- #
class VLMProvider(OCRProvider):
    """多模态大模型识图：对手写、盖章、复杂版面的鲁棒性最好，但需外网与密钥。"""

    provider_name = "vlm"
    provider_label = "多模态大模型识图"
    priority = 20

    # 识别合同照片的核心提示词：强调"只输出正文"，避免模型加上自己的解释
    PROMPT = (
        "这是一份合同/协议文件的照片。请逐字识别图片中的全部文字，"
        "按原文顺序输出，保留段落与条款编号。只输出识别到的正文，"
        "不要添加任何说明、标题或总结；无法辨认的字用 \u25a1 代替。"
    )

    @classmethod
    def probe(cls, force: bool = False) -> Tuple[bool, str]:
        # 本引擎只看配置，不碰磁盘/子进程，无需缓存，force 自然无需特殊处理
        if (cfg.LLM_PROVIDER or "").lower() != "openai":
            return False, "未启用大模型识图（config.LLM_PROVIDER 当前非 openai）"
        if not cfg.LLM_API_KEY:
            return False, "未配置 LLM_API_KEY"
        return True, "可用"

    def recognize(self, image_path: Path) -> str:
        suffix = image_path.suffix.lower().lstrip(".")
        mime = "image/jpeg" if suffix in ("jpg", "jpeg") else f"image/{suffix or 'png'}"
        b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        payload = {
            "model": cfg.LLM_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": self.PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
            "temperature": 0,
        }
        return _post_json(f"{cfg.LLM_BASE_URL.rstrip('/')}/chat/completions",
                          payload, cfg.LLM_API_KEY)["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------- #
# 引擎三：通用云 OCR 接口
# ---------------------------------------------------------------------- #
class CloudOCRProvider(OCRProvider):
    """通用云 OCR：适配"上传图片 → 返回文本"的常见 HTTP 接口形态。"""

    provider_name = "cloud"
    provider_label = "云 OCR 接口"
    priority = 30

    @classmethod
    def probe(cls, force: bool = False) -> Tuple[bool, str]:
        # 同 vlm：纯配置判定，无副作用，不需要缓存
        if not cfg.OCR_API_URL:
            return False, "未配置 OCR_API_URL"
        return True, "可用"

    def recognize(self, image_path: Path) -> str:
        b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        data = _post_json(cfg.OCR_API_URL, {"image_base64": b64}, cfg.OCR_API_KEY)
        # 兼容几种常见的返回结构，避免绑定单一厂商
        for key in ("text", "result", "data", "words"):
            val = data.get(key) if isinstance(data, dict) else None
            if isinstance(val, str) and val.strip():
                return val
            if isinstance(val, dict) and isinstance(val.get("text"), str):
                return val["text"]
            if isinstance(val, list):
                return "\n".join(str(x.get("text", x)) if isinstance(x, dict) else str(x) for x in val)
        return ""


def _post_json(url: str, payload: dict, api_key: Optional[str]) -> dict:
    """用标准库发 JSON POST（不额外引入 requests，保持依赖最小化）。"""
    import urllib.request

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------- #
# 引擎四：人工录入兜底
# ---------------------------------------------------------------------- #
class ManualProvider(OCRProvider):
    """人工录入兜底。

    它不产出文本，存在的意义是：当自动识别全链路不可用时，
    仍然能给出一个明确、可操作的出路（照着照片把正文敲/贴进来），
    而不是让用户卡在"解析受阻"上。
    """

    provider_name = "manual"
    provider_label = "人工录入（兜底）"
    priority = 999

    @classmethod
    def probe(cls, force: bool = False) -> Tuple[bool, str]:
        return True, "始终可用：识别引擎不可用时，可对照原件人工录入或粘贴正文"

    def recognize(self, image_path: Path) -> str:
        return ""


# ---------------------------------------------------------------------- #
# 服务入口
# ---------------------------------------------------------------------- #
class OCRService:
    """拍照识别统一入口：体检 → 依次尝试各引擎 → 全失败则明确降级。"""

    def __init__(self) -> None:
        self.providers: List[OCRProvider] = [cls() for cls in OCRProvider.all_providers()]

    # ---------------- 能力探测 ---------------- #
    @staticmethod
    def probe(force: bool = False) -> dict:
        """汇报各引擎可用性与修复指引，供前端在识别前就展示能力现状。

        force=True 忽略缓存重新探测，对应界面上的「重新检测」：
        用户在服务运行期间装好了引擎/语言包时，不必重启服务就能让它变绿。
        """
        items = []
        for cls in OCRProvider.all_providers():
            available, reason = cls.probe(force=force)
            items.append({
                "name": cls.provider_name,
                "label": cls.provider_label,
                "priority": cls.priority,
                "available": available,
                "reason": reason,
                "hint": INSTALL_HINTS.get(cls.provider_name, ""),
            })
        # "是否存在能自动识别的引擎"要把 manual 排除掉，否则永远为真
        auto = [i for i in items if i["name"] != "manual"]
        return {
            "providers": items,
            "auto_available": any(i["available"] for i in auto),
            "auto_available_count": sum(1 for i in auto if i["available"]),
            "supported_suffixes": sorted(IMAGE_SUFFIXES),
            "active_label": next((i["label"] for i in auto if i["available"]), ""),
            # 一次可提交的最大页数：前端据此在选图时就拦住"误选整个相册"
            "max_pages": cfg.MAX_SCAN_PAGES,
            # 当前进程的解释器路径：pytesseract 装在别的 Python 里是"探不到引擎"的常见原因，
            # 报出来才能一眼分辨"没装"还是"装到别的环境去了"。
            "python_exe": sys.executable,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ---------------- 图片体检 ---------------- #
    @staticmethod
    def inspect(image_path: Path) -> ImageQuality:
        """图片体检：分辨率 / 清晰度 / 亮度 / 对比度，并生成可操作的拍摄建议。

        阈值均为经验值，只作为提示而不是硬性拦截——模糊的照片也可能识别成功，
        因此这里只给建议，让用户自己决定是否重拍。
        """
        q = ImageQuality()
        try:
            from PIL import Image, ImageOps
        except ImportError:  # 无 PIL 时跳过体检，不影响主流程
            return q
        try:
            with Image.open(str(image_path)) as raw:
                im = ImageOps.exif_transpose(raw).convert("L")
                q.width, q.height = im.size
                # 统一缩到固定宽度再算指标，避免大小图之间不可比
                if im.width > 1200:
                    im = im.resize((1200, max(1, int(im.height * 1200 / im.width))), Image.LANCZOS)
                try:
                    import numpy as np

                    arr = np.asarray(im, dtype=np.float32)
                except ImportError:
                    return q
            if arr.shape[0] > 2 and arr.shape[1] > 2:
                # 拉普拉斯算子（二阶差分）近似，方差越大边缘越锐利
                lap = (-4 * arr[1:-1, 1:-1] + arr[:-2, 1:-1] + arr[2:, 1:-1]
                       + arr[1:-1, :-2] + arr[1:-1, 2:])
                q.sharpness = float(lap.var())
            q.brightness = float(arr.mean())
            q.contrast = float(arr.std())
            q.highlight_ratio = float((arr > 250).mean())
        except Exception:
            return q

        if 0 < q.short_side < 1000:
            q.warnings.append(f"分辨率偏低（短边 {q.short_side}px），建议靠近拍摄或提高相机分辨率")
        if 0 < q.sharpness < 60:
            q.warnings.append(f"画面偏模糊（清晰度 {q.sharpness:.0f}），建议手持稳定、重新对焦后重拍")
        if 0 < q.brightness < 70:
            q.warnings.append("光线偏暗，建议补光或开启闪光灯后重拍")
        elif q.highlight_ratio > 0.5 and q.contrast < 20:
            # 过曝必须「亮部占比高」与「对比度几乎归零」同时成立：
            #   只看亮度均值 → 白纸黑字的合同白底占大半版面，均值天然偏高（实测 245+）；
            #   只看亮部占比 → 白纸本身就是近饱和像素，占比必然很高（实测 94%）；
            #   仅用"对比度偏低"的阈值 28 → 正常合同照片对比度约 42，仍会被误判。
            # 实测分界很清楚：正常白底黑字对比度 42.3，真正一片死白的图仅 0.7，
            # 故取 20 作为"文字已被冲掉"的判据。
            q.warnings.append(
                f"画面过曝（亮部占比 {q.highlight_ratio:.0%} 且明暗对比不足），建议避开强光直射与反光面"
            )
        elif 0 < q.contrast < 28:
            q.warnings.append("明暗对比偏低，可能影响识别准确度")
        return q

    # ---------------- 识别 ---------------- #
    def recognize(self, image_path: Path) -> OCRResult:
        """按优先级尝试各自动引擎；全部不可用/未取到文本时，返回人工录入降级结果。"""
        image_path = Path(image_path)
        quality = self.inspect(image_path)
        attempts: List[dict] = []

        for prov in self.providers:
            cls = type(prov)
            if cls.provider_name == "manual":
                continue  # 兜底引擎不参与自动尝试
            available, reason = cls.probe()
            if not available:
                # 记下"为什么没试"，这是用户判断"要不要去装引擎"的依据
                attempts.append({"provider": cls.provider_name, "label": cls.provider_label,
                                 "status": "skipped", "reason": reason})
                continue
            try:
                text = (prov.recognize(image_path) or "").strip()
            except Exception as e:
                attempts.append({"provider": cls.provider_name, "label": cls.provider_label,
                                 "status": "error", "reason": f"{type(e).__name__}: {e}"})
                continue
            if text:
                attempts.append({"provider": cls.provider_name, "label": cls.provider_label,
                                 "status": "ok", "chars": len(text)})
                return OCRResult(ok=True, text=text,
                                 provider=cls.provider_name, provider_label=cls.provider_label,
                                 quality=quality, attempts=attempts)
            attempts.append({"provider": cls.provider_name, "label": cls.provider_label,
                             "status": "empty", "reason": "引擎已运行但未识别到文字"})

        reason, hint = self._degrade_message(attempts)
        return OCRResult(ok=False, provider="manual", provider_label="人工录入（兜底）",
                         reason=reason, hint=hint, quality=quality, attempts=attempts)

    @staticmethod
    def _degrade_message(attempts: List[dict]) -> Tuple[str, str]:
        """把尝试链路翻译成一句人能看懂的原因 + 一条可执行的出路。"""
        ran = [a for a in attempts if a["status"] in ("error", "empty")]
        missing = [a for a in attempts if a["status"] == "skipped"]
        if not ran and missing:
            names = "、".join(a["label"] for a in missing)
            # 「装了引擎但缺中文包」与「压根没装引擎」对用户是两回事：
            # 前者补一个语言包就能用，后者要装整套。文案必须分开说，
            # 否则能力条写着"已装 Tesseract，缺 chi_sim"，这里却说"未安装"，自相矛盾。
            lang_issue = [a for a in missing
                          if "chi_sim" in str(a.get("reason", ""))
                          or "语言包" in str(a.get("reason", ""))]
            if lang_issue:
                return ("已安装的识别引擎缺少中文语言包 chi_sim，无法识别中文合同正文。",
                        "请在服务器上安装 chi_sim.traineddata 并放入 tessdata 目录"
                        "（必要时用 config.TESSDATA_DIR 指定路径）后重试；"
                        "或对照原件人工录入/粘贴正文后继续审查。")
            return (f"本机未安装可用的 OCR 引擎（{names} 均不可用），无法自动识别图片文字。",
                    "可对照原件人工录入/粘贴正文后继续审查；"
                    "或按提示安装引擎（如 Tesseract + chi_sim 中文包）后重试。")
        if ran:
            # 语言包缺失属于**环境问题**，不是照片质量问题。
            # 若混进通用的"建议重拍"文案，用户会反复重拍却永远失败（云端沙箱实测即此情况）。
            lang_missing = [a for a in ran
                            if "traineddata" in str(a.get("reason", "")).lower()
                            or "chi_sim" in str(a.get("reason", "")).lower()]
            if lang_missing:
                names = "、".join(dict.fromkeys(a["label"] for a in lang_missing))
                return (f"{names} 缺少中文语言包 chi_sim，无法识别中文合同正文。",
                        "这不是照片质量问题，重拍无效：请先在服务器上安装 chi_sim.traineddata "
                        "（放入 tessdata 目录，必要时用 config.TESSDATA_DIR 指定路径），"
                        "或直接对照照片人工录入正文后继续审查。")
            detail = "；".join(f"{a['label']}：{a.get('reason', '未识别到文字')}" for a in ran)
            return (f"引擎已运行但未能识别出文字（{detail}）。",
                    "通常为图像模糊、倾斜、反光或光照不足所致；建议按上方体检建议重拍或重新扫描，"
                    "或直接在人工校正区录入正文继续审查。")
        return ("未能从该图片中提取到文本。", "请人工录入或粘贴正文后继续审查。")


# 模块级单例：探测结果带缓存，复用同一实例可少走子进程
ocr_service = OCRService()
