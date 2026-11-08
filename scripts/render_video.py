#!/usr/bin/env python3
"""High-resolution narrated video & thumbnail generator for toolrecall-light.

Renders:
1. thumbnail.png — 1920x1080 hero thumbnail with before/after visual comparison.
2. demo.mp4 — 1080p H.264 + AAC walkthrough video with multi-stage visual builds,
   synchronized English voiceover (via Gemini 3.8 Flash TTS), subtitles, and progress bar.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from PIL import Image, ImageDraw, ImageFont

WIDTH = 1920
HEIGHT = 1080
REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_VIDEO = REPO_ROOT / "demo.mp4"
OUTPUT_THUMB = REPO_ROOT / "thumbnail.png"
VOICE_CACHE_DIR = Path(tempfile.gettempdir()) / "toolrecall_light_tts_cache"

# Typography: Clean Sans-Serif (Lato) for UI/Headers + Monospace (DejaVuSansMono) for Code
SANS_REG = "/usr/share/fonts/truetype/lato/Lato-Regular.ttf"
SANS_MED = "/usr/share/fonts/truetype/lato/Lato-Semibold.ttf"
SANS_BOLD = "/usr/share/fonts/truetype/lato/Lato-Bold.ttf"
SANS_BLACK = "/usr/share/fonts/truetype/lato/Lato-Black.ttf"
MONO_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

F_SANS_XS = ImageFont.truetype(SANS_MED, 16)
F_SANS_SM = ImageFont.truetype(SANS_REG, 19)
F_SANS_SM_B = ImageFont.truetype(SANS_BOLD, 19)
F_SANS_MD = ImageFont.truetype(SANS_REG, 23)
F_SANS_MD_B = ImageFont.truetype(SANS_BOLD, 23)
F_SANS_LG_B = ImageFont.truetype(SANS_BOLD, 30)
F_SANS_XL_B = ImageFont.truetype(SANS_BLACK, 42)
F_SANS_HERO = ImageFont.truetype(SANS_BLACK, 58)

F_MONO_XS = ImageFont.truetype(MONO_REG, 15)
F_MONO_SM = ImageFont.truetype(MONO_REG, 17)
F_MONO_SM_B = ImageFont.truetype(MONO_BOLD, 17)
F_MONO_MD = ImageFont.truetype(MONO_REG, 20)
F_MONO_MD_B = ImageFont.truetype(MONO_BOLD, 20)
F_MONO_LG_B = ImageFont.truetype(MONO_BOLD, 25)

# Color Palette (Deep Slate / ToolRecall Argon Theme)
BG_COLOR = (11, 15, 25)
NAV_BG = (16, 21, 34)
WINDOW_BG = (19, 25, 38)
CARD_BG = (23, 30, 46)
BORDER_SUBTLE = (45, 55, 72)
TEXT_WHITE = (241, 245, 249)
TEXT_MUTED = (148, 163, 184)
TEXT_DIM = (100, 116, 139)

ACCENT_CYAN = (56, 189, 248)
ACCENT_GREEN = (52, 211, 153)
ACCENT_RED = (248, 113, 113)
ACCENT_AMBER = (251, 191, 36)
ACCENT_PURPLE = (192, 132, 252)

GREEN_BG = (16, 40, 34)
RED_BG = (44, 20, 26)
PURPLE_BG = (34, 22, 58)
AMBER_BG = (42, 32, 16)
CYAN_BG = (16, 34, 52)


# ---------------------------------------------------------------------------
# Vector Icon Helpers (avoids missing unicode glyphs in TTF fonts)
# ---------------------------------------------------------------------------
def draw_check_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color: tuple[int, int, int]):
    draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=(color[0] // 5, color[1] // 5, color[2] // 5), outline=color, width=2)
    draw.line([(cx - r // 2, cy), (cx - r // 6, cy + r // 2), (cx + r // 2 + 1, cy - r // 3)], fill=color, width=3)


def draw_bolt_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color: tuple[int, int, int]):
    draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=(color[0] // 5, color[1] // 5, color[2] // 5), outline=color, width=2)
    pts = [
        (cx + 2, cy - r + 5),
        (cx - r // 2, cy + 1),
        (cx, cy + 1),
        (cx - 2, cy + r - 5),
        (cx + r // 2, cy - 1),
        (cx, cy - 1),
    ]
    draw.polygon(pts, fill=color)


def draw_cross_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color: tuple[int, int, int]):
    draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=(color[0] // 5, color[1] // 5, color[2] // 5), outline=color, width=2)
    d = r // 2 - 1
    draw.line([(cx - d, cy - d), (cx + d, cy + d)], fill=color, width=3)
    draw.line([(cx - d, cy + d), (cx + d, cy - d)], fill=color, width=3)


def draw_shield_icon(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color: tuple[int, int, int]):
    draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=(color[0] // 5, color[1] // 5, color[2] // 5), outline=color, width=2)
    pts = [
        (cx, cy - r + 5),
        (cx + r - 6, cy - r // 2 + 2),
        (cx + r - 7, cy + 2),
        (cx, cy + r - 5),
        (cx - r + 7, cy + 2),
        (cx - r + 6, cy - r // 2 + 2),
    ]
    draw.polygon(pts, outline=color, width=2)


# ---------------------------------------------------------------------------
# Base Canvas, Top Header, Terminal Shell & Caption Bar
# ---------------------------------------------------------------------------
def create_base_canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)
    # Subtle background grid lines for engineering aesthetic
    for gx in range(0, WIDTH, 120):
        draw.line([(gx, 0), (gx, HEIGHT)], fill=(15, 20, 32), width=1)
    for gy in range(0, HEIGHT, 120):
        draw.line([(0, gy), (WIDTH, gy)], fill=(15, 20, 32), width=1)
    return img, draw


def draw_top_nav(
    draw: ImageDraw.ImageDraw,
    scene_badge: str,
    turn_badge: str,
    baseline_tokens: str,
    treat_tokens: str,
    savings_pct: str,
):
    draw.rectangle([(0, 0), (WIDTH, 88)], fill=NAV_BG)
    draw.line([(0, 88), (WIDTH, 88)], fill=BORDER_SUBTLE, width=2)

    # Brand mark
    draw_bolt_icon(draw, 58, 44, 18, ACCENT_PURPLE)
    draw.text((90, 22), "HERMES AGENT v2 PLUGIN", font=F_SANS_XS, fill=ACCENT_PURPLE)
    draw.text((90, 44), "toolrecall-light", font=F_MONO_MD_B, fill=TEXT_WHITE)

    # Scene & Turn Badges (generous width so text never overflows)
    draw.rounded_rectangle([(430, 20), (650, 68)], radius=8, fill=PURPLE_BG, outline=ACCENT_PURPLE, width=2)
    draw.text((540, 44), scene_badge, font=F_SANS_SM_B, fill=ACCENT_PURPLE, anchor="mm")

    draw.rounded_rectangle([(670, 20), (1020, 68)], radius=8, fill=CYAN_BG, outline=ACCENT_CYAN, width=2)
    draw.text((845, 44), turn_badge, font=F_MONO_SM_B, fill=ACCENT_CYAN, anchor="mm")

    # Token comparison counters
    draw.text((1065, 22), "Vanilla Context:", font=F_SANS_XS, fill=TEXT_MUTED)
    draw.text((1065, 45), baseline_tokens, font=F_MONO_MD_B, fill=ACCENT_RED)

    draw.text((1315, 22), "With toolrecall-light:", font=F_SANS_XS, fill=TEXT_MUTED)
    draw.text((1315, 45), treat_tokens, font=F_MONO_MD_B, fill=ACCENT_GREEN)

    if savings_pct:
        draw.rounded_rectangle([(1590, 18), (1870, 70)], radius=10, fill=GREEN_BG, outline=ACCENT_GREEN, width=2)
        draw.text((1730, 44), f"SAVED: {savings_pct}", font=F_SANS_MD_B, fill=ACCENT_GREEN, anchor="mm")


def draw_terminal_window(
    draw: ImageDraw.ImageDraw,
    title: str = "hermes@agent: ~/project — plugin: toolrecall-light (active)",
    bottom_y: int = 925,
):
    draw.rounded_rectangle([(50, 108), (1870, bottom_y)], radius=14, fill=WINDOW_BG, outline=BORDER_SUBTLE, width=2)
    draw.rounded_rectangle([(50, 108), (1870, 156)], radius=14, fill=(26, 33, 48), outline=BORDER_SUBTLE, width=2)
    draw.rectangle([(52, 142), (1868, 156)], fill=(26, 33, 48))
    draw.line([(50, 156), (1870, 156)], fill=BORDER_SUBTLE, width=2)

    # Window traffic lights
    draw.ellipse([(76, 124), (92, 140)], fill=(239, 68, 68))
    draw.ellipse([(102, 124), (118, 140)], fill=(245, 158, 11))
    draw.ellipse([(128, 124), (144, 140)], fill=(16, 185, 129))
    draw.text((960, 132), title, font=F_MONO_XS, fill=TEXT_MUTED, anchor="mm")


def draw_caption_and_progress(
    draw: ImageDraw.ImageDraw,
    caption: str,
    progress_ratio: float,
    step_label: str,
):
    # Closed-caption bar at bottom
    draw.rounded_rectangle([(50, 942), (1870, 1046)], radius=12, fill=(14, 18, 29), outline=(56, 70, 94), width=2)

    # Speaker indicator pill
    draw.rounded_rectangle([(74, 962), (234, 996)], radius=6, fill=PURPLE_BG, outline=ACCENT_PURPLE, width=1)
    draw.text((154, 979), "VOICEOVER", font=F_SANS_XS, fill=ACCENT_PURPLE, anchor="mm")

    draw.text((154, 1018), step_label, font=F_MONO_XS, fill=ACCENT_CYAN, anchor="mm")

    # Wrap or render 1-2 lines of caption text
    words = caption.split()
    lines = []
    cur = []
    for w in words:
        test = " ".join(cur + [w])
        if len(test) > 102 and cur:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))

    if len(lines) == 1:
        draw.text((262, 994), lines[0], font=F_SANS_MD_B, fill=TEXT_WHITE, anchor="lm")
    else:
        draw.text((262, 976), lines[0], font=F_SANS_MD_B, fill=TEXT_WHITE, anchor="lm")
        draw.text((262, 1012), " ".join(lines[1:]), font=F_SANS_MD_B, fill=TEXT_WHITE, anchor="lm")

    # Timeline progress bar at very bottom edge
    draw.rectangle([(0, 1070), (WIDTH, 1080)], fill=(22, 28, 42))
    bar_w = max(12, int(WIDTH * min(max(progress_ratio, 0.0), 1.0)))
    draw.rectangle([(0, 1070), (bar_w, 1080)], fill=ACCENT_CYAN)


# ---------------------------------------------------------------------------
# Thumbnail Generator (1920x1080)
# ---------------------------------------------------------------------------
def render_thumbnail() -> Image.Image:
    img, draw = create_base_canvas()

    # Top header banner
    draw.rounded_rectangle([(560, 36), (1360, 82)], radius=22, fill=PURPLE_BG, outline=ACCENT_PURPLE, width=2)
    draw.text(
        (960, 59),
        "HERMES AGENT v2 PLUGIN   •   ZERO DEPENDENCIES   •   STDLIB ONLY",
        font=F_SANS_SM_B,
        fill=ACCENT_PURPLE,
        anchor="mm",
    )

    # Hero Title & Subtitle
    draw.text((960, 136), "ToolRecall Light  (toolrecall-light)", font=F_SANS_HERO, fill=TEXT_WHITE, anchor="mm")
    draw.text(
        (960, 198),
        "Deterministic Tool Result Deduplication & Sub-Millisecond Recall for Hermes Agent",
        font=F_SANS_LG_B,
        fill=ACCENT_CYAN,
        anchor="mm",
    )

    # Left Card: WITHOUT PLUGIN (The Problem)
    draw.rounded_rectangle([(60, 245), (935, 815)], radius=16, fill=WINDOW_BG, outline=ACCENT_RED, width=3)
    draw.rounded_rectangle([(60, 245), (935, 312)], radius=16, fill=RED_BG, outline=ACCENT_RED, width=2)
    draw_cross_icon(draw, 104, 279, 16, ACCENT_RED)
    draw.text((135, 279), "WITHOUT PLUGIN — Vanilla Hermes Session", font=F_SANS_MD_B, fill=ACCENT_RED, anchor="lm")
    draw.rounded_rectangle([(725, 260), (915, 298)], radius=6, fill=(65, 24, 32), outline=ACCENT_RED, width=1)
    draw.text((820, 279), "135,937 Tokens", font=F_MONO_SM_B, fill=ACCENT_RED, anchor="mm")

    draw.text((95, 342), '$ terminal(command="pytest tests/test_auth.py")  # Turn 04 rerun', font=F_MONO_SM_B, fill=TEXT_WHITE)

    raw_box_lines = [
        ("============================= test session starts =============================", TEXT_MUTED),
        ("tests/test_auth.py .....................F.............................. [ 62%]", ACCENT_AMBER),
        ("=================================== FAILURES ==================================", ACCENT_RED),
        ("E   AssertionError: assert 401 == 200 (ExpiredSignatureError: Signature expired)", ACCENT_RED),
        ('  File "/workspace/project/auth/jwt.py", line 112, in verify_token', TEXT_MUTED),
        ("  ... [60 lines / 2,317 bytes re-ingested on Turn 4, 5, 6, 7 ... 20]", ACCENT_RED),
    ]
    draw.rounded_rectangle([(90, 378), (905, 588)], radius=10, fill=(14, 18, 28), outline=BORDER_SUBTLE, width=2)
    ry = 398
    for line_txt, col in raw_box_lines:
        draw.text((112, ry), line_txt, font=F_MONO_XS, fill=col)
        ry += 30

    bad_points = [
        "Every repeated pytest, cargo test, or search_files dump stays in chat history",
        "60.1% of real Hermes tool outputs >= 2 KB are exact intra-session duplicates",
        "Compounds O(N^2) across turns -> premature 128k context compaction",
    ]
    py = 618
    for pt in bad_points:
        draw_cross_icon(draw, 108, py + 11, 11, ACCENT_RED)
        draw.text((132, py), pt, font=F_SANS_SM, fill=TEXT_WHITE)
        py += 44

    draw.rounded_rectangle([(90, 748), (905, 796)], radius=8, fill=RED_BG, outline=ACCENT_RED, width=1)
    draw.text((497, 772), "Turn Context Growth: +579 Tokens Every Repeat  (Bloats History)", font=F_SANS_SM_B, fill=ACCENT_RED, anchor="mm")

    # Right Card: WITH TOOLRECALL LIGHT (The Solution)
    draw.rounded_rectangle([(985, 245), (1860, 815)], radius=16, fill=WINDOW_BG, outline=ACCENT_GREEN, width=3)
    draw.rounded_rectangle([(985, 245), (1860, 312)], radius=16, fill=GREEN_BG, outline=ACCENT_GREEN, width=2)
    draw_check_icon(draw, 1029, 279, 16, ACCENT_GREEN)
    draw.text((1060, 279), "WITH TOOLRECALL LIGHT — Intercepted & Recallable", font=F_SANS_MD_B, fill=ACCENT_GREEN, anchor="lm")
    draw.rounded_rectangle([(1640, 260), (1840, 298)], radius=6, fill=(20, 62, 48), outline=ACCENT_GREEN, width=1)
    draw.text((1740, 279), "62,477 Tokens", font=F_MONO_SM_B, fill=ACCENT_GREEN, anchor="mm")

    draw.text((1020, 342), '$ terminal(command="pytest tests/test_auth.py")  # SHA-256 Match!', font=F_MONO_SM_B, fill=TEXT_WHITE)

    # Compact stub box
    draw.rounded_rectangle([(1015, 378), (1830, 495)], radius=10, fill=PURPLE_BG, outline=ACCENT_PURPLE, width=2)
    draw.text((1040, 396), "2-LINE DETERMINISTIC STUB REPLACES 2,317-BYTE TRACEBACK:", font=F_SANS_XS, fill=ACCENT_AMBER)
    draw.text(
        (1040, 426),
        "[toolrecall-light] identical to terminal result 50f59afa067a (2317 B)",
        font=F_MONO_SM_B,
        fill=ACCENT_PURPLE,
    )
    draw.text(
        (1040, 456),
        'Call recall_result(result_id="50f59afa067a") if you need full content.',
        font=F_MONO_SM_B,
        fill=ACCENT_GREEN,
    )

    # On-demand recall box
    draw.rounded_rectangle([(1015, 512), (1830, 588)], radius=10, fill=(14, 24, 36), outline=ACCENT_CYAN, width=2)
    draw.text((1040, 528), 'On-Demand Recall:  recall_result(result_id="50f59afa067a")', font=F_MONO_SM_B, fill=ACCENT_CYAN)
    draw.text((1040, 556), "Restores 100% byte-for-byte original output from local SQLite in 0.23 ms", font=F_SANS_SM, fill=TEXT_WHITE)

    good_points = [
        "SHA-256 content verification: any 1-byte file or test change passes through live",
        "Compaction-aware (post_compaction hook) + 5-streak loop-breaker safety guard",
        "hermes plugins install https://github.com/whiskybeer/toolrecall-light",
    ]
    py = 618
    for pt in good_points:
        draw_check_icon(draw, 1033, py + 11, 11, ACCENT_GREEN)
        draw.text((1057, py), pt, font=F_SANS_SM, fill=TEXT_WHITE)
        py += 44

    draw.rounded_rectangle([(1015, 748), (1830, 796)], radius=8, fill=GREEN_BG, outline=ACCENT_GREEN, width=1)
    draw.text((1422, 772), "Turn Context Growth: +43 Tokens (-92.5% Per Repeat  •  -54.04% Session Total)", font=F_SANS_SM_B, fill=ACCENT_GREEN, anchor="mm")

    # Bottom 4 Metric Badges
    cards = [
        ("-54.04%", "Prompt Tokens Saved", "73,460 tokens avoided in 20-turn SWE bench", ACCENT_GREEN, GREEN_BG),
        ("60.1%", "Real state.db Dupes", "74.5 MB redundant across 131,520 calls", ACCENT_PURPLE, PURPLE_BG),
        ("0.23 ms", "SQLite Recall Speed", "Exact byte-for-byte restoration on demand", ACCENT_CYAN, CYAN_BG),
        ("22 / 22", "Verified & Fail-Open", "Zero runtime deps • Catalog Rule 9 compliant", ACCENT_AMBER, AMBER_BG),
    ]
    for idx, (big, title, sub, col, bg) in enumerate(cards):
        x0 = 60 + idx * 455
        x1 = x0 + 435
        draw.rounded_rectangle([(x0, 845), (x1, 1035)], radius=14, fill=CARD_BG, outline=col, width=2)
        draw.rounded_rectangle([(x0 + 20, 865), (x0 + 185, 925)], radius=8, fill=bg, outline=col, width=1)
        draw.text((x0 + 102, 895), big, font=F_SANS_LG_B, fill=col, anchor="mm")
        draw.text((x0 + 205, 895), title, font=F_SANS_MD_B, fill=TEXT_WHITE, anchor="lm")
        draw.text((x0 + 22, 955), sub, font=F_SANS_SM, fill=TEXT_MUTED)
        draw.text((x0 + 22, 990), "github.com/whiskybeer/toolrecall-light", font=F_MONO_XS, fill=TEXT_DIM)

    return img


# ---------------------------------------------------------------------------
# Multi-Stage Video Scene Renderers (Stages 0, 1, 2 per Scene)
# ---------------------------------------------------------------------------
def render_scene_1_intro(stage: int) -> Image.Image:
    img, draw = create_base_canvas()

    # Header badge
    draw.rounded_rectangle([(620, 55), (1300, 105)], radius=24, fill=PURPLE_BG, outline=ACCENT_PURPLE, width=2)
    draw.text((960, 80), "HERMES AGENT v2 PLUGIN   •   OFFICIAL WALKTHROUGH", font=F_SANS_SM_B, fill=ACCENT_PURPLE, anchor="mm")

    draw.text((960, 165), "ToolRecall Light  (toolrecall-light)", font=F_SANS_HERO, fill=TEXT_WHITE, anchor="mm")
    draw.text(
        (960, 230),
        "Deterministic Tool Result Deduplication & On-Demand Recall",
        font=F_SANS_LG_B,
        fill=ACCENT_CYAN,
        anchor="mm",
    )

    # Problem callout box
    draw.rounded_rectangle([(140, 285), (1780, 415)], radius=14, fill=RED_BG, outline=ACCENT_RED, width=2)
    draw_bolt_icon(draw, 195, 350, 22, ACCENT_AMBER)
    draw.text(
        (240, 325),
        "THE CONTEXT BLOAT TRAP IN MULTI-TURN CODING AGENTS:",
        font=F_SANS_MD_B,
        fill=ACCENT_AMBER,
    )
    draw.text(
        (240, 365),
        "Hermes core deduplicates read_file, but terminal (pytest, cargo, git) & search_files re-send identical",
        font=F_SANS_MD,
        fill=TEXT_WHITE,
    )
    draw.text(
        (240, 395),
        "tracebacks on every subsequent turn — burning 60.1% of large tool output tokens on byte-identical repeats.",
        font=F_SANS_MD,
        fill=TEXT_MUTED,
    )

    if stage >= 1:
        pillars = [
            ("54.04% Token Reduction", "73,460 prompt tokens saved in a 20-turn SWE debugging trajectory", ACCENT_GREEN, GREEN_BG),
            ("60.1% Real Duplicate Rate", "Measured across 668 real Hermes sessions (131,520 tool calls, 74.5 MB)", ACCENT_PURPLE, PURPLE_BG),
            ("0.23 ms On-Demand Recall", "Model calls recall_result(result_id) to restore exact bytes anytime", ACCENT_CYAN, CYAN_BG),
            ("100% Fail-Open & Safe", "Zero external deps, 5-streak loop guard, post_compaction generation reset", ACCENT_AMBER, AMBER_BG),
        ]
        for i, (title, desc, col, bg) in enumerate(pillars):
            if stage == 1 and i >= 2:
                continue
            x = 140 + (i % 2) * 840
            y = 450 + (i // 2) * 175
            draw.rounded_rectangle([(x, y), (x + 800, y + 145)], radius=14, fill=CARD_BG, outline=col, width=2)
            draw_check_icon(draw, x + 42, y + 45, 16, col)
            draw.text((x + 75, y + 45), title, font=F_SANS_LG_B, fill=col, anchor="lm")
            draw.text((x + 75, y + 95), desc, font=F_SANS_SM, fill=TEXT_WHITE)

    if stage >= 2:
        draw.rounded_rectangle([(140, 815), (1780, 905)], radius=12, fill=(18, 26, 40), outline=ACCENT_CYAN, width=2)
        draw.text(
            (960, 860),
            "Hooks: transform_tool_result + post_compaction   •   Tool: recall_result(result_id)   •   Pure Python Stdlib + SQLite WAL",
            font=F_MONO_MD_B,
            fill=ACCENT_CYAN,
            anchor="mm",
        )

    return img


def render_scene_2_turn01(stage: int) -> Image.Image:
    img, draw = create_base_canvas()
    draw_top_nav(draw, "SCENE 1 / 5", "TURN 01 / 20  [FIRST RUN]", "579 toks", "579 toks", "")
    draw_terminal_window(draw)

    draw.text((90, 185), "agent@hermes:~/project$ ", font=F_MONO_MD_B, fill=ACCENT_CYAN)
    draw.text((390, 185), 'terminal(command="pytest tests/test_auth.py")', font=F_MONO_MD_B, fill=TEXT_WHITE)

    if stage >= 1:
        lines = [
            ("============================= test session starts ==============================", TEXT_MUTED),
            ("platform linux -- Python 3.11.2, pytest-9.1.1, rootdir: /workspace/project", TEXT_MUTED),
            ("collected 85 items", TEXT_MUTED),
            ("tests/test_auth.py .....................F............................... [ 62%]", ACCENT_AMBER),
            ("tests/test_api.py ....................................                    [100%]", ACCENT_GREEN),
            ("=================================== FAILURES ===================================", ACCENT_RED),
            ("_________________________________ test_jwt_auth ________________________________", ACCENT_RED),
            ("tests/test_auth.py:52: in test_jwt_auth", TEXT_WHITE),
            ("    assert resp.status_code == 200", TEXT_WHITE),
            ("E   AssertionError: assert 401 == 200 (ExpiredSignatureError: Signature has expired)", ACCENT_RED),
            ("[ERROR] Authentication failed for request-id req-42: Signature expired", ACCENT_RED),
            ("jwt.exceptions.ExpiredSignatureError: Signature has expired", ACCENT_RED),
            ("Frame locals: user_id='alice', leeway=0, now=1730943600.0 (expired at 1730940000.0)", TEXT_MUTED),
            ("======================== 1 failed, 84 passed in 2.14s ==========================", ACCENT_AMBER),
        ]
        y = 232
        for text, color in lines:
            draw.text((90, y), text, font=F_MONO_SM, fill=color)
            y += 29

    if stage >= 2:
        draw.rounded_rectangle([(90, 665), (1830, 890)], radius=12, fill=GREEN_BG, outline=ACCENT_GREEN, width=2)
        draw_check_icon(draw, 135, 710, 18, ACCENT_GREEN)
        draw.text((170, 710), "[transform_tool_result] Hook Evaluated — Unseen Key in Session", font=F_SANS_LG_B, fill=ACCENT_GREEN, anchor="lm")
        draw.text((170, 760), "• Canonical Key:  terminal + {\"command\":\"pytest tests/test_auth.py\"}  (volatile timeout/_meta stripped)", font=F_MONO_SM, fill=TEXT_WHITE)
        draw.text((170, 800), "• Content SHA-256 computed & persisted to local SQLite store in 0.28 ms  (result_id: 50f59afa067a)", font=F_MONO_SM, fill=TEXT_WHITE)
        draw.text((170, 840), "• First occurrence: Full 2,317 bytes (~579 prompt tokens) passed through to model context untouched.", font=F_SANS_MD_B, fill=ACCENT_CYAN)

    return img


def render_scene_3_turn04(stage: int) -> Image.Image:
    img, draw = create_base_canvas()
    draw_top_nav(draw, "SCENE 2 / 5", "TURN 04 / 20  [DUPLICATE]", "4,634 toks", "2,491 toks", "-46.2%")
    draw_terminal_window(draw)

    draw.text((90, 185), "agent@hermes:~/project$ ", font=F_MONO_MD_B, fill=ACCENT_CYAN)
    draw.text((390, 185), 'terminal(command="pytest tests/test_auth.py")   # Rerun after edit attempt', font=F_MONO_MD_B, fill=TEXT_WHITE)

    draw.rounded_rectangle([(90, 232), (1830, 320)], radius=10, fill=PURPLE_BG, outline=ACCENT_PURPLE, width=2)
    draw_bolt_icon(draw, 135, 276, 18, ACCENT_PURPLE)
    draw.text((170, 258), "[toolrecall-light] Byte-Identical Duplicate Intercepted Before Entering Context!", font=F_SANS_MD_B, fill=ACCENT_PURPLE)
    draw.text((170, 292), "SHA-256 matches Turn 01 (2,317 bytes). Re-sending identical tracebacks across turns causes O(N^2) prompt bloat.", font=F_SANS_SM, fill=TEXT_WHITE)

    if stage >= 1:
        draw.rounded_rectangle([(90, 345), (1830, 505)], radius=12, fill=(28, 18, 52), outline=ACCENT_PURPLE, width=3)
        draw.text((130, 375), "MODEL CONTEXT RECEIVES THIS 174-BYTE STUB INSTEAD OF 60 LINES OF TRACEBACK:", font=F_SANS_SM_B, fill=ACCENT_AMBER)
        draw.text(
            (130, 415),
            "[toolrecall-light] identical to the earlier terminal result 50f59afa067a (2317 bytes) in this session.",
            font=F_MONO_MD_B,
            fill=ACCENT_PURPLE,
        )
        draw.text(
            (130, 455),
            'Call recall_result(result_id="50f59afa067a") if you need the full content.',
            font=F_MONO_MD_B,
            fill=ACCENT_GREEN,
        )

    if stage >= 2:
        # Left comparison card
        draw.rounded_rectangle([(90, 535), (940, 890)], radius=12, fill=RED_BG, outline=ACCENT_RED, width=2)
        draw_cross_icon(draw, 130, 575, 15, ACCENT_RED)
        draw.text((160, 575), "WITHOUT PLUGIN (Vanilla Hermes):", font=F_SANS_MD_B, fill=ACCENT_RED, anchor="lm")
        draw.text((130, 625), "• Appends another 2,317 bytes of identical traceback", font=F_SANS_MD, fill=TEXT_WHITE)
        draw.text((130, 670), "• Paid again on Turn 5, 6, 7 ... 20 (+37,000 tokens)", font=F_SANS_MD, fill=TEXT_WHITE)
        draw.text((130, 715), "• Pushes session toward 128k lossy compaction ceiling", font=F_SANS_MD, fill=TEXT_WHITE)
        draw.rounded_rectangle([(125, 795), (905, 860)], radius=8, fill=(65, 22, 30), outline=ACCENT_RED, width=1)
        draw.text((515, 827), "Turn Context Growth: +579 Tokens", font=F_SANS_LG_B, fill=ACCENT_RED, anchor="mm")

        # Right comparison card
        draw.rounded_rectangle([(980, 535), (1830, 890)], radius=12, fill=GREEN_BG, outline=ACCENT_GREEN, width=2)
        draw_check_icon(draw, 1020, 575, 15, ACCENT_GREEN)
        draw.text((1050, 575), "WITH TOOLRECALL LIGHT:", font=F_SANS_MD_B, fill=ACCENT_GREEN, anchor="lm")
        draw.text((1020, 625), "• Replaces payload with 174-byte reference stub", font=F_SANS_MD, fill=TEXT_WHITE)
        draw.text((1020, 670), "• Direct single-turn reduction: -2,143 bytes (-92.5%)", font=F_SANS_MD, fill=TEXT_WHITE)
        draw.text((1020, 715), "• Full bytes remain 1 tool call away via recall_result", font=F_SANS_MD, fill=TEXT_WHITE)
        draw.rounded_rectangle([(1015, 795), (1795, 860)], radius=8, fill=(20, 60, 45), outline=ACCENT_GREEN, width=1)
        draw.text((1405, 827), "Turn Context Growth: +43 Tokens (-92.5%)", font=F_SANS_LG_B, fill=ACCENT_GREEN, anchor="mm")

    return img


def render_scene_4_turn05(stage: int) -> Image.Image:
    img, draw = create_base_canvas()
    draw_top_nav(draw, "SCENE 3 / 5", "TURN 05 / 20  [ON-DEMAND RECALL]", "4,820 toks", "2,534 toks", "-47.4%")
    draw_terminal_window(draw)

    draw.text((90, 185), "agent@hermes:~/project$ ", font=F_MONO_MD_B, fill=ACCENT_CYAN)
    draw.text((390, 185), "# Agent needs exact frame locals from stubbed traceback:", font=F_MONO_MD, fill=TEXT_MUTED)
    draw.text((90, 230), "agent@hermes:~/project$ ", font=F_MONO_MD_B, fill=ACCENT_CYAN)
    draw.text((390, 230), 'recall_result(result_id="50f59afa067a")', font=F_MONO_LG_B, fill=ACCENT_AMBER)

    if stage >= 1:
        draw.rounded_rectangle([(90, 285), (1830, 420)], radius=12, fill=GREEN_BG, outline=ACCENT_GREEN, width=2)
        draw_check_icon(draw, 135, 325, 18, ACCENT_GREEN)
        draw.text((170, 325), "recall_result Executed in 0.23 ms — Sub-Millisecond Local SQLite Lookup", font=F_SANS_LG_B, fill=ACCENT_GREEN, anchor="lm")
        draw.text((170, 372), "• Verified session scope & content hash -> returned exact byte-for-byte original payload (2,317 bytes).", font=F_SANS_MD, fill=TEXT_WHITE)

    if stage >= 2:
        draw.rounded_rectangle([(90, 450), (1830, 775)], radius=12, fill=(14, 19, 30), outline=ACCENT_CYAN, width=2)
        draw.text((130, 480), "RESTORED JSON PAYLOAD DELIVERED TO MODEL:", font=F_SANS_SM_B, fill=ACCENT_CYAN)
        restored = [
            ('{ "result_id": "50f59afa067a",', ACCENT_CYAN),
            ('  "content": "tests/test_auth.py:52: in test_jwt_auth -> assert resp.status_code == 200', TEXT_WHITE),
            ("              E   AssertionError: assert 401 == 200 (ExpiredSignatureError: Signature has expired)", ACCENT_RED),
            ("              # Frame locals: user_id='alice', algorithms=['HS256']", TEXT_MUTED),
            ('              # leeway = 0, now = 1730943600.0 (expired at 1730940000.0)" }', ACCENT_AMBER),
        ]
        y = 525
        for line, col in restored:
            draw.text((130, y), line, font=F_MONO_SM, fill=col)
            y += 42

        draw.rounded_rectangle([(90, 800), (1830, 890)], radius=10, fill=CYAN_BG, outline=ACCENT_CYAN, width=2)
        draw_check_icon(draw, 135, 845, 16, ACCENT_CYAN)
        draw.text(
            (170, 845),
            "Zero Information Loss: Model spots 'leeway = 0' in frame locals and patches verify_token(leeway=60).",
            font=F_SANS_MD_B,
            fill=TEXT_WHITE,
            anchor="lm",
        )

    return img


def render_scene_5_compaction(stage: int) -> Image.Image:
    img, draw = create_base_canvas()
    draw_top_nav(draw, "SCENE 4 / 5", "TURNS 10-12  [SAFETY GUARDS]", "8,920 toks", "4,710 toks", "-47.2%")
    draw_terminal_window(draw)

    # Banner
    draw.rounded_rectangle([(90, 185), (1830, 310)], radius=12, fill=PURPLE_BG, outline=ACCENT_PURPLE, width=2)
    draw_shield_icon(draw, 135, 247, 20, ACCENT_PURPLE)
    draw.text((175, 220), "Compaction Awareness (post_compaction) & Loop-Breaker Architecture", font=F_SANS_LG_B, fill=ACCENT_PURPLE, anchor="lm")
    draw.text((175, 265), "What happens when Hermes compacts conversation history (/compress) and summarizes away Turn 01?", font=F_SANS_MD, fill=TEXT_WHITE)

    if stage >= 1:
        draw.rounded_rectangle([(90, 335), (1830, 605)], radius=12, fill=CARD_BG, outline=ACCENT_GREEN, width=2)
        draw.text((130, 370), "1. GENERATION-SCOPED COMPACTION RESET (Mirrors Hermes Core read_file):", font=F_SANS_MD_B, fill=ACCENT_GREEN)
        steps = [
            ("Turn 10  [/compress]", "Hermes compacts chat history -> post_compaction hook increments session generation (gen = 1).", ACCENT_PURPLE),
            ("Turn 11  [1st Repeat]", "served_gen (0) != current_gen (1) -> Full content is re-served ONCE so the model isn't blinded.", ACCENT_CYAN),
            ("Turn 12  [2nd Repeat]", "served_gen (1) == current_gen (1) -> Compact 2-line stubbing automatically resumes!", ACCENT_GREEN),
        ]
        sy = 420
        for badge, desc, col in steps:
            draw.rounded_rectangle([(130, sy), (390, sy + 44)], radius=6, fill=(16, 22, 34), outline=col, width=2)
            draw.text((260, sy + 22), badge, font=F_MONO_SM_B, fill=col, anchor="mm")
            draw.text((420, sy + 22), desc, font=F_SANS_MD, fill=TEXT_WHITE, anchor="lm")
            sy += 58

    if stage >= 2:
        draw.rounded_rectangle([(90, 630), (1830, 890)], radius=12, fill=CARD_BG, outline=ACCENT_AMBER, width=2)
        draw.text((130, 665), "2. STREAK GUARD LOOP-BREAKER & FAIL-OPEN GUARANTEE:", font=F_SANS_MD_B, fill=ACCENT_AMBER)
        draw.text((130, 715), "• Streak Cap (MAX_STUB_STREAK = 5): After 5 consecutive stubs for the same key, the 6th call forces full", font=F_SANS_MD, fill=TEXT_WHITE)
        draw.text((130, 752), "  content pass-through once — preventing stuck evaluation loops while keeping 95.35% dedup efficiency.", font=F_SANS_MD, fill=TEXT_MUTED)
        draw.text((130, 800), "• 2 MB Payload Guard & Parameterized SQL: Blocks OOM output bombs and SQL injection vectors.", font=F_SANS_MD, fill=TEXT_WHITE)
        draw.text((130, 840), "• 100% Fail-Open: Any SQLite or runtime exception returns None, delivering raw tool output untouched.", font=F_SANS_MD_B, fill=ACCENT_GREEN)

    return img


def render_scene_6_receipt(stage: int) -> Image.Image:
    img, draw = create_base_canvas()
    draw_top_nav(draw, "SCENE 5 / 5", "BENCHMARK & INSTALL", "135,937 toks", "62,477 toks", "-54.04%")

    # Left: 20-Turn SWE Benchmark Receipt
    draw.rounded_rectangle([(50, 115), (940, 765)], radius=14, fill=WINDOW_BG, outline=ACCENT_GREEN, width=2)
    draw_check_icon(draw, 95, 160, 18, ACCENT_GREEN)
    draw.text((130, 160), "20-TURN SWE BENCHMARK RECEIPT", font=F_SANS_LG_B, fill=ACCENT_GREEN, anchor="lm")

    metrics = [
        ("Baseline Prompt Tokens:", "135,937 tokens", ACCENT_RED),
        ("With toolrecall-light:", "62,477 tokens", ACCENT_GREEN),
        ("Cumulative Tokens Saved:", "-73,460 tokens (-54.04%)", ACCENT_GREEN),
        ("Duplicate Stubs Emitted:", "11 stubs (33.6 KB deduped)", ACCENT_PURPLE),
        ("Average Recall Latency:", "0.23 ms (SQLite WAL)", ACCENT_CYAN),
        ("Real state.db Analysis:", "60.1% dupes >=2KB (74.5 MB)", ACCENT_AMBER),
    ]
    y = 225
    for label, val, col in metrics:
        draw.text((90, y), label, font=F_SANS_MD, fill=TEXT_WHITE)
        draw.text((900, y), val, font=F_MONO_MD_B, fill=col, anchor="ra")
        draw.line([(90, y + 44), (900, y + 44)], fill=BORDER_SUBTLE, width=1)
        y += 85

    # Right: Ecosystem & Security Verification
    if stage >= 1:
        draw.rounded_rectangle([(980, 115), (1870, 765)], radius=14, fill=WINDOW_BG, outline=ACCENT_CYAN, width=2)
        draw_shield_icon(draw, 1025, 160, 18, ACCENT_CYAN)
        draw.text((1060, 160), "VERIFIED COMPLIANCE & SECURITY", font=F_SANS_LG_B, fill=ACCENT_CYAN, anchor="lm")

        checks = [
            ("Hermes Plugin Admission:", "PASSED (hermes plugins validate)", ACCENT_GREEN),
            ("Unit & Integration Tests:", "22 / 22 PASSED in 0.73s", ACCENT_GREEN),
            ("Runtime Dependencies:", "ZERO (Python 3.10+ stdlib only)", ACCENT_GREEN),
            ("Catalog Rule 9 Compliance:", "Official hooks only (no monkey-patching)", ACCENT_CYAN),
            ("Security Architecture:", "SECURITY.md • Parameterized SQL • 2MB Cap", ACCENT_PURPLE),
            ("Open-Source License:", "MIT License • Robin Schultka", ACCENT_AMBER),
        ]
        y = 225
        for label, val, col in checks:
            draw.text((1020, y), label, font=F_SANS_MD, fill=TEXT_WHITE)
            draw.text((1830, y), val, font=F_MONO_SM_B, fill=col, anchor="ra")
            draw.line([(1020, y + 44), (1830, y + 44)], fill=BORDER_SUBTLE, width=1)
            y += 85

    # Bottom Install Box
    if stage >= 2:
        draw.rounded_rectangle([(50, 790), (1870, 922)], radius=14, fill=(18, 30, 48), outline=ACCENT_CYAN, width=3)
        draw.text((90, 825), "INSTALL IN ONE COMMAND VIA HERMES CLI:", font=F_SANS_SM_B, fill=ACCENT_AMBER)
        draw.text(
            (90, 868),
            "$ hermes plugins install https://github.com/whiskybeer/toolrecall-light",
            font=F_MONO_LG_B,
            fill=ACCENT_GREEN,
        )
        draw.text(
            (1830, 868),
            "toolrecall.dev",
            font=F_SANS_LG_B,
            fill=ACCENT_CYAN,
            anchor="ra",
        )

    return img


# ---------------------------------------------------------------------------
# Gemini 3.8 Flash TTS Voiceover Synthesis
# ---------------------------------------------------------------------------
SCENES = [
    {
        "id": "scene_01",
        "renderer": render_scene_1_intro,
        "voiceover": (
            "Meet ToolRecall Light: a deterministic, zero-dependency plugin for Hermes Agent. "
            "Across one hundred thirty-one thousand real Hermes tool calls, sixty percent of large outputs are exact duplicates. "
            "ToolRecall Light intercepts those repeats before they bloat your model context."
        ),
        "captions": [
            "Meet ToolRecall Light: a deterministic, zero-dependency deduplication plugin for Hermes Agent.",
            "Across 131,520 real Hermes tool calls, 60.1% of outputs >= 2 KB are exact intra-session duplicates.",
            "ToolRecall Light intercepts repeats before they enter context — saving 54% of cumulative prompt tokens.",
        ],
    },
    {
        "id": "scene_02",
        "renderer": render_scene_2_turn01,
        "voiceover": (
            "On Turn 1, the agent runs pytest in the terminal and receives a twenty-three hundred byte failure traceback. "
            "Because this output has not been seen yet, ToolRecall Light records its SHA-256 fingerprint in SQLite "
            "and passes the full text through untouched."
        ),
        "captions": [
            "Turn 01: The agent runs pytest in the terminal and receives a 2,317-byte failure traceback.",
            "The transform_tool_result hook hashes the canonical arguments and output bytes in 0.28 milliseconds.",
            "Because this is the first occurrence, the full traceback passes into model context untouched.",
        ],
    },
    {
        "id": "scene_03",
        "renderer": render_scene_3_turn04,
        "voiceover": (
            "On Turn 4, the agent re-runs the same test and gets the exact same traceback. "
            "Instead of re-sending sixty lines of duplicate text on every future turn, ToolRecall Light replaces it "
            "with a compact two-line stub—cutting turn context growth by ninety-two percent."
        ),
        "captions": [
            "Turn 04: After an edit attempt, the agent re-runs pytest and gets the exact same 2,317-byte output.",
            "ToolRecall Light intercepts the duplicate and emits a 174-byte stub with result ID 50f59afa067a.",
            "Single-turn context growth drops by 92.5% (+43 tokens instead of +579 tokens compounded every turn).",
        ],
    },
    {
        "id": "scene_04",
        "renderer": render_scene_4_turn05,
        "voiceover": (
            "Need the exact lines back? On Turn 5, the model calls recall result with the stub ID. "
            "ToolRecall Light fetches the byte-for-byte original output from local SQLite in zero point two milliseconds, "
            "so the agent spots the expired leeway parameter with zero information loss."
        ),
        "captions": [
            "Turn 05: When the model needs specific frame locals from the stubbed output, it calls recall_result.",
            "Local SQLite WAL lookup restores the exact byte-for-byte payload in 0.23 milliseconds.",
            "Zero information loss: the agent inspects 'leeway = 0' in the traceback and fixes the bug.",
        ],
    },
    {
        "id": "scene_05",
        "renderer": render_scene_5_compaction,
        "voiceover": (
            "What happens when Hermes compacts chat history? "
            "The post-compaction hook advances the session generation counter, automatically re-serving full content once "
            "so the model is never blinded by a summarized stub, backed by a five-streak loop breaker."
        ),
        "captions": [
            "Turn 10: When Hermes compacts history (/compress), the post_compaction hook increments the generation counter.",
            "Turn 11 re-serves the full output once in the new generation so the model is never left with a dangling stub.",
            "Turn 12 resumes stubbing automatically — protected by a 5-streak loop breaker and 100% fail-open safety.",
        ],
    },
    {
        "id": "scene_06",
        "renderer": render_scene_6_receipt,
        "voiceover": (
            "Across our twenty-turn software engineering benchmark, ToolRecall Light cuts cumulative prompt tokens by fifty-four percent "
            "with twenty-two passing tests and zero runtime dependencies. "
            "Install it today in one command with hermes plugins install."
        ),
        "captions": [
            "In a 20-turn SWE debugging benchmark, ToolRecall Light cuts cumulative prompt tokens by 54.04%.",
            "22/22 unit and integration tests pass with zero external dependencies and full SECURITY.md coverage.",
            "Install in one command: hermes plugins install https://github.com/whiskybeer/toolrecall-light",
        ],
    },
]


def get_gemini_api_key() -> str:
    env_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if env_key:
        return env_key.strip()
    env_file = Path.home() / ".hermes" / ".env"
    if env_file.exists():
        text = env_file.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"^(?:GEMINI_API_KEY|GOOGLE_API_KEY)=(.+)$", text, re.M)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    raise RuntimeError("GEMINI_API_KEY not found in environment or ~/.hermes/.env")


def synthesize_tts_segment(idx: int, text: str, api_key: str) -> Path:
    VOICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"Puck:{text}".encode("utf-8")).hexdigest()[:16]
    wav_path = VOICE_CACHE_DIR / f"scene_{idx:02d}_{digest}.wav"
    if wav_path.exists() and wav_path.stat().st_size > 1000:
        return wav_path

    url = "https://generativelanguage.googleapis.com/v1beta/interactions"
    payload = {
        "model": "gemini-3.8-flash-tts",
        "input": [
            {
                "type": "user_input",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                        "annotations": [
                            {
                                "type": "speech_metadata",
                                "style": "clear, confident, engaging software engineering narrator",
                            }
                        ],
                    }
                ],
            }
        ],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": "Puck"}]},
    }

    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for step in data.get("steps", []):
                for item in step.get("content", []):
                    if item.get("type") == "audio" and item.get("data"):
                        raw_wav = base64.b64decode(item["data"])
                        wav_path.write_bytes(raw_wav)
                        return wav_path
        except Exception as err:
            if attempt == 2:
                raise RuntimeError(f"Failed TTS synthesis for scene {idx}: {err}") from err
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"Empty audio returned for scene {idx}")


def get_wav_info(wav_path: Path) -> tuple[int, int, int, bytes]:
    with wave.open(str(wav_path), "rb") as wf:
        channels = wf.getnchannels()
        rate = wf.getframerate()
        sampwidth = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    return channels, rate, sampwidth, frames


def build_combined_audio_and_frames() -> None:
    t0 = time.time()
    print("1. Rendering 1920x1080 product thumbnail ->", OUTPUT_THUMB)
    thumb = render_thumbnail()
    thumb.save(OUTPUT_THUMB, optimize=True)

    print("2. Synthesizing English voiceover via Gemini 3.8 Flash TTS (parallel)...")
    api_key = get_gemini_api_key()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(synthesize_tts_segment, i + 1, scene["voiceover"], api_key)
            for i, scene in enumerate(SCENES)
        ]
        wav_paths = [f.result() for f in futures]

    # Inspect WAV segments and assemble master audio with 0.45s padding between scenes
    scene_durations: list[float] = []
    combined_pcm = bytearray()
    sample_rate = 24000
    channels = 1
    sampwidth = 2
    tail_pad_sec = 0.45

    for idx, wp in enumerate(wav_paths):
        ch, sr, sw, pcm = get_wav_info(wp)
        channels, sample_rate, sampwidth = ch, sr, sw
        bytes_per_sec = sr * ch * sw
        dur = len(pcm) / bytes_per_sec
        pad_bytes = int(tail_pad_sec * bytes_per_sec)
        # Ensure even byte alignment
        if pad_bytes % (ch * sw) != 0:
            pad_bytes += (ch * sw) - (pad_bytes % (ch * sw))
        combined_pcm.extend(pcm)
        combined_pcm.extend(b"\x00" * pad_bytes)
        total_scene_dur = dur + (pad_bytes / bytes_per_sec)
        scene_durations.append(total_scene_dur)
        print(f"   • Scene {idx + 1}: voiceover {dur:.2f}s (+{tail_pad_sec:.2f}s pause -> {total_scene_dur:.2f}s)")

    total_duration = sum(scene_durations)
    temp_dir = Path(tempfile.mkdtemp(prefix="toolrecall_light_video_"))
    master_wav = temp_dir / "voiceover_master.wav"
    with wave.open(str(master_wav), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(combined_pcm))

    print(f"3. Rendering progressive visual frames (total duration: {total_duration:.2f}s)...")
    concat_lines: list[str] = []
    elapsed = 0.0
    total_stages = len(SCENES) * 3

    for s_idx, (scene, s_dur) in enumerate(zip(SCENES, scene_durations)):
        # Split each scene into 3 progressive visual build stages (28%, 36%, 36% of scene duration)
        stage_weights = [0.28, 0.36, 0.36]
        for st_idx, weight in enumerate(stage_weights):
            st_dur = round(s_dur * weight, 3)
            elapsed += st_dur
            progress = min(elapsed / total_duration, 1.0)

            img = scene["renderer"](st_idx)
            draw = ImageDraw.Draw(img)
            step_label = f"SCENE {s_idx + 1}.{st_idx + 1} / {len(SCENES)}.3"
            draw_caption_and_progress(draw, scene["captions"][st_idx], progress, step_label)

            frame_path = temp_dir / f"frame_{s_idx + 1:02d}_{st_idx + 1}.png"
            img.save(frame_path)
            concat_lines.append(f"file '{frame_path}'")
            concat_lines.append(f"duration {st_dur:.3f}")

    # Repeat final frame for ffmpeg concat demuxer
    concat_lines.append(f"file '{temp_dir / f'frame_{len(SCENES):02d}_3.png'}'")
    concat_file = temp_dir / "concat.txt"
    concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    print("4. Encoding 1080p MP4 with H.264 video + AAC English narration...")
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-i",
        str(master_wav),
        "-vf",
        "fps=24,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(OUTPUT_VIDEO),
    ]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0:
        print("FFmpeg error:", res.stderr.decode("utf-8", errors="ignore"))
        sys.exit(1)

    vid_mb = OUTPUT_VIDEO.stat().st_size / (1024 * 1024)
    thumb_kb = OUTPUT_THUMB.stat().st_size / 1024
    print(f"\n✔ Done in {time.time() - t0:.2f}s!")
    print(f"  • Thumbnail: {OUTPUT_THUMB} ({thumb_kb:.1f} KB)")
    print(f"  • Video:     {OUTPUT_VIDEO} ({vid_mb:.2f} MB, {total_duration:.1f}s, 1080p H.264 + AAC voiceover)")


if __name__ == "__main__":
    build_combined_audio_and_frames()
