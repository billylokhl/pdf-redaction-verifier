"""Inline CSS for the gallery page: dependency-free (no external fonts or
scripts), readable on a phone, light/dark via ``prefers-color-scheme``."""

from __future__ import annotations

CSS = """
:root {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #5a5a5a; --border: #ddd;
  --card-bg: #fafafa; --accent: #8a1f11; --code-bg: #f0f0f0;
  --miss-bg: #fff3f2; --miss-border: #c62828; --badge-fg: #ffffff;
  --new-miss-bg: #fff0fa; --new-miss-border: #9c1b8f; --note-bg: #eef6ff; --note-border: #2f6fb3;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161a; --fg: #e8e8e8; --muted: #a0a0a0; --border: #3a3a3a;
    --card-bg: #1e2126; --accent: #ff8a75; --code-bg: #242830;
    --miss-bg: #2a1414; --miss-border: #ff6b5b; --badge-fg: #14161a;
    --new-miss-bg: #2a1428; --new-miss-border: #e05fd0; --note-bg: #16232e; --note-border: #6fb0ff;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 16px 48px; background: var(--bg); color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  line-height: 1.5;
}
header { max-width: 900px; margin: 0 auto; padding-top: 24px; }
h1 { font-size: 1.5rem; margin-bottom: 4px; }
.tagline, .counts { color: var(--muted); font-size: 0.9rem; }
main { max-width: 900px; margin: 0 auto; }
h2 { border-bottom: 2px solid var(--border); padding-bottom: 6px; margin-top: 40px; }
h3 { color: var(--muted); font-size: 1rem; margin-top: 24px; }
h3 code { font-size: 0.95rem; }
section.case {
  border: 1px solid var(--border); border-radius: 8px; background: var(--card-bg);
  padding: 16px; margin: 16px 0;
}
section.case.known-miss { background: var(--miss-bg); border-color: var(--miss-border); }
section.case.undocumented-miss { background: var(--new-miss-bg); border-color: var(--new-miss-border); }
section.case h4 { margin: 0 0 8px; font-size: 1rem; word-break: break-word; }
.badge {
  display: inline-block; font-size: 0.7rem; font-weight: bold; padding: 2px 8px;
  border-radius: 999px; margin-left: 8px; vertical-align: middle; color: var(--badge-fg);
}
.badge.miss { background: var(--miss-border); }
.badge.new-miss { background: var(--new-miss-border); }
.badge.false-alarm { background: var(--muted); }
.story { color: var(--fg); }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 12px 0; }
dt { font-weight: bold; color: var(--muted); }
dd { margin: 0; }
.render img {
  max-width: 100%; height: auto; border: 1px solid var(--border); border-radius: 4px;
  background: #fff;
}
.no-render, .no-visible-secret { color: var(--muted); font-style: italic; font-size: 0.85rem; }
code {
  background: var(--code-bg); padding: 1px 5px; border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.9em;
}
.secret code { margin-right: 6px; }
.verdict { font-size: 0.9rem; }
.verdict.miss { color: var(--miss-border); font-weight: bold; }
.verdict .source { color: var(--muted); font-weight: normal; font-style: italic; }
.note {
  font-size: 0.85rem; background: var(--note-bg); border-left: 3px solid var(--note-border);
  padding: 6px 10px; border-radius: 4px;
}
.links { font-size: 0.85rem; color: var(--muted); }
.links a { color: var(--accent); }
a { color: var(--accent); }
@media (max-width: 480px) {
  dl { grid-template-columns: 1fr; }
  dt { margin-top: 8px; }
}
"""
