"""Local, escaped transactional email layouts; no images or tracking requests."""

from html import escape
from urllib.parse import urlsplit

FONT = "Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif"
TEXT = "#232823"
PAPER = "#f7f7f2"

ACCOUNT_MESSAGES = {
    "invite": {
        "heading": "你的店铺，准备好了。",
        "intro": "你已受邀成为店主。接受邀请后，设置账号密码，即可开始管理店铺。",
        "action": "接受店铺邀请",
        "lifetime": "24 小时",
        "preview": "接受店铺邀请，设置账号密码，开始管理你的店铺。",
        "ignore": "如果你不认识邀请方，可以忽略这封邮件。",
    },
    "register": {
        "heading": "确认邮箱，完成注册。",
        "intro": "请确认这是你的邮箱，完成 Extore 店铺账号注册。",
        "action": "确认我的邮箱",
        "lifetime": "30 分钟",
        "preview": "确认邮箱，完成 Extore 店铺账号注册。",
        "ignore": "如果不是你发起的注册，可以忽略这封邮件。",
    },
    "reset": {
        "heading": "重新设置你的密码。",
        "intro": "我们收到了此账号的密码重置请求。点击下方按钮，设置新的登录密码。",
        "action": "重置登录密码",
        "lifetime": "30 分钟",
        "preview": "使用这封邮件中的一次性链接，重新设置登录密码。",
        "ignore": "如果不是你发起的请求，请忽略这封邮件；你的密码不会因此改变。",
    },
}


def _url(value):
    """Only link to an absolute web URL, never turn email text into active HTML."""
    if not isinstance(value, str) or any(
        ord(char) <= 32 or ord(char) == 127 for char in value
    ):
        raise ValueError("Invalid mail action URL")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid mail action URL") from exc
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == 0
    ):
        raise ValueError("Invalid mail action URL")
    return escape(value, quote=True)


def _paragraph(text, *, secondary=False):
    color = "#555d55" if secondary else TEXT
    return (
        f'<p style="margin:0 0 20px;font-family:{FONT};font-size:16px;'
        f'line-height:1.75;color:{color};word-wrap:break-word;overflow-wrap:anywhere;">'
        + escape(text).replace("\n", "<br>")
        + "</p>"
    )


def _action(url, label):
    return (
        '<table role="presentation" border="0" cellspacing="0" cellpadding="0" '
        'style="margin:28px 0;width:100%;"><tr><td align="left">'
        '<table role="presentation" border="0" cellspacing="0" cellpadding="0">'
        '<tr><td align="center" bgcolor="#235b43" '
        'style="background-color:#235b43;border-radius:6px;mso-padding-alt:16px 24px;">'
        f'<a href="{_url(url)}" style="display:inline-block;padding:16px 24px;'
        f"font-family:{FONT};font-size:16px;font-weight:700;line-height:24px;"
        'color:#ffffff;text-decoration:none;border-radius:6px;">'
        + escape(label)
        + "</a></td></tr></table></td></tr></table>"
    )


def _link_fallback(url):
    return (
        '<p style="margin:0 0 8px;font-size:13px;line-height:1.7;color:#555d55;">'
        "按钮无法打开？复制完整链接到浏览器：</p>"
        f'<p style="margin:0;font-size:13px;line-height:1.8;word-break:break-all;">'
        f'<a href="{_url(url)}" style="color:#235b43;text-decoration:underline;">'
        + escape(url)
        + "</a></p>"
    )


def _layout(heading, content, *, preview="", footer=""):
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{escape(heading)}</title>
<style>
@media only screen and (max-width:600px) {{
  .mail-wrap {{ padding:20px 12px !important; }}
  .mail-content {{ padding:28px 24px !important; }}
  .mail-heading {{ font-size:26px !important; }}
}}
</style>
</head>
<body style="margin:0;padding:0;background-color:#181818;font-family:{FONT};color:{TEXT};">
<div aria-hidden="true" style="display:none;max-height:0;overflow:hidden;mso-hide:all;opacity:0;">{escape(preview)}</div>
<table role="presentation" border="0" cellspacing="0" cellpadding="0" width="100%" bgcolor="#181818" style="width:100%;background-color:#181818;">
<tr><td class="mail-wrap" align="center" style="padding:40px 20px;">
<!--[if mso]><table role="presentation" border="0" cellspacing="0" cellpadding="0" width="600"><tr><td><![endif]-->
<table role="presentation" border="0" cellspacing="0" cellpadding="0" width="100%" bgcolor="{PAPER}" style="width:100%;max-width:600px;background-color:{PAPER};">
<tr><td style="padding:28px 32px 0;font-family:{FONT};font-size:20px;font-weight:700;line-height:28px;color:{TEXT};">Extore <span style="color:#235b43;">· 兑所</span></td></tr>
<tr><td class="mail-content" style="padding:32px;background-color:{PAPER};">
<h1 class="mail-heading" style="margin:0 0 24px;font-family:{FONT};font-size:28px;font-weight:700;line-height:1.4;color:{TEXT};word-wrap:break-word;overflow-wrap:anywhere;">{escape(heading)}</h1>
{content}
<table role="presentation" border="0" cellspacing="0" cellpadding="0" width="100%" style="width:100%;margin:28px 0 24px;">
<tr><td style="height:1px;border-top:1px dashed #a5afa4;font-size:0;line-height:0;">&nbsp;</td></tr></table>
<p style="margin:0;font-family:{FONT};font-size:13px;line-height:1.8;color:#555d55;">{escape(footer)}</p>
</td></tr>
</table>
<!--[if mso]></td></tr></table><![endif]-->
<p style="margin:20px 0 0;font-family:{FONT};font-size:12px;line-height:1.8;color:#c6c9c2;">此邮件由 Extore 自动发送。</p>
</td></tr></table>
</body></html>"""


def account_message(kind, url, *, shop_name=None):
    """Render one known account ceremony without changing its token or expiry."""
    message = ACCOUNT_MESSAGES[kind]
    _url(url)
    content = ""
    if shop_name:
        content += _paragraph(f"店铺：{shop_name}")
    content += _paragraph(message["intro"])
    content += _action(url, message["action"])
    content += _paragraph(
        f"链接仅可使用一次，将在 {message['lifetime']}后失效。请勿转发。",
        secondary=True,
    )
    content += _link_fallback(url)
    return _layout(
        message["heading"],
        content,
        preview=message["preview"],
        footer=message["ignore"],
    )


def plain_message(subject, body):
    """Give existing/plain outbox messages a readable, escaped HTML alternative."""
    if body.startswith(subject + "\n\n"):
        body = body[len(subject) + 2 :]
    lines = []
    for line in body.splitlines():
        try:
            url = _url(line)
        except ValueError:
            lines.append(escape(line))
        else:
            lines.append(
                f'<a href="{url}" style="color:#235b43;'
                'text-decoration:underline;word-break:break-all;">'
                + escape(line)
                + "</a>"
            )
    content = (
        f'<p style="margin:0 0 20px;font-family:{FONT};font-size:16px;'
        f'line-height:1.75;color:{TEXT};word-wrap:break-word;overflow-wrap:anywhere;">'
        + "<br>".join(lines)
        + "</p>"
    )
    return _layout(subject, content)
