"""Interactive button views for WheelBot trade approval flow."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ui import Button, View, button

from data.models import Signal, SignalAction, SignalStatus
from discord_bot.embeds import alert_embed
from utils.logger import get_logger

if TYPE_CHECKING:
    from engine.signal import SignalQueue

log = get_logger(__name__)

APPROVAL_TIMEOUT = 14400  # 4 hours in seconds


# ── Helpers ───────────────────────────────────────────────────────────────


def _disable_all_buttons(view: View) -> None:
    """Disable every button child in a view."""
    for child in view.children:
        if isinstance(child, Button):
            child.disabled = True


async def _edit_embed_footer(
    interaction: discord.Interaction,
    view: View,
    status_text: str,
) -> None:
    """Append status text to the embed footer and disable buttons."""
    _disable_all_buttons(view)
    message = interaction.message
    if message and message.embeds:
        embed = message.embeds[0].copy()
        existing = embed.footer.text if embed.footer else ""
        embed.set_footer(text=f"{existing} | {status_text}")
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.edit_message(view=view)


# ── Trade Approval View ──────────────────────────────────────────────────


class TradeApprovalView(View):
    """Approve / Deny / Details buttons for a new trade signal."""

    def __init__(
        self,
        signal: Signal,
        signal_queue: SignalQueue,
        executor: object,
    ) -> None:
        super().__init__(timeout=APPROVAL_TIMEOUT)
        self.signal = signal
        self.signal_id: int = signal.id  # type: ignore[assignment]
        self.signal_queue = signal_queue
        self.executor = executor

    # ── Approve ───────────────────────────────────────────────────────

    @button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_button(
        self,
        interaction: discord.Interaction,
        btn: Button,
    ) -> None:
        log.info("Signal #%d approved by %s", self.signal_id, interaction.user)
        self.signal_queue.approve(self.signal_id)

        try:
            if self.executor is not None:
                # Execute — handle both sync and async executors
                result = await discord.utils.maybe_coroutine(
                    self.executor.execute_signal, self.signal,
                )
                self.signal_queue.mark_executed(self.signal_id)
                await _edit_embed_footer(interaction, self, "✅ APPROVED & EXECUTED")
                log.info("Signal #%d executed successfully", self.signal_id)
            else:
                # No executor (paper mode without broker)
                self.signal_queue.mark_executed(self.signal_id)
                await _edit_embed_footer(interaction, self, "✅ APPROVED (paper mode)")
                log.info("Signal #%d approved in paper mode (no executor)", self.signal_id)
        except Exception as exc:
            log.error("Execution failed for signal #%d: %s", self.signal_id, exc)
            error_embed = alert_embed(
                "Execution Error",
                f"Signal #{self.signal_id} approved but execution failed:\n```{exc}```",
                level="error",
            )
            await _edit_embed_footer(interaction, self, "⚠️ APPROVED but execution failed")
            await interaction.followup.send(embed=error_embed, ephemeral=True)

    # ── Deny ──────────────────────────────────────────────────────────

    @button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny_button(
        self,
        interaction: discord.Interaction,
        btn: Button,
    ) -> None:
        log.info("Signal #%d denied by %s", self.signal_id, interaction.user)
        self.signal_queue.deny(self.signal_id)
        await _edit_embed_footer(interaction, self, "❌ DENIED")

    # ── Details ───────────────────────────────────────────────────────

    @button(label="Details", style=discord.ButtonStyle.secondary, emoji="🔍")
    async def details_button(
        self,
        interaction: discord.Interaction,
        btn: Button,
    ) -> None:
        log.info("Details requested for signal #%d", self.signal_id)
        try:
            # Attempt to fetch expanded data from the broker
            broker = getattr(self.executor, "broker", None)
            details_lines = [f"**Signal #{self.signal_id} — {self.signal.symbol}**"]

            if broker and hasattr(broker, "get_option_chain"):
                chain = await discord.utils.maybe_coroutine(
                    broker.get_option_chain,
                    self.signal.symbol,
                )
                details_lines.append(f"Options chain entries: {len(chain) if chain else 0}")

            if broker and hasattr(broker, "get_greeks"):
                greeks = await discord.utils.maybe_coroutine(
                    broker.get_greeks,
                    self.signal.symbol,
                    self.signal.strike,
                    self.signal.expiration_date,
                    self.signal.option_type,
                )
                if greeks:
                    details_lines.append(
                        f"Delta: {greeks.get('delta', 'N/A')} | "
                        f"Theta: {greeks.get('theta', 'N/A')} | "
                        f"Gamma: {greeks.get('gamma', 'N/A')} | "
                        f"Vega: {greeks.get('vega', 'N/A')}"
                    )

            details_text = "\n".join(details_lines)
            await interaction.response.send_message(
                details_text,
                ephemeral=True,
            )
        except Exception as exc:
            log.error("Failed to fetch details for signal #%d: %s", self.signal_id, exc)
            await interaction.response.send_message(
                f"Could not fetch details: {exc}",
                ephemeral=True,
            )

    # ── Timeout ───────────────────────────────────────────────────────

    async def on_timeout(self) -> None:
        log.info("Signal #%d timed out", self.signal_id)
        from data.models import SignalStatus
        from data import database as db

        db.update_signal(self.signal_id, status=SignalStatus.EXPIRED.value)

        _disable_all_buttons(self)
        # We need the message reference to edit it; stored by the bot after sending
        if hasattr(self, "message") and self.message:
            try:
                if self.message.embeds:
                    embed = self.message.embeds[0].copy()
                    existing = embed.footer.text if embed.footer else ""
                    embed.set_footer(text=f"{existing} | ⏰ EXPIRED")
                    await self.message.edit(embed=embed, view=self)
                else:
                    await self.message.edit(view=self)
            except discord.HTTPException as exc:
                log.warning("Could not edit expired message: %s", exc)


# ── Roll Approval View ───────────────────────────────────────────────────


class RollApprovalView(View):
    """Approve Roll / Keep Current buttons for a roll recommendation."""

    def __init__(
        self,
        signal: Signal,
        signal_queue: SignalQueue,
        executor: object,
    ) -> None:
        super().__init__(timeout=APPROVAL_TIMEOUT)
        self.signal = signal
        self.signal_id: int = signal.id  # type: ignore[assignment]
        self.signal_queue = signal_queue
        self.executor = executor

    @button(label="Approve Roll", style=discord.ButtonStyle.success, emoji="🔄")
    async def approve_roll_button(
        self,
        interaction: discord.Interaction,
        btn: Button,
    ) -> None:
        log.info("Roll #%d approved by %s", self.signal_id, interaction.user)
        self.signal_queue.approve(self.signal_id)

        try:
            await self.executor.execute_signal(self.signal)  # type: ignore[attr-defined]
            await _edit_embed_footer(interaction, self, "✅ ROLL APPROVED — executing")
        except Exception as exc:
            log.error("Roll execution failed for signal #%d: %s", self.signal_id, exc)
            for child in self.children:
                if isinstance(child, Button):
                    child.disabled = False
            error_embed = alert_embed(
                "Roll Execution Error",
                f"Roll #{self.signal_id} approved but failed:\n```{exc}```",
                level="error",
            )
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(embed=error_embed, ephemeral=True)

    @button(label="Keep Current", style=discord.ButtonStyle.danger, emoji="✋")
    async def keep_current_button(
        self,
        interaction: discord.Interaction,
        btn: Button,
    ) -> None:
        log.info("Roll #%d denied (keep current) by %s", self.signal_id, interaction.user)
        self.signal_queue.deny(self.signal_id)
        await _edit_embed_footer(interaction, self, "✋ KEEPING CURRENT POSITION")

    async def on_timeout(self) -> None:
        log.info("Roll signal #%d timed out", self.signal_id)
        from data import database as db

        db.update_signal(self.signal_id, status=SignalStatus.EXPIRED.value)
        _disable_all_buttons(self)
        if hasattr(self, "message") and self.message:
            try:
                if self.message.embeds:
                    embed = self.message.embeds[0].copy()
                    existing = embed.footer.text if embed.footer else ""
                    embed.set_footer(text=f"{existing} | ⏰ EXPIRED")
                    await self.message.edit(embed=embed, view=self)
                else:
                    await self.message.edit(view=self)
            except discord.HTTPException as exc:
                log.warning("Could not edit expired roll message: %s", exc)


# ── LEAPS Approval View ──────────────────────────────────────────────────


class LEAPSApprovalView(View):
    """Buy LEAPS / Skip buttons for initial LEAPS purchase."""

    def __init__(
        self,
        signal: Signal,
        signal_queue: SignalQueue,
        executor: object,
    ) -> None:
        super().__init__(timeout=APPROVAL_TIMEOUT)
        self.signal = signal
        self.signal_id: int = signal.id  # type: ignore[assignment]
        self.signal_queue = signal_queue
        self.executor = executor

    @button(label="Buy LEAPS", style=discord.ButtonStyle.success, emoji="📈")
    async def buy_leaps_button(
        self,
        interaction: discord.Interaction,
        btn: Button,
    ) -> None:
        log.info("LEAPS #%d approved by %s", self.signal_id, interaction.user)
        self.signal_queue.approve(self.signal_id)

        cost_str = f"${abs(self.signal.limit_price or 0) * 100:,.2f}"
        try:
            await self.executor.execute_signal(self.signal)  # type: ignore[attr-defined]
            await _edit_embed_footer(
                interaction, self, f"✅ LEAPS PURCHASED — cost {cost_str}",
            )
        except Exception as exc:
            log.error("LEAPS execution failed for signal #%d: %s", self.signal_id, exc)
            for child in self.children:
                if isinstance(child, Button):
                    child.disabled = False
            error_embed = alert_embed(
                "LEAPS Execution Error",
                f"LEAPS #{self.signal_id} approved but failed:\n```{exc}```",
                level="error",
            )
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(embed=error_embed, ephemeral=True)

    @button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip_button(
        self,
        interaction: discord.Interaction,
        btn: Button,
    ) -> None:
        log.info("LEAPS #%d skipped by %s", self.signal_id, interaction.user)
        self.signal_queue.deny(self.signal_id)
        await _edit_embed_footer(interaction, self, "⏭️ SKIPPED")

    async def on_timeout(self) -> None:
        log.info("LEAPS signal #%d timed out", self.signal_id)
        from data import database as db

        db.update_signal(self.signal_id, status=SignalStatus.EXPIRED.value)
        _disable_all_buttons(self)
        if hasattr(self, "message") and self.message:
            try:
                if self.message.embeds:
                    embed = self.message.embeds[0].copy()
                    existing = embed.footer.text if embed.footer else ""
                    embed.set_footer(text=f"{existing} | ⏰ EXPIRED")
                    await self.message.edit(embed=embed, view=self)
                else:
                    await self.message.edit(view=self)
            except discord.HTTPException as exc:
                log.warning("Could not edit expired LEAPS message: %s", exc)
