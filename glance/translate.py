# -*- coding: utf-8 -*-
"""整句翻译：多个来源同时请求，谁先成功用谁。

来源（优先级从高到低）：
  - 百度翻译开放平台（需在设置里填 APP ID / 密钥，国内稳定，标准版每月有免费额度）
  - 必应翻译网页端点（无需密钥，国内能直连）
  - 谷歌免费端点（无需密钥；国内网络通常连不上）
  - MyMemory 免费接口（无需密钥，每日额度，作兜底）

几个来源并发请求，第一个成功的结果出来后，最多再等 GRACE 秒看更优先的来源有没有结果，
不再干等连不上的来源超时；连不上的来源会被记下，之后 10 分钟内不再等它（仍会顺带请求，恢复了自动用回来）。
所有请求共用一个会话（复用 HTTPS 连接：新建一次连接要一两秒）；同一段文字翻过就记住。
选文字时调用 warm() 提前连好，松开鼠标真要翻译时连接已经在了。

translate(text) -> str | None   中英自动识别方向
"""
from __future__ import annotations

import hashlib
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
GRACE = 0.4               # 有结果后，最多再等更优先的来源多久
WARM_INTERVAL = 30        # 预热连接的最短间隔（秒）
CACHE_SIZE = 300

CJK_RE = re.compile(r"[一-鿿]")

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
    _SETTINGS["baidu_appid"] = (baidu_appid or "").strip()
    _SETTINGS["baidu_key"] = (baidu_key or "").strip()


def last_source() -> str:
    """本线程最近一次翻译用的来源名（给界面显示）。"""
    return getattr(_LOCAL, "source", "")


def _is_chinese(text: str) -> bool:
    return bool(CJK_RE.search(text))


def _baidu(text: str) -> str | None:
    appid, key = _SETTINGS["baidu_appid"], _SETTINGS["baidu_key"]
    if not appid or not key:
        return None
    q = text[:2000]
    salt = str(random.randint(10000, 99999))
    sign = hashlib.md5(f"{appid}{q}{salt}{key}".encode("utf-8")).hexdigest()
    r = _SESSION.post(
        "https://fanyi-api.baidu.com/api/trans/vip/translate",
        data={"q": q, "from": "auto", "to": "en" if _is_chinese(q) else "zh",
              "appid": appid, "salt": salt, "sign": sign},
        timeout=TIMEOUT,
    )
    data = r.json()
    if "trans_result" not in data:
        raise RuntimeError(f"百度翻译返回错误 {data.get('error_code')}: {data.get('error_msg')}")
    return "\n".join(seg.get("dst", "") for seg in data["trans_result"])


def _google(text: str) -> str | None:
    """谷歌免费端点（无需密钥）。"""
    src, dst = ("zh-CN", "en") if _is_chinese(text) else ("auto", "zh-CN")
    r = _SESSION.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": src, "tl": dst, "dt": "t", "q": text[:4500]},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        return None
    data = r.json()
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def _chunks(text: str, size: int = 450, max_chunks: int = 6) -> list[str]:
    """按句子切成不超过 size 字符的小段（MyMemory 单次最多 500 字符）。"""
    out, cur = [], ""
    for sent in re.split(r"(?<=[.!?。！？;；])\s*", text):
        while len(sent) > size:
            out.append(sent[:size])
            sent = sent[size:]
        if len(cur) + len(sent) + 1 > size and cur:
            out.append(cur)
            cur = ""
        cur = (cur + " " + sent).strip() if cur else sent
    if cur:
        out.append(cur)
    return out[:max_chunks]


def _mymemory(text: str) -> str | None:
    """MyMemory 免费接口（每日额度，作备用）。"""
    pair = "zh-CN|en" if _is_chinese(text) else "en|zh-CN"
    parts = []
    for chunk in _chunks(text):
        r = _SESSION.get(
            "https://api.mymemory.translated.net/get",
            params={"q": chunk, "langpair": pair},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return None
        parts.append(r.json()["responseData"]["translatedText"])
    sep = "" if not _is_chinese(text) else " "
    return sep.join(parts)


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
    for attempt in range(2):
        p = _bing_params(refresh=attempt > 0)
        parts = []
        for chunk in _chunks(text, size=900):
            r = _SESSION.post(
                f"https://{p['host']}/ttranslatev3",
                params={"isVertical": "1", "IG": p["ig"], "IID": f"{p['iid']}.{p['n']}"},
                data={"fromLang": "auto-detect", "to": "en" if zh else "zh-Hans", "text": chunk,
                      "token": p["token"], "key": p["key"]},
                headers={"Referer": f"https://{p['host']}/translator"},
                timeout=TIMEOUT,
            )
            try:
                parts.append(r.json()[0]["translations"][0]["text"])
            except Exception:
                parts = None
                break
        if parts:
            return (" " if zh else "").join(parts)
    return None


SOURCES = [("百度翻译", _baidu), ("必应翻译", _bing), ("谷歌翻译", _google), ("MyMemory", _mymemory)]
# 预热时连哪里（必应要先取参数，取参数本身就连上了）
_WARM_URLS = {"百度翻译": "https://fanyi-api.baidu.com/", "谷歌翻译": "https://translate.googleapis.com/",
              "MyMemory": "https://api.mymemory.translated.net/"}


def _run(name: str, fn, text: str) -> str | None:
    try:
        out = fn(text)
    except Exception:
        out = None
    out = (out or "").strip()
    with _LOCK:
        if out and out != text:
            _DOWN.pop(name, None)
            return out
        if name != "百度翻译" or _SETTINGS["baidu_appid"]:
            _DOWN[name] = time.time()
    return None


def _usable(sources):
    if not (_SETTINGS["baidu_appid"] and _SETTINGS["baidu_key"]):
        sources = [s for s in sources if s[0] != "百度翻译"]
    return sources


def _down_now() -> set[str]:
    now = time.time()
    with _LOCK:
        return {n for n, t in _DOWN.items() if now - t < DOWN_SECONDS}


def translate(text: str, sources=None, deadline: float = 10.0) -> str | None:
    """整段翻译，自动识别中英方向；全部失败返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    custom = sources is not None
    if not custom and text in _CACHE:
        _LOCAL.source = _CACHE[text][1]
        return _CACHE[text][0]
    sources = _usable(sources or SOURCES)
    down = _down_now()
    futures = {_POOL.submit(_run, name, fn, text): (i, name) for i, (name, fn) in enumerate(sources)}
    end = time.time() + deadline
    pending, best, grace_end = set(futures), None, None
    while pending:
        left = (min(end, grace_end) if grace_end else end) - time.time()
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
            grace_end = grace_end or time.time() + GRACE
    if not best:
        _LOCAL.source = ""
        return None
    _LOCAL.source = best[1]
    if not custom:
        with _LOCK:
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
        if name not in down:
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
