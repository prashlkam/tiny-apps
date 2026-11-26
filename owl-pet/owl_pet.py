#!/usr/bin/env python3
"""
Hoot - a desktop pet owl whose eyes follow your mouse pointer.

Made for the Cinnamon desktop (works on any X11 desktop with a compositor:
Cinnamon, MATE, Xfce, KDE on X11 ...).

Controls
  Left-drag      move the owl around
  Left-click     the owl hops and hoots "Hoo!" (with sound)
  Right-click    menu: size, sound, always on top, nap, start at login, quit

Sound plays through paplay / pw-play / GStreamer / aplay (first one that works).
If you hear nothing, run:  python3 owl_pet.py --test-sound
and check ~/.cache/owl-pet/owl.log To use your own call, put a
WAV file at ~/.config/owl-pet/hoo.wav

The owl dozes off when the mouse has not moved for a while and wakes
up as soon as you move it again.

Run:   python3 owl_pet.py
Needs: python3-gi, python3-gi-cairo, gir1.2-gtk-3.0 (Arch/CachyOS: python-gobject python-cairo gtk3)
"""

import json
import math
import os
import random
import shutil
import signal
import struct
import subprocess
import sys
import time
import wave

# Global pointer tracking needs X11; on a Wayland session this uses XWayland.
os.environ.setdefault("GDK_BACKEND", "x11")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import cairo  # noqa: E402

try:
    gi.require_foreign("cairo")
    HAVE_FOREIGN_CAIRO = True
except Exception:  # pragma: no cover
    HAVE_FOREIGN_CAIRO = False

APP_ID = "owl-pet"
APP_NAME = "Hoot the Owl"

# Design canvas (everything is drawn in these units, then scaled)
BASE_W, BASE_H = 220, 272
TOP = 32  # headroom above the owl for the speech bubble and the Zzz

EYES = ((80.0, 92.0), (140.0, 92.0))  # eye centres, owl coordinates
IRIS_R = 24.0
PUPIL_R = 11.0
FACE = (110.0, 100.0)

SIZES = {"Tiny": 0.45, "Small": 0.65, "Medium": 0.85, "Large": 1.15, "Huge": 1.5}
SLEEP_AFTER = 30.0  # seconds of mouse stillness before a nap
FPS = 30

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), APP_ID)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
AUTOSTART_FILE = os.path.join(GLib.get_user_config_dir(), "autostart", APP_ID + ".desktop")
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), APP_ID)
CUSTOM_SOUND = os.path.join(CONFIG_DIR, "hoo.wav")  # drop your own WAV here to replace the call
SOUND_VERSION = 1


def hexc(h, a=1.0):
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0, a)


C = {
    "outline": hexc("#3b2414"),
    "body": hexc("#8b5a32"),
    "body_light": hexc("#a8733f"),
    "wing": hexc("#6a4223"),
    "wing_tip": hexc("#a37446"),
    "belly": hexc("#efdcb6"),
    "chevron": hexc("#b98a58"),
    "tuft": hexc("#5e391c"),
    "tuft_in": hexc("#b78553"),
    "disc": hexc("#e6cb9c"),
    "disc_edge": hexc("#9c6b3c"),
    "socket": hexc("#2a170a"),
    "iris_in": hexc("#ffe27a"),
    "iris_out": hexc("#f29a1a"),
    "pupil": hexc("#120a04"),
    "beak": hexc("#f4a63c"),
    "beak_dark": hexc("#b86f17"),
    "feet": hexc("#f0a547"),
    "branch": hexc("#6b4627"),
    "branch_dark": hexc("#4a2f19"),
    "leaf": hexc("#6fa64a"),
    "leaf_dark": hexc("#4a7a2f"),
    "blush": hexc("#ff8a8a", 0.28),
    "lid": hexc("#c89a66"),
}


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------

def ellipse(cr, cx, cy, rx, ry):
    cr.save()
    cr.translate(cx, cy)
    cr.scale(rx, ry)
    cr.arc(0, 0, 1, 0, 2 * math.pi)
    cr.restore()


def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def fill_stroke(cr, fill, stroke=None, width=2.0):
    cr.set_source_rgba(*fill)
    if stroke:
        cr.fill_preserve()
        cr.set_source_rgba(*stroke)
        cr.set_line_width(width)
        cr.stroke()
    else:
        cr.fill()


class Pose:
    """Everything that varies between frames."""

    def __init__(self):
        self.pupils = [[0.0, 0.0], [0.0, 0.0]]
        self.pupil_scale = 1.0
        self.lid = 0.0  # 0 open .. 1 closed
        self.sleep = 0.0  # 0 awake .. 1 asleep
        self.tilt = 0.0  # head rotation, radians
        self.head_dy = 0.0
        self.breathe = 0.0
        self.bounce = 0.0
        self.brow = 0.0  # -1 sleepy .. 1 surprised
        self.bubble = 0.0
        self.t = 0.0


def draw_branch(cr):
    # leaves hanging off the left end
    for (lx, ly, ang) in ((26, 236, 0.9), (14, 226, 2.3), (196, 236, 0.5)):
        cr.save()
        cr.translate(lx, ly)
        cr.rotate(ang)
        cr.move_to(0, 0)
        cr.curve_to(6, -7, 18, -7, 24, 0)
        cr.curve_to(18, 7, 6, 7, 0, 0)
        fill_stroke(cr, C["leaf"], C["leaf_dark"], 1.5)
        cr.move_to(2, 0)
        cr.line_to(20, 0)
        cr.set_source_rgba(*C["leaf_dark"])
        cr.set_line_width(1)
        cr.stroke()
        cr.restore()
    g = cairo.LinearGradient(0, 222, 0, 238)
    g.add_color_stop_rgba(0, *hexc("#87603a"))
    g.add_color_stop_rgba(1, *C["branch"])
    rounded_rect(cr, 6, 222, 208, 15, 7)
    cr.set_source(g)
    cr.fill_preserve()
    cr.set_source_rgba(*C["branch_dark"])
    cr.set_line_width(2)
    cr.stroke()
    # bark lines and a knot
    cr.set_line_width(1.4)
    for x0 in (40, 150, 185):
        cr.move_to(x0, 228)
        cr.curve_to(x0 + 6, 226, x0 + 12, 230, x0 + 18, 228)
        cr.stroke()
    ellipse(cr, 172, 229, 4, 3)
    cr.stroke()


def draw_body(cr, p):
    cr.save()
    # breathing: stretch gently around the feet
    cr.translate(110, 224)
    cr.scale(1 - p.breathe * 0.4, 1 + p.breathe)
    cr.translate(-110, -224)

    # tail
    cr.move_to(96, 214)
    cr.line_to(110, 246)
    cr.line_to(124, 214)
    cr.close_path()
    fill_stroke(cr, C["wing"], C["outline"], 2)

    g = cairo.RadialGradient(96, 140, 8, 110, 162, 84)
    g.add_color_stop_rgba(0, *C["body_light"])
    g.add_color_stop_rgba(1, *C["body"])
    ellipse(cr, 110, 160, 64, 66)
    cr.set_source(g)
    cr.fill_preserve()
    cr.set_source_rgba(*C["outline"])
    cr.set_line_width(2.5)
    cr.stroke()

    # belly with chevron feathers
    ellipse(cr, 110, 176, 42, 44)
    fill_stroke(cr, C["belly"])
    cr.save()
    ellipse(cr, 110, 176, 42, 44)
    cr.clip()
    cr.set_source_rgba(*C["chevron"])
    cr.set_line_width(2)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    for row, y in enumerate((148, 162, 176, 190, 204)):
        off = 7 if row % 2 else 0
        for x in range(76 + off, 150, 14):
            cr.move_to(x - 4, y)
            cr.line_to(x, y + 4)
            cr.line_to(x + 4, y)
            cr.stroke()
    cr.restore()

    # wings
    for side in (-1, 1):
        cr.save()
        cr.translate(110 + side * 58, 170)
        cr.rotate(-side * 0.16)
        ellipse(cr, 0, 0, 19, 46)
        fill_stroke(cr, C["wing"], C["outline"], 2)
        cr.set_source_rgba(*C["wing_tip"])
        cr.set_line_width(1.6)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        for k in range(3):
            y = 12 + k * 10
            cr.move_to(-11 + k * 2, y)
            cr.curve_to(-4, y + 5, 4, y + 5, 11 - k * 2, y)
            cr.stroke()
        cr.restore()
    cr.restore()

    # feet gripping the branch
    for fx in (92, 128):
        for k in (-1, 0, 1):
            rounded_rect(cr, fx + k * 7 - 3, 216, 6, 13, 3)
            fill_stroke(cr, C["feet"], C["beak_dark"], 1.3)


def draw_eye(cr, i, p):
    ex, ey = EYES[i]
    # dark socket
    cr.arc(ex, ey, IRIS_R + 3, 0, 2 * math.pi)
    fill_stroke(cr, C["socket"])
    # iris
    g = cairo.RadialGradient(ex - 6, ey - 7, 2, ex, ey, IRIS_R)
    g.add_color_stop_rgba(0, *C["iris_in"])
    g.add_color_stop_rgba(1, *C["iris_out"])
    cr.arc(ex, ey, IRIS_R, 0, 2 * math.pi)
    cr.set_source(g)
    cr.fill()
    # pupil + catch-lights
    px, py = p.pupils[i]
    pr = PUPIL_R * p.pupil_scale
    cr.arc(ex + px, ey + py, pr, 0, 2 * math.pi)
    fill_stroke(cr, C["pupil"])
    cr.arc(ex + px - pr * 0.36, ey + py - pr * 0.4, pr * 0.36, 0, 2 * math.pi)
    cr.set_source_rgba(1, 1, 1, 0.95)
    cr.fill()
    cr.arc(ex + px + pr * 0.38, ey + py + pr * 0.36, pr * 0.15, 0, 2 * math.pi)
    cr.set_source_rgba(1, 1, 1, 0.75)
    cr.fill()

    # eyelid
    if p.lid > 0.01:
        r = IRIS_R + 3
        cr.save()
        cr.arc(ex, ey, r, 0, 2 * math.pi)
        cr.clip()
        edge = ey - r + 2 * r * min(1.0, p.lid)
        cr.move_to(ex - r - 1, ey - r - 1)
        cr.line_to(ex + r + 1, ey - r - 1)
        cr.line_to(ex + r + 1, edge)
        cr.curve_to(ex + r * 0.4, edge + 5, ex - r * 0.4, edge + 5, ex - r - 1, edge)
        cr.close_path()
        fill_stroke(cr, C["lid"])
        if p.lid < 0.97:
            cr.move_to(ex - r - 1, edge)
            cr.curve_to(ex - r * 0.4, edge + 5, ex + r * 0.4, edge + 5, ex + r + 1, edge)
            cr.set_source_rgba(*C["outline"])
            cr.set_line_width(2.4)
            cr.stroke()
        cr.restore()
        if p.lid >= 0.97:
            # peaceful closed-eye curve
            cr.move_to(ex - 14, ey + 2)
            cr.curve_to(ex - 6, ey + 10, ex + 6, ey + 10, ex + 14, ey + 2)
            cr.set_source_rgba(*C["outline"])
            cr.set_line_width(2.6)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.stroke()


def draw_head(cr, p):
    cr.save()
    cr.translate(110, 130 + p.head_dy)
    cr.rotate(p.tilt)
    cr.translate(-110, -130)

    # ear tufts
    for side in (-1, 1):
        cr.move_to(110 + side * 50, 62)
        cr.line_to(110 + side * 72, 16)
        cr.line_to(110 + side * 22, 44)
        cr.close_path()
        fill_stroke(cr, C["tuft"], C["outline"], 2)
        cr.move_to(110 + side * 50, 56)
        cr.line_to(110 + side * 64, 28)
        cr.line_to(110 + side * 34, 46)
        cr.close_path()
        fill_stroke(cr, C["tuft_in"])

    # head
    g = cairo.RadialGradient(98, 66, 6, 110, 88, 80)
    g.add_color_stop_rgba(0, *C["body_light"])
    g.add_color_stop_rgba(1, *C["body"])
    ellipse(cr, 110, 88, 74, 58)
    cr.set_source(g)
    cr.fill_preserve()
    cr.set_source_rgba(*C["outline"])
    cr.set_line_width(2.5)
    cr.stroke()

    # little forehead speckles
    cr.set_source_rgba(*C["tuft_in"])
    for (sx, sy) in ((100, 44), (110, 40), (120, 44), (105, 52), (115, 52)):
        ellipse(cr, sx, sy, 2.2, 3)
        cr.fill()

    # facial disc: two overlapping circles, outlined as one shape
    for width, col in ((6, C["disc_edge"]), (0, C["disc"])):
        cr.new_path()
        for (ex, ey) in EYES:
            cr.new_sub_path()
            cr.arc(ex, ey, 34, 0, 2 * math.pi)
        cr.set_source_rgba(*col)
        if width:
            cr.set_line_width(width)
            cr.stroke()
        else:
            cr.fill()

    # rosy cheeks
    for cx in (60, 160):
        ellipse(cr, cx, 118, 9, 5)
        cr.set_source_rgba(*C["blush"])
        cr.fill()

    for i in range(2):
        draw_eye(cr, i, p)

    # brows: raised when surprised, droopy when sleepy
    b = p.brow
    cr.set_source_rgba(*C["tuft"])
    cr.set_line_width(5)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for side in (-1, 1):
        inner_x, outer_x = 110 + side * 9, 110 + side * 50
        cr.move_to(inner_x, 64 - b * 4)
        cr.curve_to(110 + side * 22, 58 - b * 7, 110 + side * 38, 56 - b * 8, outer_x, 61 - b * 5 + max(0, -b) * 6)
        cr.stroke()

    # beak
    cr.move_to(110, 104)
    cr.line_to(100, 113)
    cr.curve_to(102, 121, 106, 126, 110, 131)
    cr.curve_to(114, 126, 118, 121, 120, 113)
    cr.close_path()
    fill_stroke(cr, C["beak"], C["beak_dark"], 1.6)
    cr.move_to(103, 112)
    cr.line_to(110, 108)
    cr.set_source_rgba(1, 1, 1, 0.5)
    cr.set_line_width(1.5)
    cr.stroke()

    cr.restore()


def draw_owl(cr, p):
    """Draw the owl (and branch) in owl coordinates (already offset by TOP)."""
    draw_branch(cr)
    cr.save()
    cr.translate(0, -p.bounce)
    draw_body(cr, p)
    draw_head(cr, p)
    cr.restore()


def draw_extras(cr, p):
    """Speech bubble and snores, in window coordinates."""
    if p.sleep > 0.6:
        a = (p.sleep - 0.6) / 0.4
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        for k in range(3):
            ph = (p.t * 0.35 + k / 3.0) % 1.0
            cr.set_font_size(12 + ph * 12)
            x = 168 + ph * 30 + math.sin(ph * 6 + k) * 3
            y = 62 - ph * 52
            fade = math.sin(ph * math.pi)
            cr.move_to(x, y)
            cr.text_path("z")
            cr.set_source_rgba(1, 1, 1, 0.9 * a * fade)
            cr.set_line_width(3)
            cr.stroke_preserve()
            cr.set_source_rgba(0.25, 0.3, 0.55, a * fade)
            cr.fill()

    if p.bubble > 0.01:
        a = p.bubble
        bx, by, bw, bh = 140, 3, 74, 28
        rounded_rect(cr, bx, by, bw, bh, 12)
        cr.move_to(bx + 14, by + bh - 1)
        cr.line_to(bx + 2, by + bh + 12)
        cr.line_to(bx + 26, by + bh - 1)
        cr.set_source_rgba(1, 1, 1, 0.96 * a)
        cr.fill_preserve()
        cr.set_source_rgba(*C["outline"][:3], 0.9 * a)
        cr.set_line_width(2)
        cr.stroke()
        rounded_rect(cr, bx + 1, by + 1, bw - 2, bh - 2, 11)
        cr.set_source_rgba(1, 1, 1, a)
        cr.fill()
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(16)
        cr.set_source_rgba(*C["outline"][:3], a)
        ext = cr.text_extents("Hoo!")
        cr.move_to(bx + (bw - ext.width) / 2 - ext.x_bearing, by + bh / 2 + ext.height / 2)
        cr.show_text("Hoo!")


def render_frame(cr, p, scale=1.0, extras=True):
    cr.save()
    cr.scale(scale, scale)
    cr.save()
    cr.translate(0, TOP)
    draw_owl(cr, p)
    cr.restore()
    if extras:
        draw_extras(cr, p)
    cr.restore()


def export_png(path, size=256, pose=None):
    p = pose or Pose()
    s = size / max(BASE_W, BASE_H)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(BASE_W * s), int(BASE_H * s))
    cr = cairo.Context(surf)
    render_frame(cr, p, s)
    surf.write_to_png(path)


# --------------------------------------------------------------------------
# Window / behaviour
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Sound: a synthesized "hoo ... hoo-hoooo" owl call
# --------------------------------------------------------------------------

def synth_hoot_wav(path, rate=22050):
    """Write a soft, breathy owl call to a 16-bit mono WAV (pure Python)."""
    rng = random.Random(7)
    # (start s, length s, base pitch Hz, loudness)
    hoots = [(0.00, 0.26, 410, 0.70), (0.44, 0.20, 395, 0.60), (0.70, 0.62, 372, 0.85)]
    total = int(rate * 1.45)
    buf = [0.0] * total
    for start, dur, f0, amp in hoots:
        n = int(dur * rate)
        off = int(start * rate)
        phase = 0.0
        lp = 0.0
        for i in range(n):
            t = i / rate
            x = t / dur
            # soft attack, long rounded release
            att = min(1.0, t / 0.05)
            env = math.sin(att * math.pi / 2) ** 2 * (math.cos(max(0.0, x - 0.35) / 0.65 * math.pi / 2) ** 1.5)
            # pitch swells a little, then sags at the end; gentle vibrato
            f = f0 * (1 + 0.05 * math.sin(math.pi * min(1.0, x * 1.4)) - 0.07 * x * x)
            f *= 1 + 0.008 * math.sin(2 * math.pi * 5.5 * t)
            phase += 2 * math.pi * f / rate
            tone = math.sin(phase) + 0.22 * math.sin(2 * phase) + 0.06 * math.sin(3 * phase)
            # breath: low-passed noise
            lp += 0.12 * (rng.uniform(-1, 1) - lp)
            buf[off + i] += amp * env * (tone * 0.8 + lp * 0.55)
    peak = max(abs(v) for v in buf) or 1.0
    frames = b"".join(struct.pack("<h", int(v / peak * 0.85 * 32767)) for v in buf)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


LOG_FILE = os.path.join(CACHE_DIR, "owl.log")


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, file=sys.stderr)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def sound_backends():
    """Every way we know to play a WAV, in order of preference, that exists here."""
    out = []
    for name, cmd in (
        ("paplay", ["paplay"]),
        ("pw-play", ["pw-play"]),
        ("pw-cat", ["pw-cat", "--playback"]),
        ("gstreamer", None),
        ("aplay", ["aplay", "-q"]),
        ("canberra", ["canberra-gtk-play", "-f"]),
        ("ffplay", ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error"]),
        ("mpv", ["mpv", "--no-video", "--really-quiet"]),
    ):
        if cmd is None:
            if gst_module() is not None:
                out.append((name, None))
        elif shutil.which(cmd[0]):
            out.append((name, cmd))
    return out


_GST = []


def gst_module():
    if not _GST:
        try:
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            Gst.init(None)
            _GST.append(Gst)
        except Exception:
            _GST.append(None)
    return _GST[0]


class HootSound:
    """Plays the hoot, falling back to the next player whenever one fails."""

    def __init__(self):
        self.backends = sound_backends()
        self.good = None  # name of the last backend that worked
        self.token = 0
        self.proc = None
        self.pipeline = None
        self.queue = []
        if self.backends:
            log("sound players found: " + ", ".join(n for n, _ in self.backends))
        else:
            log("no sound player found - install pipewire-pulse (paplay) or gst-plugins-base")

    def path(self):
        if os.path.isfile(CUSTOM_SOUND):
            return CUSTOM_SOUND
        p = os.path.join(CACHE_DIR, f"hoo-v{SOUND_VERSION}.wav")
        if not os.path.isfile(p) or os.path.getsize(p) < 1000:
            try:
                synth_hoot_wav(p)
            except Exception as e:
                log(f"could not create hoot sound: {e}")
                return None
        return p

    def play(self):
        path = self.path()
        if not path or not self.backends:
            return
        self.stop()
        self.token += 1
        order = list(self.backends)
        if self.good:
            order.sort(key=lambda b: b[0] != self.good)
        self.queue = order
        self._next(path, self.token)

    def _next(self, path, token):
        if token != self.token:
            return
        while self.queue:
            name, cmd = self.queue.pop(0)
            try:
                if cmd is None:
                    self._play_gst(path, token)
                else:
                    self.proc = subprocess.Popen(cmd + [path], stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    GLib.timeout_add(100, self._watch, self.proc, name, path, token)
                self.current = name
                return
            except Exception as e:
                log(f"{name}: could not start ({e})")
        log("every sound player failed - see the lines above")

    def _watch(self, proc, name, path, token):
        rc = proc.poll()
        if rc is None:
            return token == self.token  # keep watching unless superseded
        err = (proc.stderr.read() or b"").decode(errors="replace").strip()
        proc.stderr.close()
        if token != self.token or rc < 0:
            return False  # stopped on purpose
        if rc == 0:
            if self.good != name:
                log(f"sound is playing through {name}")
            self.good = name
        else:
            log(f"{name} failed (exit {rc}): {err[:300]}")
            if self.good == name:
                self.good = None
            self._next(path, token)
        return False

    def _play_gst(self, path, token):
        Gst = gst_module()
        pb = Gst.ElementFactory.make("playbin", None)
        if pb is None:
            raise RuntimeError("playbin missing (install gst-plugins-base)")
        pb.set_property("uri", GLib.filename_to_uri(os.path.abspath(path), None))
        fake_video = Gst.ElementFactory.make("fakesink", None)
        if fake_video is not None:
            pb.set_property("video-sink", fake_video)
        bus = pb.get_bus()
        bus.add_signal_watch()

        def finish():
            pb.set_state(Gst.State.NULL)
            if self.pipeline is pb:
                self.pipeline = None
            try:
                bus.remove_signal_watch()
            except Exception:
                pass

        def on_msg(_bus, msg):
            if msg.type == Gst.MessageType.EOS:
                finish()
                if self.good != "gstreamer":
                    log("sound is playing through gstreamer")
                self.good = "gstreamer"
            elif msg.type == Gst.MessageType.ERROR:
                e, dbg = msg.parse_error()
                finish()
                log(f"gstreamer failed: {e.message}")
                if self.good == "gstreamer":
                    self.good = None
                self._next(path, token)

        bus.connect("message", on_msg)
        if pb.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            finish()
            raise RuntimeError("could not start playback")
        self.pipeline = pb

    def stop(self):
        self.token += 1
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None
        if self.pipeline is not None:
            Gst = gst_module()
            pb, self.pipeline = self.pipeline, None
            pb.set_state(Gst.State.NULL)
            try:
                pb.get_bus().remove_signal_watch()
            except Exception:
                pass


def test_sound():
    """python3 owl_pet.py --test-sound : try every player, one by one, and report."""
    snd = HootSound()
    path = snd.path()
    print(f"\nHoot sound file: {path} ({os.path.getsize(path) if path else 0} bytes)")
    print("Players available:", ", ".join(n for n, _ in snd.backends) or "NONE")
    for env in ("XDG_RUNTIME_DIR", "PULSE_SERVER", "PIPEWIRE_REMOTE"):
        print(f"  {env}={os.environ.get(env, '(unset)')}")
    print()
    for name, cmd in snd.backends:
        print(f"Trying {name} ... ", end="", flush=True)
        if cmd is None:
            Gst = gst_module()
            pb = Gst.ElementFactory.make("playbin", None)
            if pb is None:
                print("FAILED: playbin missing")
                continue
            pb.set_property("uri", GLib.filename_to_uri(os.path.abspath(path), None))
            pb.set_state(Gst.State.PLAYING)
            msg = pb.get_bus().timed_pop_filtered(5 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            pb.set_state(Gst.State.NULL)
            if msg is None:
                print("timed out")
            elif msg.type == Gst.MessageType.ERROR:
                print("FAILED:", msg.parse_error()[0].message)
            else:
                print("finished OK")
        else:
            try:
                r = subprocess.run(cmd + [path], capture_output=True, timeout=6)
                if r.returncode == 0:
                    print("finished OK")
                else:
                    print(f"FAILED (exit {r.returncode}): {r.stderr.decode(errors='replace').strip()[:300]}")
            except subprocess.TimeoutExpired:
                print("timed out")
        time.sleep(0.6)
    print("\nWhich of those did you actually hear? Log file:", LOG_FILE)



def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def approach(cur, target, rate, dt):
    return cur + (target - cur) * min(1.0, rate * dt)


class OwlWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title=APP_NAME)
        self.cfg = self.load_config()
        self.size_name = self.cfg.get("size", "Medium")
        if self.size_name not in SIZES:
            self.size_name = "Medium"
        self.scale = SIZES[self.size_name]

        self.p = Pose()
        now = time.monotonic()
        self.t0 = now
        self.last = now
        self.last_move = now
        self.last_ptr = None
        self.next_blink = now + random.uniform(1.5, 4)
        self.blink_start = -10.0
        self.hoot_start = -10.0
        self.forced_nap = False
        self.press = None
        self.dragging = False
        self.sound = HootSound()
        GLib.idle_add(lambda: (self.sound.path(), False)[1])  # pre-build the WAV

        self.set_decorated(False)
        self.set_app_paintable(True)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_keep_above(self.cfg.get("on_top", True))
        self.set_accept_focus(False)
        self.stick()
        self.set_icon_name("face-smile")

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)
        else:
            print("Note: no compositor detected - the owl will have a solid background.", file=sys.stderr)

        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("draw", self.on_draw)
        self.connect("button-press-event", self.on_press)
        self.connect("button-release-event", self.on_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("destroy", self.on_quit)

        self.pointer = Gdk.Display.get_default().get_default_seat().get_pointer()
        self.apply_size()
        self.place_initially()
        self.show_all()
        self.update_input_shape()
        GLib.timeout_add(int(1000 / FPS), self.tick)

    # ---- config -------------------------------------------------------------
    def load_config(self):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def save_config(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            x, y = self.get_position()
            self.cfg.update({"x": x, "y": y, "size": self.size_name, "on_top": self.get_keep_above_flag()})
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.cfg, f, indent=2)
        except Exception as e:
            print("Could not save settings:", e, file=sys.stderr)

    def get_keep_above_flag(self):
        return self.cfg.get("on_top", True)

    # ---- geometry -----------------------------------------------------------
    def win_size(self):
        return int(math.ceil(BASE_W * self.scale)), int(math.ceil(BASE_H * self.scale))

    def apply_size(self):
        w, h = self.win_size()
        self.set_size_request(w, h)
        self.resize(w, h)

    def place_initially(self):
        w, h = self.win_size()
        if "x" in self.cfg and "y" in self.cfg:
            self.move(int(self.cfg["x"]), int(self.cfg["y"]))
            return
        display = Gdk.Display.get_default()
        mon = display.get_primary_monitor() or display.get_monitor(0)
        area = mon.get_workarea()
        self.move(area.x + area.width - w - 40, area.y + area.height - h - 20)

    def update_input_shape(self):
        """Clicks on transparent pixels fall through to whatever is underneath."""
        if not HAVE_FOREIGN_CAIRO:
            return
        try:
            w, h = self.win_size()
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
            cr = cairo.Context(surf)
            render_frame(cr, Pose(), self.scale, extras=False)
            region = Gdk.cairo_region_create_from_surface(surf)
            self.input_shape_combine_region(region)
        except Exception as e:
            print("Input shape not set:", e, file=sys.stderr)

    def origin(self):
        gw = self.get_window()
        if gw is None:
            return 0, 0
        _, x, y = gw.get_origin()
        return x, y

    # ---- animation loop -----------------------------------------------------
    def tick(self):
        now = time.monotonic()
        dt = min(0.1, now - self.last)
        self.last = now
        p = self.p
        p.t = now - self.t0

        try:
            _, mx, my = self.pointer.get_position()
        except Exception:
            mx, my = self.last_ptr or (0, 0)
        moved = (mx, my) != self.last_ptr
        if moved:
            if self.last_ptr is not None and self.forced_nap is False:
                self.last_move = now
            self.last_ptr = (mx, my)
        idle = now - self.last_move

        asleep = self.forced_nap or idle > SLEEP_AFTER
        p.sleep = approach(p.sleep, 1.0 if asleep else 0.0, 0.7 if asleep else 8.0, dt)

        wx, wy = self.origin()
        sc = self.scale

        # surprise when the pointer gets close to the face
        fx, fy = wx + FACE[0] * sc, wy + (FACE[1] + TOP) * sc
        face_d = math.hypot(mx - fx, my - fy) / sc
        near = face_d < 70 and p.sleep < 0.3
        p.pupil_scale = approach(p.pupil_scale, 1.3 if near else 1.0, 6, dt)
        brow_target = 1.0 if near else 0.0
        if p.sleep > 0.3:
            brow_target = -0.6
        p.brow = approach(p.brow, brow_target, 6, dt)

        # pupils look at the pointer
        pr = PUPIL_R * p.pupil_scale
        maxoff = IRIS_R - pr - 1.0
        for i, (ex, ey) in enumerate(EYES):
            sx = wx + ex * sc
            sy = wy + (ey + TOP + p.head_dy) * sc
            dx, dy = mx - sx, my - sy
            d = math.hypot(dx, dy)
            if d < 0.5:
                tx = ty = 0.0
            else:
                off = min(maxoff, (d / sc) * 0.35)
                tx, ty = dx / d * off, dy / d * off
            if p.sleep > 0.5:
                tx, ty = 0.0, 3.0
            p.pupils[i][0] = approach(p.pupils[i][0], tx, 18, dt)
            p.pupils[i][1] = approach(p.pupils[i][1], ty, 18, dt)

        # head turns a little toward the pointer
        hx, hy = wx + 110 * sc, wy + (TOP + 88) * sc
        tilt_t = clamp((mx - hx) / sc / 700.0, -1, 1) * 0.14
        dy_t = clamp((my - hy) / sc / 600.0, -1, 1) * 5
        if p.sleep > 0.5:
            tilt_t, dy_t = 0.16, 5
        p.tilt = approach(p.tilt, tilt_t, 5, dt)
        p.head_dy = approach(p.head_dy, dy_t, 5, dt)

        # blinking
        if now >= self.next_blink and p.sleep < 0.1:
            self.blink_start = now
            gap = random.uniform(2.5, 6.5)
            if random.random() < 0.2:
                gap = 0.28  # double blink
            self.next_blink = now + gap
        bt = now - self.blink_start
        blink = math.sin(math.pi * bt / 0.18) if bt < 0.18 else 0.0
        p.lid = max(blink, p.sleep)

        # hop + hoot
        ht = now - self.hoot_start
        p.bounce = abs(math.sin(ht * math.pi / 0.32)) * 9 if ht < 0.64 else 0.0
        if ht < 1.1:
            p.bubble = min(1.0, ht / 0.08)
        elif ht < 1.5:
            p.bubble = 1.0 - (ht - 1.1) / 0.4
        else:
            p.bubble = 0.0

        # breathing (slower while asleep)
        speed = 1.3 if p.sleep > 0.5 else 2.2
        p.breathe = math.sin(p.t * speed) * (0.022 if p.sleep > 0.5 else 0.012)

        self.queue_draw()
        return True

    def on_draw(self, _w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        render_frame(cr, self.p, self.scale)
        return False

    # ---- interaction --------------------------------------------------------
    def hoot(self, *_a):
        self.forced_nap = False
        self.last_move = time.monotonic()
        self.hoot_start = time.monotonic()
        if self.cfg.get("sound", True):
            self.sound.play()

    def on_press(self, _w, ev):
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return True
        if ev.button == 1:
            x, y = self.get_position()
            self.press = (ev.x_root, ev.y_root, x, y)
            self.dragging = False
        elif ev.button == 3:
            self.show_menu(ev)
        return True

    def on_motion(self, _w, ev):
        if self.press and (ev.state & Gdk.ModifierType.BUTTON1_MASK):
            px, py, wx, wy = self.press
            dx, dy = ev.x_root - px, ev.y_root - py
            if self.dragging or abs(dx) + abs(dy) > 4:
                self.dragging = True
                self.move(int(wx + dx), int(wy + dy))
        return True

    def on_release(self, _w, ev):
        if ev.button == 1 and self.press:
            if self.dragging:
                self.save_config()
            else:
                self.hoot()
            self.press = None
            self.dragging = False
        return True

    def show_menu(self, ev):
        menu = Gtk.Menu()

        item = Gtk.MenuItem(label="Hoot!")
        item.connect("activate", self.hoot)
        menu.append(item)

        nap = Gtk.MenuItem(label="Wake up" if self.forced_nap else "Take a nap")
        nap.connect("activate", self.toggle_nap)
        menu.append(nap)

        menu.append(Gtk.SeparatorMenuItem())

        size_item = Gtk.MenuItem(label="Size")
        size_menu = Gtk.Menu()
        group = None
        for name in SIZES:
            r = Gtk.RadioMenuItem.new_with_label_from_widget(group, name)
            group = r
            r.set_active(name == self.size_name)
            r.connect("toggled", self.on_size, name)
            size_menu.append(r)
        size_item.set_submenu(size_menu)
        menu.append(size_item)

        top = Gtk.CheckMenuItem(label="Always on top")
        top.set_active(self.get_keep_above_flag())
        top.connect("toggled", self.on_top)
        menu.append(top)

        snd = Gtk.CheckMenuItem(label="Sound")
        snd.set_active(self.cfg.get("sound", True))
        snd.connect("toggled", self.on_sound)
        menu.append(snd)

        auto = Gtk.CheckMenuItem(label="Start at login")
        auto.set_active(os.path.exists(AUTOSTART_FILE))
        auto.connect("toggled", self.on_autostart)
        menu.append(auto)

        menu.append(Gtk.SeparatorMenuItem())
        q = Gtk.MenuItem(label="Goodbye (quit)")
        q.connect("activate", self.quit)
        menu.append(q)

        menu.show_all()
        menu.attach_to_widget(self, None)
        menu.popup_at_pointer(ev)
        self._menu = menu  # keep a reference

    def toggle_nap(self, *_a):
        self.forced_nap = not self.forced_nap
        if not self.forced_nap:
            self.last_move = time.monotonic()

    def on_size(self, item, name):
        if not item.get_active() or name == self.size_name:
            return
        # keep the owl's feet where they were
        x, y = self.get_position()
        old_w, old_h = self.win_size()
        self.size_name = name
        self.scale = SIZES[name]
        new_w, new_h = self.win_size()
        self.apply_size()
        self.move(x + (old_w - new_w) // 2, y + (old_h - new_h))
        self.update_input_shape()
        GLib.timeout_add(200, lambda: (self.save_config(), False)[1])

    def on_top(self, item):
        on = item.get_active()
        self.cfg["on_top"] = on
        self.set_keep_above(on)
        self.save_config()

    def on_autostart(self, item):
        try:
            if item.get_active():
                os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
                with open(AUTOSTART_FILE, "w") as f:
                    f.write(
                        "[Desktop Entry]\n"
                        "Type=Application\n"
                        f"Name={APP_NAME}\n"
                        "Comment=A desktop pet owl that watches your mouse\n"
                        f"Exec={sys.executable} \"{os.path.abspath(__file__)}\"\n"
                        "X-GNOME-Autostart-enabled=true\n"
                        "X-GNOME-Autostart-Delay=5\n"
                    )
            elif os.path.exists(AUTOSTART_FILE):
                os.remove(AUTOSTART_FILE)
        except Exception as e:
            print("Autostart change failed:", e, file=sys.stderr)

    def on_sound(self, item):
        self.cfg["sound"] = item.get_active()
        if not item.get_active():
            self.sound.stop()
        self.save_config()

    def quit(self, *_a):
        self.save_config()
        self.sound.stop()
        self.destroy()

    def on_quit(self, *_a):
        Gtk.main_quit()


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--export-icon":
        export_png(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 256)
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--test-sound":
        test_sound()
        return
    win = OwlWindow()
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda *_: (win.quit(), False)[1])
    Gtk.main()


if __name__ == "__main__":
    main()
