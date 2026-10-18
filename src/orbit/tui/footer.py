"""The key footer, which patches Textual's footer internals to change only the keys that
change."""

from collections.abc import Mapping
from types import MappingProxyType

from textual.binding import Binding
from textual.widget import AwaitMount
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

FooterEntry = tuple[Binding, bool, str]
"""A key the footer shows: its binding, whether it's enabled and its tooltip."""


class KeyFooter(Footer):
    """Footer that changes only the keys whose binding or state changes: Textual's rebuilds
    them all on every focus change, remounting and restyling each key. Keys that come, go
    or change state are removed or mounted one by one; grouped keys and the command
    palette's key are left to Textual's footer, which rebuilds them all."""

    # The keys last shown, with the footer's style; None before the first.
    _shown: tuple[tuple[FooterEntry, ...], bool, bool] | None = None
    # Each key's widget, in order, while this footer makes them; empty while Textual's does.
    _keys: Mapping[FooterEntry, FooterKey] = MappingProxyType({})

    async def recompose(self) -> None:
        if not self.is_attached:  # As Textual's recompose, which a closing app may call.
            return
        entries = self._entries()
        shown = (entries, self.compact, self.show_command_palette)
        if shown == self._shown:
            return
        self._shown = shown
        if self._patchable(entries):
            await self._patch(entries)
            return
        self._keys = {}
        await super().recompose()

    def _entries(self) -> tuple[FooterEntry, ...]:
        """The keys to show, the first binding of each action as Textual's footer picks
        them, then the command palette's when it shows."""
        active = self.screen.active_bindings
        firsts: dict[str, FooterEntry] = {}
        for _, binding, enabled, tooltip in active.values():
            if binding.show:
                firsts.setdefault(binding.action, (binding, enabled, tooltip))
        palette = active.get(self.app.COMMAND_PALETTE_BINDING)
        if palette is None or not self._shows_palette():
            return tuple(firsts.values())
        return (*firsts.values(), palette[1:])

    def _shows_palette(self) -> bool:
        """Whether the footer shows the command palette's key, if it's bound."""
        return self.show_command_palette and self.app.ENABLE_COMMAND_PALETTE

    def _patchable(self, entries: tuple[FooterEntry, ...]) -> bool:
        """Whether ``entries`` are shown one key widget each, in order: none grouped and no
        command palette's key."""
        return not self._shows_palette() and all(binding.group is None for binding, *_ in entries)

    def _keeps_order(self, entries: tuple[FooterEntry, ...]) -> bool:
        """Whether the keys shown that stay in ``entries`` keep their order."""
        kept = [entry for entry in self._keys if entry in entries]
        return kept == [entry for entry in entries if entry in self._keys]

    async def _patch(self, entries: tuple[FooterEntry, ...]) -> None:
        """Show ``entries``, a key widget each: remove the keys gone and mount the new ones in
        their places, or all of them anew when those staying change order or Textual's footer
        made them."""
        old = self._keys if self._keeps_order(entries) else {}
        self._keys = self._keys_for(entries, old)
        kept = set(self._keys.values())
        # Removed and mounted before awaiting either, so nothing changes in between.
        removed = self.remove_children([child for child in self.children if child not in kept])
        mounts = self._mount_new(old)
        await removed
        for mounted in mounts:
            await mounted

    def _keys_for(
        self, entries: tuple[FooterEntry, ...], old: Mapping[FooterEntry, FooterKey]
    ) -> dict[FooterEntry, FooterKey]:
        """A key widget for each of ``entries``: its widget in ``old``, or a new one."""
        return {entry: old[entry] if entry in old else self._footer_key(entry) for entry in entries}

    def _mount_new(self, old: Mapping[FooterEntry, FooterKey]) -> list[AwaitMount]:
        """Mount the keys not in ``old``, each before the key after it."""
        mounts: list[AwaitMount] = []
        before: FooterKey | None = None
        for entry, key in reversed(list(self._keys.items())):
            if entry not in old:
                mounts.append(self.mount(key, before=before))
            before = key
        return mounts

    def _footer_key(self, entry: FooterEntry) -> FooterKey:
        """A key widget for ``entry``, made as Textual's footer makes an ungrouped one."""
        binding, enabled, tooltip = entry
        key = FooterKey(
            binding.key,
            self.app.get_key_display(binding),
            binding.description,
            binding.action,
            disabled=not enabled,
            tooltip=tooltip,
        )
        # Born with the footer's style, so binding it doesn't restyle the key once mounted.
        key.set_reactive(FooterKey.compact, self.compact)
        return key.data_bind(compact=Footer.compact)
