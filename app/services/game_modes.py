"""Display names for the main Dota game modes."""

GAME_MODE_NAMES = {1: "All Pick", 22: "Ranked All Pick", 23: "Turbo"}


def get_game_mode_name(game_mode: int | None) -> str:
    return GAME_MODE_NAMES.get(game_mode, f"Mode #{game_mode if game_mode is not None else '?'}")
