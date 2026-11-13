# -*- coding: utf-8 -*-
"""只在 Windows 上跑：系统文字识别真的能认出屏幕上的词、取词服务能启动、高亮窗口能建。"""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上跑")


def _text_image(text, size=22):
    from PIL import Image, ImageDraw, ImageFont
    font = None
    for fp in (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"):
        try:
            font = ImageFont.truetype(fp, size)
            break
        except OSError:
            continue
    img = Image.new("RGB", (560, 60), "white")
    d = ImageDraw.Draw(img)
    d.text((10, 14), text, font=font, fill="black")
    return img, d, font


def test_winocr_gives_word_boxes():
    from glance.capture.geometry import pick_word
    from glance.capture.winocr import WinOcr
    try:
        ocr = WinOcr()
    except RuntimeError as e:
        pytest.skip(f"这台机器没有文字识别语言：{e}")
    text = "They had to abandon the car and walk"
    img, d, font = _text_image(text)
    img = img.resize((img.width * 2, img.height * 2))
    words = [w.shifted(0, 0, 2) for w in ocr.words(img)]
    print("识别结果:", [(w.text, w.rect()) for w in words], "语言:", ocr.language)
    assert any(w.text.lower() == "abandon" for w in words)
    left = 10 + d.textlength("They had to ", font=font)
    width = d.textlength("abandon", font=font)
    hit = pick_word(words, left + width / 2, 30)
    assert hit is not None and hit.text.lower() == "abandon"


def test_capture_service_starts():
    from glance.capture.client import CaptureClient
    c = CaptureClient()
    try:
        print("取词服务状态:", c.status)
        assert c.status, "取词服务没有发出就绪消息"
        r = c.word_at(5, 5)
        assert isinstance(r, dict)
    finally:
        c.close()


def test_highlight_window():
    from glance.overlay import Highlight
    h = Highlight(log=print)
    assert h.hwnd
    h.show((100, 100, 160, 120))
    h.hide()


def test_hotkey_parse():
    from glance.hotkeys import MOD_ALT, MOD_CONTROL, parse_hotkey, valid_hotkey
    mods, vk = parse_hotkey("ctrl+alt+d")
    assert mods & MOD_CONTROL and mods & MOD_ALT and vk == 0x44
    assert valid_hotkey("ctrl+alt+f5") and not valid_hotkey("shift+d") and not valid_hotkey("ctrl+alt")


def test_translate_word_fallback(monkeypatch):
    """词典里没有的词：联网翻译一次后缓存；原样返回、关了联网翻译、中文都不给结果。"""
    from glance import app, translate
    calls = []

    def fake(text, deadline=10.0):
        calls.append(text)
        return {"serendipity": "意外发现珍宝的运气", "xyzzy": "xyzzy"}.get(text)

    monkeypatch.setattr(translate, "translate", fake)
    monkeypatch.setattr(translate, "last_source", lambda: "测试")
    fake_app = _fake_float_app(app)
    tw = fake_app.translate_word
    assert tw("serendipity") == ("意外发现珍宝的运气", "测试")
    assert tw("Serendipity") == ("意外发现珍宝的运气", "测试") and calls == ["serendipity"]  # 走缓存
    assert tw("xyzzy") is None  # 原样返回算翻不出来
    assert tw("中文") is None
    fake_app.cfg["online_translate"] = False
    assert tw("hello") is None and "hello" not in calls


def _fake_float_app(app, **kw):
    """只带浮窗状态的假 App：用来测取词后台填翻译这类逻辑，不建真窗口。"""
    import threading
    from types import SimpleNamespace
    shown, hidden = [], []
    fake = SimpleNamespace(cfg={"online_translate": True}, _word_tr={}, _float_lock=threading.Lock(),
                           hover=SimpleNamespace(visible=True, pinned=False), float_word="xenial",
                           float_box=(1, 2, 3, 4), shown=shown, hidden=hidden,
                           show_float=lambda d, b: shown.append((d, b)), hide_float=lambda: hidden.append(1))
    fake.translate_word = lambda w: app.App.translate_word(fake, w)
    fake.can_translate_word = lambda w: app.App.can_translate_word(fake, w)
    for k, v in kw.items():
        setattr(fake, k, v)
    return fake


def test_fill_online_updates_float(monkeypatch):
    from glance import app, translate
    monkeypatch.setattr(translate, "translate", lambda t, deadline=10.0: "好客的")
    monkeypatch.setattr(translate, "last_source", lambda: "测试")
    fake = _fake_float_app(app)
    data = {"query": "xenial", "card": None, "online": {"loading": True}}
    app.App._fill_online(fake, "xenial", data, (1, 2, 3, 4), False)
    assert fake.shown and fake.shown[0][0]["online"] == {"text": "好客的", "source": "测试"}
    # 浮窗已经换了别的词：不覆盖
    fake = _fake_float_app(app, float_word="other")
    app.App._fill_online(fake, "xenial", data, (1, 2, 3, 4), False)
    assert not fake.shown and not fake.hidden


def test_fill_online_failure(monkeypatch):
    from glance import app, translate
    monkeypatch.setattr(translate, "translate", lambda t, deadline=10.0: None)
    data = {"query": "xenial", "card": None, "online": {"loading": True}}
    fake = _fake_float_app(app)
    app.App._fill_online(fake, "xenial", data, (1, 2, 3, 4), False)
    assert fake.hidden and not fake.shown  # 按住键扫过、翻不出来：收起
    fake = _fake_float_app(app)
    app.App._fill_online(fake, "xenial", data, (1, 2, 3, 4), True)
    assert fake.shown and fake.shown[0][0]["online"] is None and "没有结果" in fake.shown[0][0]["status"]


def test_translate_selection_quiet(monkeypatch):
    """按住划词键拖动后自动翻译：读不到文字时不弹提示；按快捷键时照常提示。"""
    from types import SimpleNamespace

    from glance import app
    monkeypatch.setattr(app, "copy_selection_text", lambda: None)
    notes = []
    fake = SimpleNamespace(capture=None, notify=notes.append, show_translate=lambda t: notes.append("shown"),
                           cfg={"hotkeys": {"translate": "ctrl+alt+t"}})
    app.App.translate_selection(fake, quiet=True)
    assert notes == []
    app.App.translate_selection(fake)
    assert len(notes) == 1 and "没读到" in notes[0]


def test_fill_online_unchanged_says_so(monkeypatch):
    """翻译服务把词原样返回（专有名词）：手动取词时说明不用翻译，而不是「没有结果」。"""
    from glance import app, translate
    monkeypatch.setattr(translate, "translate", lambda t, deadline=10.0: None)
    monkeypatch.setattr(translate, "last_status", lambda: "unchanged")
    data = {"query": "xenial", "card": None, "online": {"loading": True}}
    fake = _fake_float_app(app)
    app.App._fill_online(fake, "xenial", data, (1, 2, 3, 4), True)
    assert fake.shown and "专有名词" in fake.shown[0][0]["status"]


def test_show_translate_explains_missing_translation(monkeypatch):
    """翻译窗口：没有译文时按原因给提示。"""
    import threading
    from types import SimpleNamespace

    from glance import app, translate
    rendered = []
    fake = SimpleNamespace(
        _translate_lock=threading.Lock(), _translate_seq=0, hover=SimpleNamespace(visible=False),
        cfg={"online_translate": True}, engine=None, prefs=lambda: {},
        wm=SimpleNamespace(call=lambda name, func, data: rendered.append(data), show_window=lambda name: None))
    monkeypatch.setattr(translate, "translate", lambda t: None)
    monkeypatch.setattr(translate, "last_source", lambda: "必应翻译")
    monkeypatch.setattr(translate, "last_status", lambda: "unchanged")
    app.App.show_translate(fake, "ChatGPT")
    assert rendered[0]["loading"] and "不用翻译" in rendered[-1]["error"]
    monkeypatch.setattr(translate, "last_status", lambda: "failed")
    app.App.show_translate(fake, "hello world")
    assert "没取到译文" in rendered[-1]["error"]
    monkeypatch.setattr(translate, "translate", lambda t: "你好，世界")
    app.App.show_translate(fake, "hello world")
    assert rendered[-1]["translated"] == "你好，世界" and "error" not in rendered[-1]
