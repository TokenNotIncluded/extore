"""Transactional email must stay readable without loading remote resources."""

from html.parser import HTMLParser

import pytest

from extore.mail_templates import account_message, plain_message

URL = "https://extore.example.test/account/invite#synthetic-token&quote=%22"


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.links = []
        self.text = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "a":
            self.links.append(dict(attrs)["href"])

    def handle_data(self, data):
        self.text.append(data)


@pytest.mark.parametrize(
    ("kind", "heading", "action", "lifetime"),
    [
        ("invite", "你的店铺，准备好了。", "接受店铺邀请", "24 小时"),
        ("register", "确认邮箱，完成注册。", "确认我的邮箱", "30 分钟"),
        ("reset", "重新设置你的密码。", "重置登录密码", "30 分钟"),
    ],
)
def test_account_templates_explain_the_specific_action_and_expiry(
    kind, heading, action, lifetime
):
    html = account_message(kind, URL, shop_name="测试店铺")
    document = Document(html)
    text = "".join(document.text)
    assert heading in text and action in text and lifetime in text
    assert "测试店铺" in text and "仅可使用一次" in text and "请勿转发" in text
    assert "复制完整链接到浏览器" in text and URL in text
    assert document.links == [URL, URL]
    assert "max-width:600px" in html and "@media" in html


def test_merchant_names_are_text_and_cannot_inject_html_or_remote_images():
    name = '"><img src="https://evil.example.test/pixel" onerror="steal()"> & 店铺'
    document = Document(account_message("invite", URL, shop_name=name))
    assert name in "".join(document.text)
    assert not any(
        tag in {"img", "script", "iframe", "object"} for tag, _ in document.tags
    )
    assert document.links == [URL, URL]
    assert not any(
        attr in {"src", "srcset", "background"} or attr.startswith("on")
        for _, attrs in document.tags
        for attr in attrs
    )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hello",
        "//example.test/account#token",
        "https://user:password@example.test/account#token",
        "https://example.test:0/account#token",
        "https://example.test:99999/account#token",
        "https://example.test/account#token\nBcc:attacker@example.test",
    ],
)
def test_action_links_reject_non_web_urls_credentials_and_control_characters(url):
    with pytest.raises(ValueError, match="Invalid mail action URL"):
        account_message("invite", url)


def test_action_url_attribute_is_escaped_without_changing_the_destination():
    url = 'https://extore.example.test/account/reset#token&"quoted"'
    html = account_message("reset", url)
    assert "&amp;&quot;quoted&quot;" in html
    assert Document(html).links == [url, url]


def test_plain_outbox_fallback_preserves_content_and_only_links_standalone_web_urls():
    subject = "Title <script>"
    body = subject + "\n\nHello <b>friend</b>\n" + URL + "\n\njavascript:alert(1)"
    document = Document(plain_message(subject, body))
    text = "".join(document.text)
    assert subject in text and "Hello <b>friend</b>" in text
    assert "javascript:alert(1)" in text
    assert not any(tag in {"script", "b"} for tag, _ in document.tags)
    assert document.links == [URL]


def test_email_markup_contains_no_remote_images_scripts_fonts_or_tracking_requests():
    html = account_message("register", URL)
    document = Document(html)
    assert not any(
        tag in {"img", "script", "link", "iframe", "form"} for tag, _ in document.tags
    )
    assert "url(" not in html and "@import" not in html
    assert all(
        attrs.get("role") == "presentation"
        for tag, attrs in document.tags
        if tag == "table"
    )


def test_reset_does_not_claim_the_password_has_already_changed():
    document = Document(account_message("reset", URL))
    assert "你的密码不会因此改变" in "".join(document.text)
