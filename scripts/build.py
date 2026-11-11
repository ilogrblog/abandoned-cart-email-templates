#!/usr/bin/env python3
"""Build the three abandoned cart emails.

    python3 scripts/build.py

Writes, for each email:
  dist/<name>.html and dist/<name>.txt     templates with runtime {{ placeholders }} left in, for your platform
  out/<variant>/<name>.html, .txt, .eml    the same emails rendered with sample/cart.json, for previews and tests

Python 3.9 or later, standard library only.
"""
import html
import json
import re
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent

# One entry per email. 'timing' is documentation only: your scheduler decides when each one goes out.
STEPS = [
    {
        "name": "email-1-reminder",
        "step": "step1",
        "timing": "about 1 hour after the cart was last changed",
        "subject": "You left something in your cart",
        "preheader": "Your cart at {{ store_name }} is saved. Pick up where you stopped.",
        "button_label": "Return to your cart",
    },
    {
        "name": "email-2-questions",
        "step": "step2",
        "timing": "about 24 hours",
        "subject": "Questions before you check out?",
        "preheader": "Delivery, returns and help, in one place.",
        "button_label": "Review your cart",
    },
    {
        "name": "email-3-last-reminder",
        "step": "step3",
        "timing": "about 48 hours",
        "subject": "Last reminder about your cart",
        "preheader": "This is the last email about this cart.",
        "button_label": "Complete your order",
    },
]

UTM_SOURCE = "cart_flow"
UTM_MEDIUM = "email"
UTM_CAMPAIGN = "abandoned_cart"

INCLUDE = re.compile(r"\{\{>\s*(\w+)\s*\}\}")
SECTION = re.compile(r"\{\{#(\w+)\}\}(.*?)\{\{/\1\}\}", re.S)
VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def add_utm(url, step, link):
    """Append UTM parameters to a link that points from the email to your own site.

    Keeps any query string the URL already has (a restore token, for example) and never overwrites it.
    Values are lowercase with underscores, one utm_content per link, so reports do not split.
    """
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    existing = {k for k, _ in query}
    for key, value in (
        ("utm_source", UTM_SOURCE),
        ("utm_medium", UTM_MEDIUM),
        ("utm_campaign", UTM_CAMPAIGN),
        ("utm_content", f"{step}_{link}"),
    ):
        if key not in existing:
            query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def read(path):
    return (ROOT / "templates" / path).read_text(encoding="utf-8")


def resolve_includes(text, ext):
    return INCLUDE.sub(lambda m: read(f"partials/{m.group(1)}.{ext}"), text)


def compose(step, ext):
    """Join base + body + partials and fill the static values. Runtime placeholders stay in place."""
    body = resolve_includes(read(f"{step['name']}.{ext}"), ext)
    text = read(f"base.{ext}").replace("{{ body }}", body)
    static = {k: step[k] for k in ("subject", "preheader", "button_label")}

    def fill(match):
        key = match.group(1)
        if key in static:
            value = static[key]
            return html.escape(value, quote=True) if ext == "html" else value
        return match.group(0)

    # The preheader contains a runtime placeholder, so fill the static values twice.
    return VAR.sub(fill, VAR.sub(fill, text))


def render(text, context, ext):
    """Render sections and variables. A missing variable raises KeyError, so a typo cannot ship."""

    def section(match, ctx):
        key, inner = match.group(1), match.group(2)
        value = ctx[key]
        if isinstance(value, list):
            return "".join(render_with(inner, {**ctx, **item}) for item in value)
        return render_with(inner, ctx) if value else ""

    def render_with(chunk, ctx):
        while SECTION.search(chunk):
            chunk = SECTION.sub(lambda m: section(m, ctx), chunk, count=1)

        def var(match):
            value = str(ctx[match.group(1)])
            return html.escape(value, quote=True) if ext == "html" else value

        return VAR.sub(var, chunk)

    return render_with(text, context)


def tidy_blank_lines(text):
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def context_for(step, data, variant):
    common = dict(data["common"])
    base = common["base_url"]
    common["url_shop"] = add_utm(base + "/", step["step"], "shop")
    common["url_restore"] = add_utm(base + common["restore_path"], step["step"], "restore")
    common["url_returns"] = add_utm(base + common["returns_path"], step["step"], "returns")
    common.update(data["variants"][variant])
    return common


def build_mime(step, html_body, text_body, context):
    msg = EmailMessage()
    msg["From"] = f"{context['store_name']} <orders@example.com>"
    msg["To"] = "customer@example.net"
    msg["Subject"] = step["subject"]
    msg["Date"] = formatdate(1795000000, usegmt=True)  # fixed, so builds are repeatable
    msg["Message-ID"] = make_msgid(idstring=step["name"], domain="example.com")
    unsubscribe = context["unsubscribe_url"]
    msg["List-Unsubscribe"] = f"<{unsubscribe}>, <mailto:unsubscribe@example.com?subject=unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text_body, subtype="plain", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")
    return msg


def main():
    data = json.loads((ROOT / "sample" / "cart.json").read_text(encoding="utf-8"))
    (ROOT / "dist").mkdir(exist_ok=True)
    for step in STEPS:
        for ext in ("html", "txt"):
            (ROOT / "dist" / f"{step['name']}.{ext}").write_text(compose(step, ext), encoding="utf-8")
    for variant in data["variants"]:
        out = ROOT / "out" / variant
        out.mkdir(parents=True, exist_ok=True)
        for step in STEPS:
            ctx = context_for(step, data, variant)
            rendered = {}
            for ext in ("html", "txt"):
                rendered[ext] = tidy_blank_lines(render(compose(step, ext), ctx, ext)) if ext == "txt" else render(compose(step, ext), ctx, ext)
                (out / f"{step['name']}.{ext}").write_text(rendered[ext], encoding="utf-8")
            eml = build_mime(step, rendered["html"], rendered["txt"], ctx)
            (out / f"{step['name']}.eml").write_bytes(bytes(eml))
    print("built", len(STEPS), "emails x", len(data["variants"]), "variants")


if __name__ == "__main__":
    sys.exit(main())
