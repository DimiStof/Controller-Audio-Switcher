"""Audio Switcher: switch Windows audio devices with a controller, mouse or keyboard."""
import ctypes
import os
import sys
import warnings

warnings.filterwarnings("ignore")

# These must be set before SDL starts.
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
os.environ.setdefault("SDL_JOYSTICK_HIDAPI_STEAM", "1")
os.environ.setdefault("SDL_MOUSE_FOCUS_CLICKTHROUGH", "1")

import comtypes
import pygame
from comtypes import CLSCTX_ALL, COMMETHOD, GUID, HRESULT
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

# ---------------------------------------------------------------- paths
FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)


def find_file(name):
    """Look inside the PyInstaller bundle first, then next to the program."""
    bundled = os.path.join(RES_DIR, name)
    return bundled if os.path.exists(bundled) else os.path.join(APP_DIR, name)


PIN_FILE = os.path.join(APP_DIR, "pinned_devices.txt")


# ---------------------------------------------------------------- pins and settings
# One file: lines starting with # are settings, every other line is a pinned device.
def load_pins():
    pins, pin_only, fullscreen = set(), False, False
    try:
        with open(PIN_FILE, "r", encoding="utf-8") as f:
            for line in (l.strip() for l in f):
                if line.startswith("#PIN_ONLY_MODE:"):
                    pin_only = line.split(":")[1] == "1"
                elif line.startswith("#FULLSCREEN:"):
                    fullscreen = line.split(":")[1] == "1"
                elif line:
                    pins.add(line)
    except OSError:
        pass
    return pins, pin_only, fullscreen


def save_pins(pins, pin_only, fullscreen):
    try:
        with open(PIN_FILE, "w", encoding="utf-8") as f:
            f.write(f"#PIN_ONLY_MODE:{1 if pin_only else 0}\n")
            f.write(f"#FULLSCREEN:{1 if fullscreen else 0}\n")
            f.writelines(p + "\n" for p in sorted(pins))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- audio
class IPolicyConfig(comtypes.IUnknown):
    """Windows' interface for changing the default device. Microsoft doesn't document it.

    Only SetDefaultEndpoint is called. The entries before it are placeholders
    that keep it in the correct position, so don't remove or reorder them.
    """
    _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
    _methods_ = [
        COMMETHOD([], HRESULT, name) for name in (
            "GetMixFormat", "GetDeviceFormat", "ResetDeviceFormat", "SetDeviceFormat",
            "GetProcessingPeriod", "SetProcessingPeriod", "GetShareMode", "SetShareMode",
            "GetPropertyValue", "SetPropertyValue")
    ] + [
        COMMETHOD([], HRESULT, "SetDefaultEndpoint",
                  (["in"], ctypes.c_wchar_p, "device_id"),
                  (["in"], ctypes.c_int, "role")),
    ]


POLICY_CONFIG_CLIENT = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")


def set_default_endpoint(device):
    policy = comtypes.CoCreateInstance(POLICY_CONFIG_CLIENT, IPolicyConfig, CLSCTX_ALL)
    for role in (0, 1, 2):  # console, multimedia, communications
        policy.SetDefaultEndpoint(device.id, role)


def endpoint_volume(device):
    imm = AudioUtilities.GetDeviceEnumerator().GetDevice(device.id)
    interface = imm.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return interface.QueryInterface(IAudioEndpointVolume)


def get_volume(device):
    try:
        return round(endpoint_volume(device).GetMasterVolumeLevelScalar() * 100)
    except Exception:  # unplugged or disabled devices have no volume control
        return None


def change_volume(device, amount):
    volume = endpoint_volume(device)
    # Step from the whole percentage shown, so 1% steps don't drift.
    percent = round(volume.GetMasterVolumeLevelScalar() * 100) + amount
    volume.SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100, None)


def get_mute(device):
    try:
        return bool(endpoint_volume(device).GetMute())
    except Exception:
        return False


def set_mute(device, muted):
    endpoint_volume(device).SetMute(int(muted), None)


def all_devices(pins):
    found, seen = [], set()
    for d in AudioUtilities.GetAllDevices():
        try:
            name = d.FriendlyName
        except Exception:
            continue
        if name and name not in seen:
            found.append(d)
            seen.add(name)
    found.sort(key=lambda d: (d.FriendlyName not in pins, d.FriendlyName.lower()))
    return found


def default_device():
    try:
        return AudioUtilities.GetSpeakers()
    except Exception:
        return None


# ---------------------------------------------------------------- controllers
# SDL's standard gamepad layout. SDL2 and SDL3 use the same numbering.
BUTTON_ACTIONS = {
    0: "switch",     # A / Cross
    1: "exit",       # B / Circle
    2: "reload",     # X / Square
    3: "pin",        # Y / Triangle
    4: "pin_only",   # Back / View / Share
    6: "mute",       # Start / Menu / Options
    8: "fullscreen", # Right stick click (RS / R3)
    9: "vol-5",      # LB
    10: "vol+5",     # RB
    11: "up",
    12: "down",
    13: "vol-1",     # D-pad left
    14: "vol+1",     # D-pad right
}
# Layout your original script used, for pads SDL has no mapping for.
RAW_ACTIONS = {0: "switch", 1: "exit", 2: "reload", 3: "pin", 4: "vol-5",
               5: "vol+5", 6: "pin_only", 7: "mute", 9: "fullscreen"}
STICK = 16000    # out of 32767
TRIGGER = 16000


def stick_actions(x, y, threshold):
    """Only the stronger direction counts, so scrolling doesn't nudge the volume."""
    if max(abs(x), abs(y)) <= threshold:
        return set()
    if abs(y) >= abs(x):
        return {"up" if y < 0 else "down"}
    return {"vol-1" if x < 0 else "vol+1"}


def pad_actions(button, axis):
    held = {act for b, act in BUTTON_ACTIONS.items() if button(b)}
    held |= stick_actions(axis(0), axis(1), STICK)
    if axis(4) > TRIGGER:
        held.add("vol-20")
    if axis(5) > TRIGGER:
        held.add("vol+20")
    return held


class SDL3Pads:
    """Used when SDL3.dll is present. Needed for newer pads such as the 2026 Steam Controller."""

    def __init__(self, path):
        sdl = self.sdl = ctypes.CDLL(path)
        ptr, u32, c_int, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_bool
        signatures = {
            "SDL_SetHint": (boolean, [ctypes.c_char_p, ctypes.c_char_p]),
            "SDL_Init": (boolean, [u32]),
            "SDL_SetGamepadEventsEnabled": (None, [boolean]),
            "SDL_UpdateGamepads": (None, []),
            "SDL_GetGamepads": (ctypes.POINTER(u32), [ctypes.POINTER(c_int)]),
            "SDL_OpenGamepad": (ptr, [u32]),
            "SDL_CloseGamepad": (None, [ptr]),
            "SDL_GetGamepadName": (ctypes.c_char_p, [ptr]),
            "SDL_GetGamepadButton": (boolean, [ptr, c_int]),
            "SDL_GetGamepadAxis": (ctypes.c_int16, [ptr, c_int]),
            "SDL_free": (None, [ptr]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(sdl, name)
            fn.restype, fn.argtypes = result, args
        sdl.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
        sdl.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI_STEAM", b"1")
        if not sdl.SDL_Init(0x2000):  # SDL_INIT_GAMEPAD
            raise OSError("SDL3 failed to start")
        sdl.SDL_SetGamepadEventsEnabled(False)  # we poll instead
        self.pads = {}

    def poll(self):
        sdl = self.sdl
        sdl.SDL_UpdateGamepads()
        count = ctypes.c_int(0)
        ids = sdl.SDL_GetGamepads(ctypes.byref(count))
        present = {ids[i] for i in range(count.value)} if ids else set()
        if ids:
            sdl.SDL_free(ctypes.cast(ids, ctypes.c_void_p))
        for gone in set(self.pads) - present:
            sdl.SDL_CloseGamepad(self.pads.pop(gone))
        for new in present - set(self.pads):
            pad = sdl.SDL_OpenGamepad(new)
            if pad:
                self.pads[new] = pad
        held = set()
        for pad in self.pads.values():
            held |= pad_actions(lambda b: sdl.SDL_GetGamepadButton(pad, b),
                                lambda a: sdl.SDL_GetGamepadAxis(pad, a))
        names = [(sdl.SDL_GetGamepadName(p) or b"Controller").decode("utf-8", "replace")
                 for p in self.pads.values()]
        return held, names


class PygamePads:
    """Default backend, using pygame's built-in SDL2."""

    def __init__(self):
        pygame.joystick.init()
        try:
            from pygame._sdl2 import controller
            controller.init()
            self.controller = controller
        except Exception:
            self.controller = None
        self.pads = {}  # instance id -> (is_mapped, object, name)

    def add(self, index):
        try:
            joy = pygame.joystick.Joystick(index)
            joy.init()
            key = joy.get_instance_id()
            if key in self.pads:
                return
            if self.controller and self.controller.is_controller(index):
                self.pads[key] = (True, self.controller.Controller(index), joy.get_name())
            else:
                self.pads[key] = (False, joy, joy.get_name())
        except pygame.error:
            pass

    def handle(self, event):
        if event.type == pygame.JOYDEVICEADDED:
            self.add(event.device_index)
        elif event.type == pygame.JOYDEVICEREMOVED:
            self.pads.pop(event.instance_id, None)

    def poll(self):
        held = set()
        for mapped, pad, _ in list(self.pads.values()):
            try:
                if mapped:
                    held |= pad_actions(pad.get_button, pad.get_axis)
                    continue
                count = pad.get_numbuttons()
                held |= {a for b, a in RAW_ACTIONS.items() if b < count and pad.get_button(b)}
                if pad.get_numhats():
                    x, y = pad.get_hat(0)
                    if y:
                        held.add("up" if y > 0 else "down")
                    if x:
                        held.add("vol+1" if x > 0 else "vol-1")
                axes = pad.get_numaxes()
                if axes > 1:
                    held |= stick_actions(pad.get_axis(0), pad.get_axis(1), 0.5)
                if axes > 5:
                    if pad.get_axis(4) > 0.5:
                        held.add("vol-20")
                    if pad.get_axis(5) > 0.5:
                        held.add("vol+20")
            except pygame.error:
                pass
        return held, [name for _, _, name in self.pads.values()]


def open_controllers():
    path = find_file("SDL3.dll")
    if os.path.exists(path):
        try:
            return SDL3Pads(path)
        except (OSError, AttributeError) as error:
            print("Could not use SDL3.dll, falling back to pygame:", error)
    return PygamePads()


# ---------------------------------------------------------------- look
BG = (15, 17, 23)
PANEL = (24, 27, 36)
ROW = (31, 35, 46)
HOVER = (41, 46, 60)
SELECTED = (39, 66, 102)
ACCENT = (92, 164, 255)
TEXT = (232, 235, 242)
DIM = (139, 147, 165)
GREEN = (92, 214, 140)
YELLOW = (244, 205, 88)
RED = (239, 100, 100)
# Xbox face button colours, drawn as see-through tints
A_GREEN = (80, 200, 100)
B_RED = (240, 90, 90)
X_BLUE = (70, 150, 245)
Y_YELLOW = (245, 200, 70)


def blend(color, under, alpha):
    """The colour you get by laying `color` over `under` at this opacity (0 to 1)."""
    return tuple(round(c * alpha + u * (1 - alpha)) for c, u in zip(color, under))

KEYS = {
    pygame.K_UP: "up", pygame.K_DOWN: "down", pygame.K_RETURN: "switch",
    pygame.K_KP_ENTER: "switch", pygame.K_ESCAPE: "exit", pygame.K_r: "reload",
    pygame.K_p: "pin", pygame.K_f: "pin_only", pygame.K_m: "mute",
    pygame.K_LEFT: "vol-1", pygame.K_RIGHT: "vol+1",
    pygame.K_MINUS: "vol-5", pygame.K_EQUALS: "vol+5",
    pygame.K_PAGEDOWN: "vol-20", pygame.K_PAGEUP: "vol+20",
    pygame.K_F11: "fullscreen",
}
REPEATING = {"up", "down", "vol-1", "vol+1", "vol-5", "vol+5", "vol-20", "vol+20"}
REPEAT_DELAY, REPEAT_EVERY = 300, 110  # milliseconds

# Design sizes. Everything is drawn relative to whichever one fits the window best.
WIDE = (880, 600)  # controls beside the device list
TALL = (520, 900)  # controls under the device list


def pick_layout(width, height):
    """Return (is_tall, scale) for a window of this size."""
    wide = min(width / WIDE[0], height / WIDE[1])
    tall = min(width / TALL[0], height / TALL[1])
    return tall > wide, max(0.4, wide, tall)


def initial_size(desktop_w, desktop_h):
    """A starting window suited to the screen's resolution and orientation."""
    if desktop_h > desktop_w:
        design, share = TALL, (0.85, 0.75)
    else:
        design, share = WIDE, (0.6, 0.7)
    scale = min(desktop_w * share[0] / design[0], desktop_h * share[1] / design[1])
    return int(design[0] * scale), int(design[1] * scale)


def make_dpi_aware():
    """Stop Windows from stretching (and blurring) the window."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per monitor
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


class App:
    def __init__(self):
        make_dpi_aware()
        pygame.init()
        self.window_size = initial_size(*pygame.display.get_desktop_sizes()[0])
        self.pins, self.pin_only, self.fullscreen = load_pins()  # same as last time
        self.screen = self.open_window()
        pygame.display.set_caption("Audio Switcher")
        self.tall, self.scale = pick_layout(*self.window_size)
        self.fonts = {}
        self.switch_sound = self.load_sound("switch.wav")
        self.selected_sound = self.load_sound("selected.wav", 0.3)
        self.controllers = open_controllers()
        self.pad_names = []
        self.pad_held = {}  # action -> time of next repeat

        self.devices = []
        self.selected = 0
        self.scroll = 0
        self.volume = None
        self.message = ""
        self.running = True
        self.buttons = []  # (rect, callback), rebuilt every frame
        self.list_rect = pygame.Rect(0, 0, 0, 0)
        self.visible = 1
        self.last_action = ("", "", -1000)
        self.last_click = (None, 0)
        self.reload()
        current = default_device()
        self.muted = get_mute(current) if current else False
        self.volume = get_volume(current) if current else None

    def s(self, value):
        return int(round(value * self.scale))

    def open_window(self):
        if self.fullscreen:
            return pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        return pygame.display.set_mode(self.window_size, pygame.RESIZABLE)

    def toggle_fullscreen(self):
        if not self.fullscreen:
            self.window_size = self.screen.get_size()  # to restore later
        self.fullscreen = not self.fullscreen
        self.screen = self.open_window()
        self.save()

    @staticmethod
    def load_sound(name, volume=1.0):
        try:
            sound = pygame.mixer.Sound(find_file(name))
            sound.set_volume(volume)
            return sound
        except (pygame.error, FileNotFoundError):
            return None

    @staticmethod
    def play(sound):
        if sound:
            sound.play()

    # ------------------------------------------------------------ actions
    def current(self):
        return self.devices[self.selected] if self.devices else None

    def reload(self):
        try:
            devices = all_devices(self.pins)
        except Exception as error:
            self.message = f"Could not list devices: {error}"
            devices = []
        if self.pin_only:
            devices = [d for d in devices if d.FriendlyName in self.pins]
        self.devices = devices
        self.selected = self.scroll = 0
        default = default_device()
        self.default_name = getattr(default, "FriendlyName", None)

    def keep_visible(self):
        if self.selected < self.scroll:
            self.scroll = self.selected
        elif self.selected >= self.scroll + self.visible:
            self.scroll = self.selected - self.visible + 1

    def attempt(self, what, function, *args):
        """Run an audio command. Show a message instead of crashing if Windows refuses."""
        try:
            function(*args)
        except Exception as error:
            print(f"{what} failed: {error}")
            self.message = f"{what} failed. Is the device connected?"
            return False
        self.message = ""
        return True

    def set_default(self, device):
        ok = self.attempt("Switching", set_default_endpoint, device)
        if ok:
            self.default_name = device.FriendlyName
        return ok

    def do(self, action, source="mouse"):
        # Steam Input can send one press as both a key and a button. Count it once.
        now = pygame.time.get_ticks()
        last, last_source, when = self.last_action
        if action == last and source != last_source and now - when < 60:
            return
        self.last_action = (action, source, now)

        device = self.current()
        if action == "exit":
            self.running = False
        elif action == "fullscreen":
            self.toggle_fullscreen()
        elif action == "reload":
            self.message = ""
            self.reload()
            self.volume = None
        elif action == "pin_only":
            self.pin_only = not self.pin_only
            self.save()
            self.reload()
            self.play(self.switch_sound)
        elif device is None:
            return
        elif action in ("up", "down"):
            self.selected = (self.selected + (1 if action == "down" else -1)) % len(self.devices)
            self.keep_visible()
            self.play(self.switch_sound)
        elif action == "switch":
            if self.set_default(device):
                self.play(self.selected_sound)
            self.volume = get_volume(device)
            self.muted = get_mute(device)
        elif action == "pin":
            self.pins ^= {device.FriendlyName}
            self.save()
            self.reload()
            self.play(self.switch_sound)
        elif action.startswith("vol"):
            if self.attempt("Volume change", change_volume, device, int(action[3:])):
                self.play(self.switch_sound)
            self.volume = get_volume(device)
        elif action == "mute":
            # Same as the original: make the highlighted device default, then toggle.
            if not self.set_default(device):
                return
            if self.attempt("Mute", set_mute, device, not self.muted):
                self.muted = not self.muted
                self.play(self.selected_sound)

    def save(self):
        if not save_pins(self.pins, self.pin_only, self.fullscreen):
            self.message = "Failed to save pins and settings"

    # ------------------------------------------------------------ input
    def handle_events(self):
        for event in pygame.event.get():
            if isinstance(self.controllers, PygamePads):
                self.controllers.handle(event)
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN and event.key in KEYS:
                self.do(KEYS[event.key], "key")
            elif event.type == pygame.MOUSEWHEEL:
                limit = max(0, len(self.devices) - self.visible)
                self.scroll = max(0, min(limit, self.scroll - event.y))
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                for rect, callback in reversed(self.buttons):  # topmost first
                    if rect.collidepoint(event.pos):
                        callback()
                        break

    def handle_controllers(self):
        held, self.pad_names = self.controllers.poll()
        now = pygame.time.get_ticks()
        for action in sorted(held):
            if action not in self.pad_held:
                self.pad_held[action] = now + REPEAT_DELAY
                self.do(action, "pad")
            elif action in REPEATING and now >= self.pad_held[action]:
                self.pad_held[action] = now + REPEAT_EVERY
                self.do(action, "pad")
        for action in list(self.pad_held):
            if action not in held:
                del self.pad_held[action]

    def click_row(self, index):
        """One click highlights a device, a second quick click switches to it."""
        now = pygame.time.get_ticks()
        last_index, when = self.last_click
        self.selected = index
        if index == last_index and now - when < 400:
            self.last_click = (None, 0)
            self.do("switch")
        else:
            self.last_click = (index, now)
            self.play(self.switch_sound)

    # ------------------------------------------------------------ drawing
    def font(self, size, bold=False):
        key = (size, bold)
        if key not in self.fonts:
            self.fonts[key] = pygame.font.SysFont("segoeui,arial", self.s(size), bold=bold)
        return self.fonts[key]

    def text(self, value, pos, size=16, color=TEXT, bold=False, anchor="topleft", width=None):
        font = self.font(size, bold)
        if width and font.size(value)[0] > width:
            while value and font.size(value + "…")[0] > width:
                value = value[:-1]
            value += "…"
        image = font.render(value, True, color)
        self.screen.blit(image, image.get_rect(**{anchor: pos}))

    def box(self, rect, color, radius=10, border=0):
        pygame.draw.rect(self.screen, color, rect, border, border_radius=self.s(radius))

    def button(self, rect, label, hint, action, tint=None, text_color=TEXT):
        """A grey button, or with a tint, a see-through coloured one."""
        hover = rect.collidepoint(pygame.mouse.get_pos())
        if tint:
            self.box(rect, blend(tint, BG, 0.34 if hover else 0.20))
            hint_color = blend(tint, TEXT, 0.65)
        else:
            self.box(rect, HOVER if hover else ROW)
            hint_color = DIM
        if hint:
            self.text(label, (rect.centerx, rect.centery - self.s(7)), 16, text_color, True, "center")
            self.text(hint, (rect.centerx, rect.centery + self.s(12)), 12, hint_color, anchor="center")
        else:
            self.text(label, rect.center, 16, text_color, True, "center")
        self.buttons.append((rect, lambda: self.do(action)))

    def star(self, center, color, filled):
        points = [(0, -9), (2.4, -3), (8.6, -2.8), (3.8, 1.2), (5.3, 7.4),
                  (0, 3.8), (-5.3, 7.4), (-3.8, 1.2), (-8.6, -2.8), (-2.4, -3)]
        cx, cy = center
        pygame.draw.polygon(self.screen, color,
                            [(cx + self.s(x), cy + self.s(y)) for x, y in points],
                            0 if filled else max(1, self.s(1)))

    def layout(self):
        """Work out the scale and where the three main areas go."""
        width, height = self.screen.get_size()
        tall, scale = pick_layout(width, height)
        if scale != self.scale:
            self.fonts.clear()  # re-render text at the new size so it stays sharp
        self.tall, self.scale = tall, scale
        s = self.s
        pad, top = s(20), s(68)
        # Very wide or very tall windows: centre the content instead of stretching it.
        content_w = min(width, s(TALL[0] * 1.3 if tall else WIDE[1] * 2))
        content_h = min(height, s(TALL[1] * 1.3)) if tall else height
        page = pygame.Rect((width - content_w) // 2, (height - content_h) // 2, content_w, content_h)
        if tall:
            side = page.w - pad * 2
            controls_h = s(228 + 12 + 168 + 30)
            left = pygame.Rect(page.x + pad, page.y + top, side,
                               page.h - top - controls_h - pad * 2)
            controls = (left.x, left.bottom + pad)
        else:
            side = s(300)
            left = pygame.Rect(page.x + pad, page.y + top, page.w - side - pad * 3,
                               page.h - top - pad)
            controls = (left.right + pad, left.y)
        return page, left, controls, side

    def draw(self):
        page, left, (x, controls_y), side = self.layout()
        s = self.s
        self.screen.fill(BG)
        self.buttons = []
        pad = s(20)

        # Header
        self.text("Audio Switcher", (page.x + pad, page.y + s(14)), 28, bold=True)
        if self.pad_names:
            status = self.pad_names[0]
            if len(self.pad_names) > 1:
                status += f"  +{len(self.pad_names) - 1}"
            color = GREEN
        else:
            status, color = "No controller connected", DIM
        room = page.w - s(280)
        self.text(status, (page.right - pad, page.y + s(33)), 14, color, anchor="midright", width=room)
        dot_x = page.right - pad - min(self.font(14).size(status)[0], room) - s(12)
        pygame.draw.circle(self.screen, color, (dot_x, page.y + s(34)), s(4))

        # Device list
        self.box(left, PANEL, 14)
        self.text("DEVICES", (left.x + s(16), left.y + s(14)), 13, DIM, True)
        toggle = pygame.Rect(left.right - s(190), left.y + s(8), s(180), s(30))
        self.button(toggle, f"Pinned only: {'ON' if self.pin_only else 'OFF'}", "",
                    "pin_only", text_color=GREEN if self.pin_only else DIM)

        self.list_rect = pygame.Rect(left.x + s(10), left.y + s(48), left.w - s(20), left.h - s(58))
        row_h, step = s(52), s(58)
        self.visible = max(1, (self.list_rect.h + step - row_h) // step)
        self.scroll = max(0, min(self.scroll, len(self.devices) - self.visible))
        mouse = pygame.mouse.get_pos()

        if not self.devices:
            hint = "Nothing pinned. Turn off Pinned only." if self.pin_only else "No devices. Press Reload."
            self.text(hint, self.list_rect.center, 16, DIM, anchor="center", width=self.list_rect.w)

        scrolling = len(self.devices) > self.visible
        for index in range(self.scroll, min(len(self.devices), self.scroll + self.visible)):
            name = self.devices[index].FriendlyName
            row = pygame.Rect(self.list_rect.x, self.list_rect.y + (index - self.scroll) * step,
                              self.list_rect.w - (s(10) if scrolling else 0), row_h)
            chosen = index == self.selected
            self.box(row, SELECTED if chosen else HOVER if row.collidepoint(mouse) else ROW)
            if chosen:
                self.box(row, ACCENT, border=max(2, s(2)))
            pinned, is_default = name in self.pins, name == self.default_name
            color = GREEN if is_default else YELLOW if pinned else TEXT
            badge = s(80) if is_default else 0
            self.text(name, (row.x + s(50), row.centery), 17, color, anchor="midleft",
                      width=row.w - s(62) - badge)
            if is_default:
                tag = pygame.Rect(row.right - s(82), row.centery - s(11), s(70), s(22))
                self.box(tag, (30, 66, 48), 11)
                self.text("DEFAULT", tag.center, 11, GREEN, True, "center")
            star_area = pygame.Rect(row.x + s(8), row.y + s(8), s(36), s(36))
            self.star(star_area.center, YELLOW if pinned else DIM, pinned)
            self.buttons.append((row, lambda i=index: self.click_row(i)))
            self.buttons.append((star_area, lambda i=index: (setattr(self, "selected", i), self.do("pin"))))

        if scrolling:
            track = pygame.Rect(self.list_rect.right - s(4), self.list_rect.y, s(4), self.list_rect.h)
            self.box(track, ROW, 2)
            thumb_h = max(s(24), track.h * self.visible // len(self.devices))
            offset = (track.h - thumb_h) * self.scroll // (len(self.devices) - self.visible)
            self.box(pygame.Rect(track.x, track.y + offset, track.w, thumb_h), DIM, 2)

        # Volume panel
        panel = pygame.Rect(x, controls_y, side, s(228))
        self.box(panel, PANEL, 14)
        self.text("VOLUME", (x + s(16), panel.y + s(14)), 13, DIM, True)
        if self.muted:
            tag = pygame.Rect(panel.right - s(86), panel.y + s(10), s(70), s(24))
            self.box(tag, (76, 34, 38), 12)
            self.text("MUTED", tag.center, 12, RED, True, "center")
        value = "N/A" if self.volume is None else f"{self.volume:g}%"
        self.text(value, (panel.centerx, panel.y + s(68)), 46, RED if self.muted else TEXT, True, "center")
        bar = pygame.Rect(x + s(16), panel.y + s(108), side - s(32), s(8))
        self.box(bar, ROW, 4)
        if self.volume:
            self.box(pygame.Rect(bar.x, bar.y, int(bar.w * self.volume / 100), bar.h),
                     RED if self.muted else ACCENT, 4)
        gap = s(8)
        each = (side - s(32) - gap * 3) // 4
        for n, (label, hint, action) in enumerate([("-20", "LT", "vol-20"), ("-5", "LB", "vol-5"),
                                                   ("+5", "RB", "vol+5"), ("+20", "RT", "vol+20")]):
            self.button(pygame.Rect(x + s(16) + n * (each + gap), panel.y + s(132), each, s(46)),
                        label, hint, action)
        self.button(pygame.Rect(x + s(16), panel.y + s(186), side - s(32), s(32)),
                    f"{'Unmute' if self.muted else 'Mute'} (Start)", "", "mute")

        # Action buttons
        y = panel.bottom + s(12)
        self.button(pygame.Rect(x, y, side, s(56)), "Switch to device", "A  /  Enter  /  double-click",
                    "switch", A_GREEN)
        half = (side - gap) // 2
        device = self.current()
        pinned = device is not None and device.FriendlyName in self.pins
        self.button(pygame.Rect(x, y + s(64), half, s(50)), "Unpin" if pinned else "Pin", "Y  /  P", "pin", Y_YELLOW)
        self.button(pygame.Rect(x + half + gap, y + s(64), half, s(50)), "Reload", "X  /  R", "reload", X_BLUE)
        self.button(pygame.Rect(x, y + s(122), side, s(46)), "Exit", "B  /  Esc", "exit", B_RED)

        footer = self.message or "Left / Right: ±1%   Back / F: Pinned   R3 / F11: fullscreen"
        self.text(footer, (x + side // 2, page.bottom - pad - s(6)), 12,
                  RED if self.message else DIM, anchor="center", width=side)
        pygame.display.flip()

    # ------------------------------------------------------------ loop
    def run(self):
        clock = pygame.time.Clock()
        while self.running:
            self.handle_events()
            self.handle_controllers()
            if self.running:
                self.draw()
            clock.tick(60)
        pygame.quit()


if __name__ == "__main__":
    App().run()
