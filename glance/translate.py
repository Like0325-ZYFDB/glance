# -*- coding: utf-8 -*-
"""中英划词翻译。百度、必应、谷歌优先，MyMemory 仅在前三个来源都连不上时尝试。

来源把原文原样还回来（专有名词、数字、代码）算「不用翻译」，不算失败：
短的原文以最优先的来源为准，不再换成别家的硬译，也不去问兜底来源。
"""
from __future__ import annotations

import hashlib
import html
import random
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urlsplit

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}
TIMEOUT = (3, 8)          # (连接, 读取) 秒：连不上的来源 3 秒就放弃
DOWN_SECONDS = 600        # 失败的来源多久内不再等待
GRACE = 1.25              # 有主要来源的译文后，最多再等更优先的来源多久
WARM_INTERVAL = 30        # 预热连接的最短间隔（秒）
CACHE_SIZE = 300
MAX_TEXT_LENGTH = 3000
DEADLINE = 10.0           # 一次翻译最多等多久；长原文按长度再加（3000 字约 20 秒）
FALLBACK = "MyMemory"
FALLBACK_BUDGET = 3.5     # 主要来源都没结果时，留给兜底来源的时间
MYMEMORY_LIMIT = 500      # UTF-8 字节
MYMEMORY_MAX_CHUNKS = 4
GOOGLE_LIMIT = 3000       # UTF-8 字节：谷歌免费端点把原文放在网址里，太长会被拒（HTTP 400）
SHORT_WORDS, SHORT_CHARS = 3, 40   # 这么短的原文多半是一个词或一个名字

HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
LATIN_WORD = re.compile(r"[A-Za-z]+")
HAN_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
KANA = re.compile(r"[ぁ-ゖァ-ヺ]")
# 英文句子里少不了的虚词：夹着汉字的原文里它们占比高，说明是英文句子引了几个中文词
EN_FUNCTION_WORDS = frozenset("""
an the is are was were be been am do does did to of in on at for with from by and or but not this that these
those it its he she they we you my your his her their our as if so than then there here have has had will would
can could should may might must
""".split())
PARAGRAPH_BREAK = re.compile(r"(?:\r?\n[ \t]*){2,}")
SENTENCE_BREAK = re.compile(r"""[.!?。！？；;]+["'”’」』）)]*\s*""")
WORD_BREAK = re.compile(r"\s+")

_SETTINGS = {"baidu_appid": "", "baidu_key": ""}
_DOWN: dict[str, float] = {}
_LOCAL = threading.local()                 # 每个线程各记各的「上次用的来源」：取词和整句翻译可能同时在翻
_CACHE: dict[str, tuple[str, str]] = {}    # 原文 → (译文, 来源)
_WARM = {"at": 0.0}
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="translate")
# 长原文切开后各段同时请求。另开一个线程池：和 _POOL 共用的话，池子满了会互相等死
_CHUNK_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="translate-chunk")
_LOCK = threading.Lock()


def _session() -> requests.Session:
    s = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
    s.mount("https://", adapter)
    s.headers.update(HEADERS)
    return s


_SESSION = _session()


def configure(baidu_appid: str = "", baidu_key: str = "") -> None:
    settings = {"baidu_appid": (baidu_appid or "").strip(), "baidu_key": (baidu_key or "").strip()}
    global _SETTINGS
    with _LOCK:
        if settings != _SETTINGS:
            _SETTINGS = settings
            _CACHE.clear()
            _DOWN.pop("百度翻译", None)


def last_source() -> str:
    """本线程最近一次翻译用的来源名（给界面显示）。"""
    return getattr(_LOCAL, "source", "")


def last_status() -> str:
    """本线程最近一次翻译的结果：ok 有译文 / unchanged 不用翻译（来源原样返回）/ failed 没取到。"""
    return getattr(_LOCAL, "status", "")


def _is_japanese(text: str) -> bool:
    """假名够多才算日文：中文里偶尔夹一个「の」不算。"""
    kana = len(KANA.findall(text))
    return bool(kana) and kana * 10 >= len(HAN.findall(text))


def _is_chinese(text: str) -> bool:
    """原文算不算中文（算就翻成英文，不算就翻成中文）。"""
    han = len(HAN.findall(text))
    if not han or _is_japanese(text):
        return False
    words = LATIN_WORD.findall(text)
    if han >= len(words):
        return True
    # 汉字比英文单词少：汉字散在几处、英文部分又不像句子（命令、术语）的，是夹着英文的中文
    function_words = sum(w.lower() in EN_FUNCTION_WORDS for w in words)
    return len(HAN_RUN.findall(text)) >= 2 and function_words * 4 < len(words)


def _is_mixed(text: str) -> bool:
    """中英各占一些、方向说不准的原文：来源往哪边翻都接受。"""
    han = len(HAN.findall(text))
    return 0 < han < len(LATIN_WORD.findall(text)) and not _is_japanese(text)


def _is_short(text: str) -> bool:
    return len(text) <= SHORT_CHARS and len(text.split()) <= SHORT_WORDS


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def _baidu(text: str) -> str | None:
    settings = _SETTINGS
    appid, key = settings["baidu_appid"], settings["baidu_key"]
    if not appid or not key:
        return None
    target = "en" if _is_chinese(text) else "zh"

    def request(chunk):
        salt = str(random.randint(10000, 99999))
        sign = hashlib.md5(f"{appid}{chunk}{salt}{key}".encode("utf-8")).hexdigest()
        r = _SESSION.post(
            "https://fanyi-api.baidu.com/api/trans/vip/translate",
            data={"q": chunk, "from": "auto", "to": target, "appid": appid, "salt": salt, "sign": sign},
            timeout=TIMEOUT,
        )
        data = r.json()
        segments = [seg.get("dst", "") for seg in data.get("trans_result", [])]
        return "\n".join(segments) if segments and all(seg.strip() for seg in segments) else None

    # 百度免费档每秒只许一次请求：各段挨个来
    return _translate_chunks(_chunks(text, size=2000), request, target == "en")


def _google(text: str) -> str | None:
    """谷歌免费端点（无需密钥）。原文在网址里，长了要切开。"""
    zh = _is_chinese(text)
    src, dst = ("zh-CN", "en") if zh else ("auto", "zh-CN")

    def request(chunk):
        r = _SESSION.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": src, "tl": dst, "dt": "t", "q": chunk},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return "".join(seg[0] for seg in data[0] if seg and seg[0])

    return _translate_chunks(_chunks(text, size=GOOGLE_LIMIT, utf8=True), request, zh, parallel=True)


def _chunks(text: str, size: int = 450, *, utf8: bool = False) -> list[str]:
    """优先在段落、句子和单词边界切分，保留完整原文。"""
    out = []
    while len(text.encode("utf-8") if utf8 else text) > size:
        prefix = text.encode("utf-8")[:size].decode("utf-8", "ignore") if utf8 else text[:size]
        cut = len(prefix)
        for boundary in (PARAGRAPH_BREAK, SENTENCE_BREAK, WORD_BREAK):
            end = max((match.end() for match in boundary.finditer(prefix)), default=0)
            if end >= len(prefix) // 2 and end:
                cut = end
                break
        out.append(text[:cut])
        text = text[cut:]
    if text:
        out.append(text)
    return out


def _translate_chunks(chunks, request, to_english: bool, parallel: bool = False) -> str | None:
    """逐段翻译再拼回去（保留段落间的换行）；有一段没翻出来就整个算失败。"""
    todo = [chunk.strip() for chunk in chunks if chunk.strip()]
    if parallel and len(todo) > 1:
        results = iter(list(_CHUNK_POOL.map(request, todo)))
    else:
        results = (request(chunk) for chunk in todo)   # 挨个来：前一段失败就不再请求后面的
    parts = []
    for chunk in chunks:
        if chunk.strip():
            result = next(results)
            if not result or not result.strip():
                return None
            parts.append(result.strip())
        trailing = chunk[len(chunk.rstrip()):]
        if "\n" in trailing:
            parts.append("\n\n" if PARAGRAPH_BREAK.search(trailing) else "\n")
        elif to_english:
            parts.append(" ")
    return "".join(parts).strip()


def _mymemory(text: str) -> str | None:
    """MyMemory 免费接口（每日额度，作备用）。"""
    zh = _is_chinese(text)
    if KANA.search(text):
        return None  # 兜底接口要求指定原文语言，这里只支持中英互译
    chunks = _chunks(text, size=MYMEMORY_LIMIT, utf8=True)
    if len(chunks) > MYMEMORY_MAX_CHUNKS:
        return None  # 兜底服务只接受能在少量请求内翻完的原文
    pair = "zh-CN|en" if zh else "en|zh-CN"

    def request(chunk):
        r = _SESSION.get(
            "https://api.mymemory.translated.net/get",
            params={"q": chunk, "langpair": pair},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if str(data.get("responseStatus")) != "200" or data.get("quotaFinished"):
            return None
        return html.unescape(data["responseData"]["translatedText"])

    return _translate_chunks(chunks, request, zh)


# 必应翻译：先从翻译页拿到 IG / key / token（约一小时有效），再调用页面自己用的接口
_BING: dict = {"until": 0.0}
_BING_LOCK = threading.Lock()


def _bing_params(rejected: str = "") -> dict:
    """rejected：刚被拒的那个 token。几段同时被拒时只重新取一次参数。"""
    with _BING_LOCK:
        if time.time() > _BING["until"] or (rejected and _BING.get("token") == rejected):
            r = _SESSION.get("https://cn.bing.com/translator", timeout=TIMEOUT)
            page = r.text
            ig = re.search(r'IG:"([^"]+)"', page)
            iid = re.search(r'data-iid="([^"]+)"', page)
            ab = re.search(r'params_AbusePreventionHelper\s*=\s*\[(\d+),"([^"]+)",(\d+)', page)
            if not (ig and ab):
                raise RuntimeError("必应翻译页面里没找到参数")
            ttl = min(int(ab.group(3)) / 1000, 3600)
            _BING.clear()
            _BING.update(host=urlsplit(r.url).netloc or "www.bing.com", ig=ig.group(1),
                         iid=iid.group(1) if iid else "translator.5028", key=ab.group(1),
                         token=ab.group(2), until=time.time() + max(60, ttl - 120), n=0)
        _BING["n"] += 1
        return dict(_BING)


def _bing(text: str) -> str | None:
    """必应翻译网页端点（无需密钥，国内能直连）。参数过期或被拒时重新取一次。"""
    zh = _is_chinese(text)

    def request(chunk):
        rejected = ""
        for _attempt in range(2):
            p = _bing_params(rejected)
            rejected = p["token"]
            r = _SESSION.post(
                f"https://{p['host']}/ttranslatev3",
                params={"isVertical": "1", "IG": p["ig"], "IID": f"{p['iid']}.{p['n']}"},
                data={"fromLang": "auto-detect", "to": "en" if zh else "zh-Hans", "text": chunk,
                      "token": p["token"], "key": p["key"]},
                headers={"Referer": f"https://{p['host']}/translator"},
                timeout=TIMEOUT,
            )
            try:
                result = r.json()[0]["translations"][0]["text"]
                if result.strip():
                    return result
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                pass
        return None

    return _translate_chunks(_chunks(text, size=900), request, zh, parallel=True)


SOURCES = [("百度翻译", _baidu), ("必应翻译", _bing), ("谷歌翻译", _google), ("MyMemory", _mymemory)]
# 预热时连哪里（必应要先取参数，取参数本身就连上了）
_WARM_URLS = {
    "百度翻译": "https://fanyi-api.baidu.com/",
    "谷歌翻译": "https://translate.googleapis.com/",
}


def _run(name: str, fn, text: str) -> tuple[str, str] | None:
    """调一个来源：("ok", 译文) / ("same", "") 原样返回，即不用翻译 / None 没结果或翻错了方向。"""
    try:
        out = fn(text)
    except Exception:
        out = None
    out = out.strip() if isinstance(out, str) else ""
    with _LOCK:
        if out:
            _DOWN.pop(name, None)
        else:
            _DOWN[name] = time.time()
    if not out:
        return None
    if _same(out, text):
        return ("same", "")
    if _is_mixed(text):
        return ("ok", out)
    if _is_chinese(text):
        return ("ok", out) if LATIN_WORD.search(out) and not _is_chinese(out) else None
    return ("ok", out) if HAN.search(out) and not KANA.search(out) else None


def _usable(sources):
    settings = _SETTINGS
    if not (settings["baidu_appid"] and settings["baidu_key"]):
        sources = [s for s in sources if s[0] != "百度翻译"]
    return sources


def _down_now() -> set[str]:
    now = time.time()
    with _LOCK:
        return {n for n, t in _DOWN.items() if now - t < DOWN_SECONDS}


def _race(text, sources, end):
    """几个来源同时请求，返回 (优先级, 来源名, "ok" / "same", 译文)；都没结果返回 None。

    短的原文（一个词、一个名字）：最优先的来源说什么就是什么，原样返回也算数——
    必应把 ChatGPT 原样留着是对的，不该换成别家硬译的「聊天GPT」。
    长的原文：有译文的优先于原样返回的。
    """
    if not sources or end <= time.monotonic():
        return None
    short = _is_short(text)

    def rank(i, kind):
        return (False, i) if short else (kind != "ok", i)

    down = _down_now()
    futures = {_POOL.submit(_run, name, fn, text): (i, name) for i, (name, fn) in enumerate(sources)}
    pending, best, grace_end = set(futures), None, None
    while pending:
        left = (min(end, grace_end) if grace_end else end) - time.monotonic()
        if left <= 0:
            break
        done, pending = wait(pending, timeout=left, return_when=FIRST_COMPLETED)
        for fut in done:
            i, name = futures[fut]
            try:
                out = fut.result()
            except Exception:
                out = None
            if out and (best is None or rank(i, out[0]) < rank(best[0], best[2])):
                best = (i, name, out[0], out[1])
        if best:
            # 还在等的来源里有可能给出更好结果、最近也没失败过的：最多再等 GRACE 秒，否则马上用
            floor = rank(best[0], best[2])
            if not any(rank(futures[f][0], "ok") < floor and futures[f][1] not in down for f in pending):
                break
            grace_end = grace_end or time.monotonic() + GRACE
    for fut in pending:
        fut.cancel()
    return best


def translate(text: str, sources=None, deadline: float | None = None) -> str | None:
    """整段翻译，自动识别中英方向。没有译文时返回 None，原因看 last_status()。"""
    _LOCAL.source, _LOCAL.status = "", "failed"
    text = (text or "").strip()
    if deadline is None:
        deadline = DEADLINE + min(len(text), MAX_TEXT_LENGTH) / 300
    if not text or len(text) > MAX_TEXT_LENGTH or deadline <= 0:
        return None
    custom = sources is not None
    with _LOCK:
        settings = _SETTINGS
        cached = None if custom else _CACHE.get(text)
    if cached:
        translated, _LOCAL.source = cached
        _LOCAL.status = "ok" if translated else "unchanged"
        return translated or None
    available = _usable(sources if custom else SOURCES)
    primary = [source for source in available if source[0] != FALLBACK]
    fallback = [source for source in available if source[0] == FALLBACK]
    if len(text.encode("utf-8")) > MYMEMORY_LIMIT * MYMEMORY_MAX_CHUNKS:
        fallback = []   # 兜底来源接不了这么长的原文：时间全留给主要来源
    now = time.monotonic()
    end = now + deadline
    primary_end = end - min(FALLBACK_BUDGET, deadline * 0.35) if fallback else end
    best = _race(text, primary, primary_end)
    if not best:
        # 主要来源一个都没答上来（多半是连不上）才问兜底来源；它原样返回不可信，算没结果
        best = _race(text, fallback, end)
        if best and best[2] != "ok":
            best = None
    if not best:
        return None
    _LOCAL.source = best[1]
    _LOCAL.status = "ok" if best[2] == "ok" else "unchanged"
    if not custom and best[1] != FALLBACK:
        with _LOCK:
            if settings is _SETTINGS:
                if len(_CACHE) >= CACHE_SIZE:
                    _CACHE.clear()
                _CACHE[text] = (best[3], best[1])
    return best[3] or None


def _warm_one(name: str) -> None:
    try:
        if name == "必应翻译":
            _bing_params()
        else:
            _SESSION.head(_WARM_URLS[name], timeout=TIMEOUT)
    except Exception:
        pass


def warm() -> None:
    """提前连好各个来源（选文字、按翻译快捷键时调用；30 秒内只做一次，不阻塞）。"""
    now = time.time()
    with _LOCK:
        if now - _WARM["at"] < WARM_INTERVAL:
            return
        _WARM["at"] = now
    down = _down_now()
    for name, _fn in _usable(SOURCES):
        if name != FALLBACK and name not in down:
            _POOL.submit(_warm_one, name)


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    for s in [
        "The quick brown fox jumps over the lazy dog.",
        "Learning a language takes time and patience.",
        "今天天气很好，我们去公园散步吧。",
    ]:
        t0 = time.time()
        out = translate(s)
        print(f"{s}\n  -> {out}  [{last_source()} {last_status()}] ({time.time()-t0:.1f}s)")
