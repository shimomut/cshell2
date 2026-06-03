"""Color scheme support."""

from __future__ import annotations

from dataclasses import dataclass, field


def _fg(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"


def _bg(r: int, g: int, b: int) -> str:
    return f"\033[48;2;{r};{g};{b}m"


@dataclass
class ColorScheme:
    # Prompt
    prompt_context: tuple[int, int, int] = (0, 188, 212)
    prompt_path: tuple[int, int, int] = (100, 149, 237)
    prompt_time: tuple[int, int, int] = (80, 200, 100)
    prompt_bg_count: tuple[int, int, int] = (229, 192, 123)
    # Picker
    picker_row_bg: tuple[int, int, int] = (68, 68, 68)
    picker_row_fg: tuple[int, int, int] = (220, 220, 220)
    picker_sel_bg: tuple[int, int, int] = (0, 95, 135)
    picker_sel_fg: tuple[int, int, int] = (255, 255, 255)
    picker_scroll_thumb: tuple[int, int, int] = (128, 128, 128)
    picker_scroll_track: tuple[int, int, int] = (48, 48, 48)
    # Status bar (bottom line shown while TUI is active)
    statusbar_bg: tuple[int, int, int] = (30, 30, 30)
    statusbar_fg: tuple[int, int, int] = (130, 130, 130)


SCHEMES: dict[str, ColorScheme] = {
    "dark": ColorScheme(),
    "light": ColorScheme(
        prompt_context=(0, 150, 170),
        prompt_path=(30, 80, 200),
        prompt_time=(30, 140, 60),
        prompt_bg_count=(180, 100, 0),
        picker_row_bg=(220, 220, 220),
        picker_row_fg=(30, 30, 30),
        picker_sel_bg=(0, 100, 180),
        picker_sel_fg=(255, 255, 255),
        picker_scroll_thumb=(160, 160, 160),
        picker_scroll_track=(200, 200, 200),
        statusbar_bg=(195, 210, 225),
        statusbar_fg=(60, 60, 80),
    ),
}

_active: ColorScheme = SCHEMES["dark"]


def set_color_scheme(scheme: str | ColorScheme) -> None:
    """Set the active color scheme by name or ColorScheme instance.

    Available built-in names: "dark", "light".
    """
    global _active
    if isinstance(scheme, str):
        if scheme not in SCHEMES:
            raise ValueError(f"Unknown color scheme {scheme!r}. Available: {sorted(SCHEMES)}")
        _active = SCHEMES[scheme]
    else:
        _active = scheme


def get_color_scheme() -> ColorScheme:
    return _active
