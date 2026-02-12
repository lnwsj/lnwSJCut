import unittest

from core.shortcuts import (
    ACTION_DELETE,
    ACTION_EXPORT,
    ACTION_IMPORT,
    ACTION_REDO,
    ACTION_SAVE,
    ACTION_SHOW_SHORTCUTS,
    ACTION_SPLIT,
    ACTION_TOGGLE_PLAY_PAUSE,
    ACTION_UNDO,
    ACTION_ZOOM_IN,
    ACTION_ZOOM_OUT,
    resolve_shortcut_action,
)


class TestShortcuts(unittest.TestCase):
    def test_plain_actions(self):
        self.assertEqual(resolve_shortcut_action(key="Delete"), ACTION_DELETE)
        self.assertEqual(resolve_shortcut_action(key="Backspace"), ACTION_DELETE)
        self.assertEqual(resolve_shortcut_action(key="s"), ACTION_SPLIT)
        self.assertEqual(resolve_shortcut_action(key="space"), ACTION_TOGGLE_PLAY_PAUSE)
        self.assertEqual(resolve_shortcut_action(key="+"), ACTION_ZOOM_IN)
        self.assertEqual(resolve_shortcut_action(key="add"), ACTION_ZOOM_IN)
        self.assertEqual(resolve_shortcut_action(key="-"), ACTION_ZOOM_OUT)
        self.assertEqual(resolve_shortcut_action(key="subtract"), ACTION_ZOOM_OUT)

    def test_primary_modifier_actions_ctrl_or_meta(self):
        self.assertEqual(resolve_shortcut_action(key="s", ctrl=True), ACTION_SAVE)
        self.assertEqual(resolve_shortcut_action(key="s", meta=True), ACTION_SAVE)
        self.assertEqual(resolve_shortcut_action(key="z", ctrl=True), ACTION_UNDO)
        self.assertEqual(resolve_shortcut_action(key="y", ctrl=True), ACTION_REDO)
        self.assertEqual(resolve_shortcut_action(key="z", ctrl=True, shift=True), ACTION_REDO)
        self.assertEqual(resolve_shortcut_action(key="i", ctrl=True), ACTION_IMPORT)
        self.assertEqual(resolve_shortcut_action(key="e", ctrl=True), ACTION_EXPORT)

    def test_typing_focus_blocks_plain_shortcuts(self):
        self.assertIsNone(resolve_shortcut_action(key="s", typing_focus=True))
        self.assertIsNone(resolve_shortcut_action(key="left", typing_focus=True))
        self.assertIsNone(resolve_shortcut_action(key="delete", typing_focus=True))
        # Modifier shortcuts should still work while typing.
        self.assertEqual(resolve_shortcut_action(key="s", ctrl=True, typing_focus=True), ACTION_SAVE)

    def test_help_shortcuts(self):
        self.assertEqual(resolve_shortcut_action(key="f1"), ACTION_SHOW_SHORTCUTS)
        self.assertEqual(resolve_shortcut_action(key="?", typing_focus=True), ACTION_SHOW_SHORTCUTS)
        self.assertEqual(resolve_shortcut_action(key="/", shift=True), ACTION_SHOW_SHORTCUTS)

    def test_alt_is_ignored(self):
        self.assertIsNone(resolve_shortcut_action(key="s", alt=True))


if __name__ == "__main__":
    unittest.main()

