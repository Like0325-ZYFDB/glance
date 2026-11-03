# -*- coding: utf-8 -*-
"""翻译调度测试：用假来源，不联网。"""
import threading
import time

from glance import translate as tr


def _slow_fail(text):
    time.sleep(0.6)
    raise TimeoutError


def _ok(text):
    return "好的结果"


def _setup():
    tr._DOWN.clear()
    tr._CACHE.clear()
    tr._WARM["at"] = 0.0
    tr.configure("", "")


def test_does_not_wait_for_slow_failing_source():
    _setup()
    srcs = [("谷歌翻译", _slow_fail), ("MyMemory", _ok)]
    t0 = time.time()
    assert tr.translate("hello", srcs) == "好的结果"
    assert time.time() - t0 < tr.GRACE + 0.15     # 有结果后最多再等更优先的来源 GRACE 秒
    assert tr.last_source() == "MyMemory"
    time.sleep(0.4)                                # 谷歌稍后失败，记下来
    assert "谷歌翻译" in tr._DOWN
    t0 = time.time()
    assert tr.translate("hello", srcs) == "好的结果"
    assert time.time() - t0 < 0.15                 # 之后不再等已知连不上的来源


def test_prefers_higher_priority_within_grace():
    _setup()

    def fast_low(text):
        return "兜底结果"

    def a_bit_slower_high(text):
        time.sleep(0.15)
        return "优先结果"

    assert tr.translate("hello", [("必应翻译", a_bit_slower_high), ("MyMemory", fast_low)]) == "优先结果"
    assert tr.last_source() == "必应翻译"


def test_all_fail_returns_none():
    _setup()
    assert tr.translate("hello", [("MyMemory", lambda t: t)]) is None   # 原样返回算失败


def test_baidu_skipped_without_key():
    _setup()
    called = []
    srcs = [("百度翻译", lambda t: called.append(1) or "x"), ("MyMemory", _ok)]
    assert tr.translate("hello", srcs) == "好的结果" and not called


def test_cache_and_thread_local_source(monkeypatch):
    _setup()
    calls = []

    def src(text):
        calls.append(text)
        return "译文"

    monkeypatch.setattr(tr, "SOURCES", [("必应翻译", src)])
    assert tr.translate("cache me") == "译文" and tr.last_source() == "必应翻译"
    assert tr.translate("cache me") == "译文" and calls == ["cache me"]   # 第二次走缓存
    seen = []
    t = threading.Thread(target=lambda: seen.append(tr.last_source()))
    t.start()
    t.join()
    assert seen == [""]                     # 别的线程看不到这个线程的来源


class _Resp:
    def __init__(self, text="", url="https://www.bing.com/translator", data=None):
        self.text, self.url, self._data = text, url, data

    def json(self):
        if self._data is None:
            raise ValueError("empty")
        return self._data


PAGE = ('<div id="rich_tta" data-iid="translator.5028"></div><script>IG:"ABC123",'
        'var params_AbusePreventionHelper = [1700000000000,"TOKEN1",3600000];</script>')


def test_bing_parses_page_and_refreshes_on_failure(monkeypatch):
    _setup()
    monkeypatch.setattr(tr, "_BING", {"until": 0.0})
    pages, posts = [], []

    class FakeSession:
        def get(self, url, timeout=None):
            pages.append(url)
            return _Resp(PAGE)

        def post(self, url, params=None, data=None, headers=None, timeout=None):
            posts.append((url, params, data))
            if len(posts) == 1:
                return _Resp("")    # 第一次被拒（参数过期）：应重新取参数再试
            return _Resp(data=[{"translations": [{"text": "你好，世界。"}]}])

    monkeypatch.setattr(tr, "_SESSION", FakeSession())
    assert tr._bing("Hello, world.") == "你好，世界。"
    assert len(pages) == 2                   # 第一次取参数 + 被拒后重新取
    url, params, data = posts[-1]
    assert url == "https://www.bing.com/ttranslatev3" and params["IG"] == "ABC123"
    assert data["token"] == "TOKEN1" and data["key"] == "1700000000000" and data["to"] == "zh-Hans"
    assert params["IID"].startswith("translator.5028.")
    tr._bing("你好")                         # 中文翻成英文
    assert posts[-1][2]["to"] == "en"


def test_warm_is_rate_limited(monkeypatch):
    _setup()
    warmed = []
    monkeypatch.setattr(tr, "_warm_one", lambda name: warmed.append(name))
    tr.warm()
    tr.warm()                                # 30 秒内第二次不再预热
    time.sleep(0.2)
    assert sorted(warmed) == sorted(["必应翻译", "谷歌翻译", "MyMemory"])   # 没填百度密钥：不连百度


def test_chunks():
    text = "This is one sentence. " * 60
    parts = tr._chunks(text)
    assert all(len(p) <= 450 for p in parts) and len(parts) >= 2
