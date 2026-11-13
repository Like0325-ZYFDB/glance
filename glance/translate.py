# -*- coding: utf-8 -*-
"""中英划词翻译。百度、必应、谷歌优先，MyMemory 仅在前三个来源不可用时尝试。"""
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
FALLBACK = "MyMemory"
MYMEMORY_LIMIT = 500      # UTF-8 字节
MYMEMORY_MAX_CHUNKS = 4

HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
LATIN_WORD = re.compile(r"[A-Za-z]+")
KANA = re.compile(r"[ぁ-ゖァ-ヺ]")
PARAGRAPH_BREAK = re.compile(r"(?:\r?\n[ \t]*){2,}")
SENTENCE_BREAK = re.compile(r"""[.!?。！？；;]+["'”’」』）)]*\s*""")
WORD_BREAK = re.compile(r"\s+")

_SETTINGS = {"baidu_appid": "", "baidu_key": ""}
_DOWN: dict[str, float] = {}
_LOCAL = threading.local()                 # 每个线程各记各的「上次用的来源」：取词和整句翻译可能同时在翻
_CACHE: dict[str, tuple[str, str]] = {}    # 原文 → (译文, 来源)
_WARM = {"at": 0.0}
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="translate")
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


def _is_chinese(text: str) -> bool:
    han = len(HAN.findall(text))
    return bool(han and han >= len(LATIN_WORD.findall(text)) and not KANA.search(text))


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

    return _translate_chunks(_chunks(text, size=2000), request, target == "en")


def _google(text: str) -> str | None:
    """谷歌免费端点（无需密钥）。"""
    src, dst = ("zh-CN", "en") if _is_chinese(text) else ("auto", "zh-CN")
    r = _SESSION.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": src, "tl": dst, "dt": "t", "q": text},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        return None
    data = r.json()
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


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


def _translate_chunks(chunks, request, to_english: bool) -> str | None:
    parts = []
    for chunk in chunks:
        if chunk.strip():
            result = request(chunk.strip())
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


def _bing_params(refresh: bool = False) -> dict:
    with _BING_LOCK:
        if refresh or time.time() > _BING["until"]:
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
        for attempt in range(2):
            p = _bing_params(refresh=attempt > 0)
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

    return _translate_chunks(_chunks(text, size=900), request, zh)


SOURCES = [("百度翻译", _baidu), ("必应翻译", _bing), ("谷歌翻译", _google), ("MyMemory", _mymemory)]
# 预热时连哪里（必应要先取参数，取参数本身就连上了）
_WARM_URLS = {
    "百度翻译": "https://fanyi-api.baidu.com/",
    "谷歌翻译": "https://translate.googleapis.com/",
}


def _run(name: str, fn, text: str) -> str | None:
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
    if not out or out.casefold() == text.casefold():
        return None
    if _is_chinese(text):
        return out if LATIN_WORD.search(out) and not _is_chinese(out) else None
    return out if HAN.search(out) and not KANA.search(out) else None


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
    if not sources or end <= time.monotonic():
        return None
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
            if out and (best is None or i < best[0]):
                best = (i, name, out)
        if best:
            # 还在等的来源里有更优先、最近也没失败过的：最多再等 GRACE 秒，否则马上用
            if not any(futures[f][0] < best[0] and futures[f][1] not in down for f in pending):
                break
            grace_end = grace_end or time.monotonic() + GRACE
    for fut in pending:
        fut.cancel()
    return best


def translate(text: str, sources=None, deadline: float = 10.0) -> str | None:
    """整段翻译，自动识别中英方向；全部失败返回 None。"""
    _LOCAL.source = ""
    text = (text or "").strip()
    if not text or len(text) > MAX_TEXT_LENGTH or deadline <= 0:
        return None
    custom = sources is not None
    with _LOCK:
        settings = _SETTINGS
        cached = None if custom else _CACHE.get(text)
    if cached:
        translated, _LOCAL.source = cached
        return translated
    available = _usable(sources if custom else SOURCES)
    primary = [source for source in available if source[0] != FALLBACK]
    fallback = [source for source in available if source[0] == FALLBACK]
    now = time.monotonic()
    end = now + deadline
    primary_end = now + deadline * 0.65 if fallback else end
    best = _race(text, primary, primary_end)
    if not best:
        best = _race(text, fallback, end)
    if not best:
        return None
    _LOCAL.source = best[1]
    if not custom and best[1] != FALLBACK:
        with _LOCK:
            if settings is _SETTINGS:
                if len(_CACHE) >= CACHE_SIZE:
                    _CACHE.clear()
                _CACHE[text] = (best[2], best[1])
    return best[2]


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
        print(f"{s}\n  -> {out}  [{last_source()}] ({time.time()-t0:.1f}s)")
