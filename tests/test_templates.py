"""Checks for the built emails. Run from the repository root:

    python3 -m unittest discover -s tests -v

These checks cover what can be tested without a mail client: structure, inlined CSS, size, links, UTM
parameters, colour contrast, the plain-text part and the MIME message. They do not prove how Outlook,
Gmail or any other client draws the email. See README.md, "What is and is not tested".
"""
import email
import json
import re
import subprocess
import sys
import unittest
from email import policy
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build  # noqa: E402

# Every colour the templates may use, with what it is for. A new colour must be added here, which forces a contrast check.
PALETTE = {
    "#f3efe8": "page", "#ffffff": "card", "#1f2430": "ink", "#5a5f6b": "muted", "#e6e1d8": "rule",
    "#9a2a1f": "accent", "#eee9e0": "image placeholder",
    "#14161a": "dark page", "#1e2128": "dark card", "#f2f3f5": "dark ink", "#b9bfcb": "dark muted",
    "#3a3f4b": "dark rule", "#ff9a8a": "dark accent", "#2a0d08": "dark button text", "#ffb4a8": "dark link",
}
# (foreground, background, minimum ratio). Text needs 4.5 (WCAG 2.2 AA), borders are not text.
CONTRAST_PAIRS = [
    ("#1f2430", "#ffffff"), ("#1f2430", "#f3efe8"), ("#5a5f6b", "#f3efe8"), ("#5a5f6b", "#ffffff"),
    ("#ffffff", "#9a2a1f"), ("#9a2a1f", "#ffffff"),
    ("#f2f3f5", "#1e2128"), ("#f2f3f5", "#14161a"), ("#b9bfcb", "#1e2128"), ("#b9bfcb", "#14161a"),
    ("#2a0d08", "#ff9a8a"), ("#ffb4a8", "#1e2128"),
]
RUNTIME_PLACEHOLDERS = {
    "store_name", "postal_address", "support_email", "greeting", "unsubscribe_url", "subtotal", "url_shop",
    "url_restore", "url_returns", "title", "variant", "quantity", "price", "image_url", "delivery_promise",
    "returns_summary", "review_quote", "review_attribution", "hold_note", "low_stock_note", "incentive_text",
    "incentive_code", "incentive_terms", "items",
}
BANNED_IN_HTML = [
    r"display\s*:\s*(flex|grid)", r"<picture", r"<video", r"<script", r"<form", r"<link\b", r"@import",
    r"background-image", r"position\s*:", r"float\s*:", r"calc\(", r"var\(", r"<iframe", r"@font-face",
    r"display\s*:\s*none", r"visibility\s*:\s*hidden", r"font-size\s*:\s*0\b", r"mso-hide",
]


def luminance(hex_colour):
    h = hex_colour.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.stack = []
        self.errors = []
        self.text = []
        self.in_style = False

    VOID = {"meta", "img", "br", "hr", "input", "link"}

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag not in self.VOID:
            self.stack.append(tag)
        if tag == "style":
            self.in_style = True

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}> (open: {self.stack[-3:]})")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()

    def handle_data(self, data):
        if not self.in_style and data.strip():
            self.text.append(data.strip())


def parse(path):
    c = Collector()
    c.feed(Path(path).read_text(encoding="utf-8"))
    return c


class BuiltEmails(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build.main()
        cls.data = json.loads((ROOT / "sample" / "cart.json").read_text(encoding="utf-8"))
        cls.names = [s["name"] for s in build.STEPS]
        cls.variants = list(cls.data["variants"])

    def rendered(self):
        for v in self.variants:
            for n in self.names:
                yield v, n, ROOT / "out" / v / f"{n}.html"

    # --- structure -------------------------------------------------------------------------------------------
    def test_no_unresolved_placeholders_in_rendered_output(self):
        for _, _, path in self.rendered():
            for ext in ("html", "txt"):
                text = path.with_suffix("." + ext).read_text(encoding="utf-8")
                self.assertNotRegex(text, r"\{\{|\}\}", str(path))

    def test_dist_templates_only_use_known_placeholders(self):
        for n in self.names:
            for ext in ("html", "txt"):
                text = (ROOT / "dist" / f"{n}.{ext}").read_text(encoding="utf-8")
                found = set(re.findall(r"\{\{[#/]?\s*(\w+)\s*\}\}", text))
                self.assertFalse(found - RUNTIME_PLACEHOLDERS, f"{n}.{ext}: {found - RUNTIME_PLACEHOLDERS}")

    def test_html_tags_balance_and_basics(self):
        for v, n, path in self.rendered():
            c = parse(path)
            self.assertEqual(c.errors, [], f"{v}/{n}")
            tags = [t for t, _ in c.tags]
            self.assertEqual(tags.count("h1"), 1, f"{v}/{n}: exactly one h1")
            html_attrs = dict(c.tags[0][1]) if c.tags[0][0] == "html" else {}
            self.assertEqual(html_attrs.get("lang"), "en")
            self.assertIn("title", tags)
            for t, a in c.tags:
                if t == "table":
                    self.assertEqual(a.get("role"), "presentation", f"{v}/{n}: layout table needs role=presentation")

    def test_optional_blocks_follow_data(self):
        full = (ROOT / "out/full/email-3-last-reminder.html").read_text(encoding="utf-8")
        minimal = (ROOT / "out/minimal/email-3-last-reminder.html").read_text(encoding="utf-8")
        self.assertIn("THANKS5", full)
        self.assertNotIn("THANKS5", minimal)
        self.assertNotIn("Stock:", minimal)
        self.assertIn("Stock:", full)

    # --- CSS and unsupported features -------------------------------------------------------------------------
    def test_layout_is_inline_and_style_block_is_small(self):
        for v, n, path in self.rendered():
            text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)  # skip the Outlook-only block
            style = re.search(r"<style>(.*?)</style>", text, re.S).group(1)
            style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
            outside_media = re.sub(r"@media[^{]+\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", style)
            selectors = [s.strip() for s in re.findall(r"([^{}]+)\{", outside_media)]
            self.assertEqual(selectors, ["body, table, td, a", "img"], f"{v}/{n}: only resets outside @media")
            for tag, attrs in parse(path).tags:
                if attrs.get("class") and tag in {"td", "table", "a", "body"}:
                    self.assertIn("style", attrs, f"{v}/{n}: <{tag} class={attrs['class']}> has no inline style")

    def test_unsupported_or_risky_features_absent(self):
        for v, n, path in self.rendered():
            text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)
            for pattern in BANNED_IN_HTML:
                self.assertNotRegex(text, "(?i)" + pattern, f"{v}/{n}: {pattern}")

    def test_visible_preheader_is_first_body_text(self):
        for v, n, path in self.rendered():
            c = parse(path)
            body_start = c.text.index(c.text[[i for i, t in enumerate(c.text) if "saved" in t or "Delivery, returns" in t or "last email" in t][0]])
            self.assertTrue(body_start < 4, f"{v}/{n}")

    # --- palette and contrast -----------------------------------------------------------------------------------
    def test_every_colour_is_in_the_palette(self):
        for v, n, path in self.rendered():
            used = set(re.findall(r"#[0-9a-fA-F]{6}\b", path.read_text(encoding="utf-8").lower()))
            self.assertFalse(used - set(PALETTE), f"{v}/{n}: {used - set(PALETTE)}")

    def test_text_contrast_meets_wcag_aa(self):
        for fg, bg in CONTRAST_PAIRS:
            self.assertGreaterEqual(contrast(fg, bg), 4.5, f"{fg} on {bg}: {contrast(fg, bg):.2f}")

    # --- images, links, UTM ----------------------------------------------------------------------------------------
    def test_images_have_alt_and_dimensions(self):
        for v, n, path in self.rendered():
            imgs = [a for t, a in parse(path).tags if t == "img"]
            self.assertLessEqual(len(imgs), 5)
            for a in imgs:
                self.assertTrue(a.get("alt"), f"{v}/{n}: missing alt")
                self.assertTrue(a.get("width") and a.get("height"), f"{v}/{n}: missing width/height")

    def test_links_and_utm_rules(self):
        for v, n, path in self.rendered():
            links = [a["href"] for t, a in parse(path).tags if t == "a"]
            self.assertTrue(all(h.startswith(("https://", "mailto:")) for h in links), f"{v}/{n}: {links}")
            contents = []
            for href in links:
                parts = urlsplit(href)
                query = parse_qs(parts.query)
                if href.startswith("mailto:"):
                    self.assertNotIn("utm_source", query)
                elif "unsubscribe" in parts.path:
                    self.assertFalse(any(k.startswith("utm_") for k in query), "never tag the unsubscribe link")
                else:
                    for key in ("utm_source", "utm_medium", "utm_campaign", "utm_content"):
                        self.assertIn(key, query, f"{v}/{n}: {href}")
                    for key in query:
                        if key.startswith("utm_"):
                            self.assertRegex(query[key][0], r"^[a-z0-9_]+$", "lowercase and underscores only")
                    contents.append(query["utm_content"][0])
            restore = [h for h in links if "/cart/restore/" in h]
            self.assertTrue(restore and all("SAMPLE-TOKEN" in h for h in restore), "UTM must not replace the restore token")
            self.assertTrue(any("unsubscribe" in h for h in links), f"{v}/{n}: no unsubscribe link")
            # The same destination may appear twice (button and plain link); each utm_content is still tied to its step.
            self.assertTrue(all(c.startswith(f"step{self.names.index(n) + 1}_") for c in contents), contents)

    def test_add_utm_keeps_existing_query_and_does_not_overwrite(self):
        url = build.add_utm("https://shop.example.com/cart?token=abc&utm_source=keep", "step1", "restore")
        q = parse_qs(urlsplit(url).query)
        self.assertEqual(q["token"], ["abc"])
        self.assertEqual(q["utm_source"], ["keep"])
        self.assertEqual(q["utm_content"], ["step1_restore"])

    # --- size ------------------------------------------------------------------------------------------------------
    def test_size_budget(self):
        for v, n, path in self.rendered():
            self.assertLess(path.stat().st_size, 100 * 1024, f"{v}/{n}: HTML over 100 KB")
            self.assertLess(path.with_suffix(".eml").stat().st_size, 150 * 1024)

    # --- plain text and MIME ----------------------------------------------------------------------------------------
    def test_plain_text_part_is_complete(self):
        for v, n, path in self.rendered():
            text = path.with_suffix(".txt").read_text(encoding="utf-8")
            self.assertNotRegex(text, r"<[a-zA-Z/][^>]*>", "no HTML tags in the text part")
            self.assertIn("SAMPLE-TOKEN?utm_source=cart_flow", text)
            self.assertIn("unsubscribe", text.lower())
            self.assertIn(self.data["common"]["postal_address"], text)
            self.assertTrue(all(len(line) <= 998 for line in text.splitlines()))

    def test_mime_message_is_alternative_with_unsubscribe_headers(self):
        for v, n, path in self.rendered():
            msg = email.message_from_bytes(path.with_suffix(".eml").read_bytes(), policy=policy.default)
            self.assertEqual(msg.get_content_type(), "multipart/alternative")
            kinds = [p.get_content_type() for p in msg.iter_parts()]
            self.assertEqual(kinds, ["text/plain", "text/html"], "plain text first, HTML last")
            self.assertIn("unsubscribe", msg["List-Unsubscribe"])
            self.assertEqual(msg["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
            self.assertEqual(msg["Subject"], next(s["subject"] for s in build.STEPS if s["name"] == n))


class ValidatorOptional(unittest.TestCase):
    """Runs the W3C Nu validator when its Docker container answers on localhost:8899. See README."""

    def test_validator_reports_only_the_known_exception(self):
        import urllib.request

        try:
            urllib.request.urlopen("http://localhost:8899/", timeout=2)
        except Exception:
            self.skipTest("validator container not running (see README)")
        for path in sorted((ROOT / "out").glob("*/email-*.html")):
            req = urllib.request.Request("http://localhost:8899/?out=text", data=path.read_bytes(),
                                         headers={"Content-Type": "text/html; charset=utf-8"})
            report = urllib.request.urlopen(req, timeout=30).read().decode()
            errors = [l for l in report.splitlines() if l.startswith("Error")]
            self.assertEqual(errors, ["Error: Attribute “xmlns:o” not allowed here."], f"{path}: {errors}")


if __name__ == "__main__":
    unittest.main()
