# -*- coding: utf-8 -*-
"""翻译调度测试：用假来源，不联网。"""
import threading
import time

from glance import translate as tr


def _ok(text):
    return "好的结果"


def _setup():
    tr._DOWN.clear()
    tr._CACHE.clear()
    tr._WARM["at"] = 0.0
    tr.configure("", "")


def test_fallback_waits_for_primary_budget():
    _setup()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def unavailable(text):
        started.set()
        release.wait(3)
        finished.set()
        raise TimeoutError

    def fallback(text):
        assert started.is_set() and not finished.is_set()
        return _ok(text)

    srcs = [("谷歌翻译", unavailable), ("MyMemory", fallback)]
    try:
        assert tr.translate("hello", srcs, deadline=1.0) == "好的结果"
    finally:
        release.set()
    assert tr.last_source() == "MyMemory"


def test_prefers_higher_priority_within_grace():
    _setup()

    def fast_low(text):
        return "兜底结果"

    def a_bit_slower_high(text):
        time.sleep(0.15)
        return "优先结果"

    assert tr.translate("hello", [("必应翻译", a_bit_slower_high), ("谷歌翻译", fast_low)]) == "优先结果"
    assert tr.last_source() == "必应翻译"


def test_primary_avoids_fallback_and_fallback_is_not_cached(monkeypatch):
    _setup()
    primary, fallback = [], []

    def fast_fallback(text):
        fallback.append(text)
        return "兜底结果"

    def google(text):
        primary.append(text)
        return "优先结果" if len(primary) > 1 else "ENGLISH RESPONSE"

    monkeypatch.setattr(tr, "SOURCES", [("谷歌翻译", google), ("MyMemory", fast_fallback)])
    assert tr.translate("hello") == "兜底结果"
    assert tr.translate("hello") == "优先结果"
    assert tr.translate("hello") == "优先结果"
    assert primary == ["hello", "hello"] and fallback == ["hello"]


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
    status_code = 200

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
    assert sorted(warmed) == sorted(["必应翻译", "谷歌翻译"])


def test_chunks():
    text = "This is one sentence. " * 60
    parts = tr._chunks(text)
    assert all(len(p) <= 450 for p in parts) and len(parts) >= 2
    assert "".join(parts) == text
    text = "这是一段中文。\n\n" * 240
    parts = tr._chunks(text, size=500, utf8=True)
    assert len(parts) > 6 and all(len(part.encode("utf-8")) <= 500 for part in parts)
    assert "".join(parts) == text


def test_mixed_language_direction_and_wrong_language_results():
    _setup()
    assert not tr._is_chinese("Keep the label 中文 in this sentence.")
    assert tr._is_chinese("使用 GitHub 提交这次更新。")
    assert not tr._is_chinese("翻訳をお願いします。")
    assert tr.translate("这段是中文。", [("必应翻译", lambda t: "另一段中文。")]) is None
    assert tr.translate("这段是中文。", [("谷歌翻译", lambda t: "This is Chinese.")]) == "This is Chinese."


def test_baidu_translates_beyond_first_chunk(monkeypatch):
    _setup()
    tr.configure("test-app", "test-key")
    calls = []

    class FakeSession:
        def post(self, url, data, timeout):
            calls.append(data)
            return _Resp(data={"trans_result": [{"dst": f"译文{len(calls)}"}]})

    monkeypatch.setattr(tr, "_SESSION", FakeSession())
    text = "Read this whole sentence. " * 110 + "End."
    assert tr._baidu(text) == "译文1译文2"
    assert len(calls) == 2 and calls[-1]["q"].endswith("End.")
    assert {call["to"] for call in calls} == {"zh"}


def test_mymemory_rejects_quota_response_even_when_http_succeeds(monkeypatch):
    _setup()

    class FakeSession:
        def get(self, url, params, timeout):
            return _Resp(data={"responseStatus": 429, "responseData": {"translatedText": "额度已用完"}})

    monkeypatch.setattr(tr, "_SESSION", FakeSession())
    assert tr._mymemory("A sample sentence.") is None
