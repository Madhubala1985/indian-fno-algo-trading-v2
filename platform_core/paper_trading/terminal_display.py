"""
Terminal observability utilities — paper trading and backtest pipeline.

Structured, colored output blocks for trade entries, exits, position
snapshots, session heartbeats, performance summaries, and operator warnings.

Color detection: enabled when stdout is a TTY and NO_COLOR is unset.
Set FORCE_COLOR=1 to force color in pipes/redirects.
Gracefully degrades to plain ASCII on CI, file redirects, legacy consoles.

Tested compatible with: WSL, Windows Terminal, VS Code Terminal, tmux, SSH.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date
from typing import Callable

# ── ANSI detection ─────────────────────────────────────────────────────────────

def _detect_ansi() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


_ANSI: bool = _detect_ansi()
_W: int = 76  # total visible block width including borders


def _strip(s: str) -> str:
    """Strip ANSI escape codes to get printable character count."""
    return re.sub(r"\033\[[0-9;]*m", "", s)


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ANSI else text


# ── Color helpers ──────────────────────────────────────────────────────────────

_CF = Callable[[str], str]

def _green(t: str)   -> str: return _c("1;92", t)   # bold bright green
def _red(t: str)     -> str: return _c("1;91", t)   # bold bright red
def _yellow(t: str)  -> str: return _c("1;93", t)   # bold bright yellow
def _cyan(t: str)    -> str: return _c("1;96", t)   # bold bright cyan
def _blue(t: str)    -> str: return _c("1;94", t)   # bold bright blue
def _magenta(t: str) -> str: return _c("1;95", t)   # bold bright magenta
def _white(t: str)   -> str: return _c("1;97", t)   # bold bright white
def _dim(t: str)     -> str: return _c("2",    t)   # dim
def _bold(t: str)    -> str: return _c("1",    t)   # bold


# ── Box-drawing primitives ─────────────────────────────────────────────────────
#
# All helpers target _W = 76 visible columns.  ANSI escape sequences are
# stripped when calculating padding, so colored inner content stays aligned.

def _box_top(cf: _CF) -> str:
    return cf("╔" + "═" * (_W - 2) + "╗")

def _box_div(cf: _CF) -> str:
    return cf("╟" + "─" * (_W - 2) + "╢")

def _box_bot(cf: _CF) -> str:
    return cf("╚" + "═" * (_W - 2) + "╝")

def _box_row(content: str, cf: _CF) -> str:
    vis = len(_strip(content))
    max_inner = _W - 4   # ║ + "  " + content + " " + ║
    if vis > max_inner:
        # Truncate ANSI-aware: walk the string, count visible chars, cut
        cut_pos = 0
        seen = 0
        i = 0
        target = max_inner - 1   # reserve 1 for "…"
        while i < len(content) and seen < target:
            if _ANSI and content[i] == "\033":
                end = content.find("m", i)
                i = (end + 1) if end != -1 else len(content)
            else:
                seen += 1
                i += 1
                cut_pos = i
        content = content[:cut_pos] + "…"
        vis = len(_strip(content))
    pad = max(0, max_inner - vis)
    return cf("║") + "  " + content + " " * pad + " " + cf("║")

def _thin_top(label: str, cf: _CF) -> str:
    prefix = f"── {label} " if label else "── "
    dashes = "─" * max(0, _W - 2 - len(prefix))
    return cf("┌" + prefix + dashes + "┐")

def _thin_bot(cf: _CF) -> str:
    return cf("└" + "─" * (_W - 2) + "┘")

def _thin_row(content: str, cf: _CF) -> str:
    vis = len(_strip(content))
    max_inner = _W - 4
    if vis > max_inner:
        cut_pos = 0
        seen = 0
        i = 0
        target = max_inner - 1
        while i < len(content) and seen < target:
            if _ANSI and content[i] == "\033":
                end = content.find("m", i)
                i = (end + 1) if end != -1 else len(content)
            else:
                seen += 1
                i += 1
                cut_pos = i
        content = content[:cut_pos] + "…"
        vis = len(_strip(content))
    pad = max(0, max_inner - vis)
    return cf("│") + "  " + content + " " * pad + " " + cf("│")


# ── Entry block ────────────────────────────────────────────────────────────────

def print_entry_block(
    *,
    direction: str,
    underlying: str,
    strike: float,
    option_type: str,
    expiry: date,
    lots: int,
    lot_size: int,
    premium: float,
    notional: float,
    bar_index: int,
    session: str = "",
    conf_raw: float = 0.0,
    conf_eff: float = 0.0,
    fvg_quality: float = 1.0,
    setup_score: float = 0.0,
    vol_ratio: float = 1.0,
    liquidity: float = 0.0,
    drawdown: float = 0.0,
    pdh: float | None = None,
    pdl: float | None = None,
    or_low: float | None = None,
    or_high: float | None = None,
    extra_label: str = "",
) -> None:
    """Print a visually prominent entry block (green borders)."""
    cf = _green
    dir_label = "▶ LONG" if direction.upper() == "LONG" else "▼ SHORT"
    contract = f"{underlying} {strike:.0f} {option_type}"
    extra = f"  │  {extra_label}" if extra_label else ""

    line1 = f"{dir_label}  │  {contract}  │  expiry={expiry}  │  bar={bar_index}{extra}"
    line2 = (
        f"lots={lots}  lot_sz={lot_size}"
        f"  │  prem=₹{premium:.2f}"
        f"  │  notional=₹{notional:,.0f}"
    )

    detail: list[str] = []
    if session:
        detail.append(f"session={session}")
    detail.append(f"conf={conf_raw:.2f}→{conf_eff:.2f}")
    detail.append(f"fvg_q={fvg_quality:.2f}")
    detail.append(f"setup={setup_score:.2f}")
    detail.append(f"vol={vol_ratio:.2f}")
    detail.append(f"liq={liquidity:.2f}")
    detail.append(f"dd={drawdown:.1%}")
    if pdh is not None and pdl is not None:
        detail.append(f"PDH={pdh:.0f}/PDL={pdl:.0f}")
    if or_low is not None and or_high is not None:
        detail.append(f"OR={or_low:.0f}–{or_high:.0f}")
    line3 = "  ".join(detail)

    print()
    print(_box_top(cf))
    print(_box_row(_bold(line1), cf))
    print(_box_div(cf))
    print(_box_row(line2, cf))
    print(_box_row(line3, cf))
    print(_box_bot(cf))


# ── Exit block ─────────────────────────────────────────────────────────────────

def print_exit_block(
    *,
    underlying: str,
    strike: float,
    option_type: str,
    direction: str,
    lots: int,
    lot_size: int,
    entry_premium: float,
    exit_premium: float,
    pnl: float,
    bars_held: int,
    exit_reason: str,
    equity: float | None = None,
) -> None:
    """Print a visually prominent exit block (green on profit, red on loss)."""
    cf = _green if pnl >= 0 else _red
    pnl_fmt = f"+₹{pnl:,.2f}" if pnl >= 0 else f"₹{pnl:,.2f}"
    pct = (exit_premium / max(entry_premium, 0.001) - 1) * 100

    contract = f"{underlying} {strike:.0f} {option_type}"
    line1 = f"◀ EXIT  {direction}  │  {contract}  │  {exit_reason}"
    pnl_compact = f"+₹{pnl:,.0f}" if pnl >= 0 else f"₹{pnl:,.0f}"
    line2 = (
        f"lots={lots}"
        f"  entry=₹{entry_premium:.2f}"
        f"  exit=₹{exit_premium:.2f} ({pct:+.0f}%)"
        f"  pnl={pnl_compact}"
        f"  held={bars_held}bars"
    )
    line3 = f"equity=₹{equity:,.0f}" if equity is not None else ""

    print()
    print(_box_top(cf))
    print(_box_row(_bold(line1), cf))
    print(_box_div(cf))
    print(_box_row(line2, cf))
    if line3:
        print(_box_row(line3, cf))
    print(_box_bot(cf))


# ── PaperBroker open / close (legacy signal-based path) ──────────────────────

def print_broker_open(
    *,
    direction: str,
    symbol: str,
    price: float,
    quantity: int,
) -> None:
    """Compact entry print for the legacy PaperBroker path."""
    cf = _green
    dir_label = "▶ LONG" if direction.upper() == "LONG" else "▼ SHORT"
    line = f"{dir_label}  │  {symbol}  │  @{price:.2f}  │  qty={quantity}"
    print()
    print(_thin_top("PAPER OPEN", cf))
    print(_thin_row(line, cf))
    print(_thin_bot(cf))


def print_broker_close(
    *,
    symbol: str,
    pnl: float,
    entry_price: float,
    exit_price: float,
) -> None:
    """Compact exit print for the legacy PaperBroker path."""
    cf = _green if pnl >= 0 else _red
    pnl_fmt = f"+₹{pnl:,.2f}" if pnl >= 0 else f"₹{pnl:,.2f}"
    line = (
        f"{symbol}  │  entry=₹{entry_price:.2f}"
        f"  │  exit=₹{exit_price:.2f}  │  pnl={pnl_fmt}"
    )
    print()
    print(_thin_top("PAPER CLOSE", cf))
    print(_thin_row(line, cf))
    print(_thin_bot(cf))


# ── Active position snapshot ───────────────────────────────────────────────────

def print_position_snapshot(
    *,
    underlying: str,
    strike: float,
    option_type: str,
    direction: str,
    lots: int,
    entry_premium: float,
    bars_held: int,
    equity: float,
    drawdown: float,
) -> None:
    """Periodic snapshot of the currently open option position."""
    cf = _cyan
    contract = f"{underlying} {strike:.0f} {option_type}"
    line1 = f"⊙ OPEN POSITION  │  {direction}  │  {contract}"
    line2 = (
        f"entry=₹{entry_premium:.2f}"
        f"  lots={lots}"
        f"  held={bars_held}bars"
        f"  equity=₹{equity:,.0f}"
        f"  dd={drawdown:.1%}"
    )
    print()
    print(_box_top(cf))
    print(_box_row(_bold(line1), cf))
    print(_box_div(cf))
    print(_box_row(line2, cf))
    print(_box_bot(cf))


# ── Session heartbeat ──────────────────────────────────────────────────────────

def print_heartbeat(
    *,
    bar_index: int,
    equity: float,
    drawdown: float,
    trades: int,
    wins: int,
    bias: str = "",
    confidence: float = 0.0,
    session: str = "",
    has_position: bool = False,
) -> None:
    """Dim periodic status line — one line, minimal visual weight."""
    win_rate = f"{wins / trades:.0%}" if trades > 0 else "—"
    parts: list[str] = [
        f"bar={bar_index}",
        f"equity=₹{equity:,.0f}",
        f"dd={drawdown:.1%}",
        f"trades={trades}",
        f"win={win_rate}",
    ]
    if bias:
        parts.append(f"bias={bias}")
    if confidence:
        parts.append(f"conf={confidence:.2f}")
    if session:
        parts.append(f"session={session}")
    if has_position:
        parts.append("POSITION=OPEN")
    text = "  ".join(parts)
    visible = len(_strip(text))
    pad = max(0, _W - visible - 6)
    half = pad // 2
    line = f"  {'─' * half}  {text}  {'─' * (pad - half)}"
    print(_dim(line))


# ── Warning / filter block ─────────────────────────────────────────────────────

def print_warning_block(
    tag: str,
    *,
    bar_index: int | None = None,
    reason: str = "",
    **details: object,
) -> None:
    """Yellow thin-bordered block for blocks, rejections, halts, and warnings."""
    cf = _yellow
    bar_str = f"bar={bar_index}  " if bar_index is not None else ""
    detail_str = "  ".join(
        f"{k}={v}" for k, v in details.items() if v is not None and v != ""
    )
    line1 = (bar_str + detail_str).strip()

    print()
    print(_thin_top(tag.upper(), cf))
    if line1:
        print(_thin_row(line1, cf))
    if reason:
        print(_thin_row(reason, cf))
    print(_thin_bot(cf))


# ── Performance summary ────────────────────────────────────────────────────────

def print_performance_summary(
    *,
    total_trades: int,
    wins: int,
    losses: int,
    total_pnl: float,
    avg_pnl: float,
    equity: float,
    initial_equity: float,
    label: str = "PERFORMANCE SUMMARY",
) -> None:
    """Magenta-bordered performance summary block."""
    cf = _magenta
    win_rate = wins / total_trades if total_trades > 0 else 0.0
    # Session return is PnL over starting capital.  (equity/initial - 1) is
    # wrong whenever equity was restored from a state file created with a
    # different capital basis than initial_equity.
    ret = total_pnl / max(initial_equity, 1.0)
    pnl_str = f"+₹{total_pnl:,.2f}" if total_pnl >= 0 else f"₹{total_pnl:,.2f}"
    avg_str = f"+₹{avg_pnl:,.2f}" if avg_pnl >= 0 else f"₹{avg_pnl:,.2f}"

    line1 = (
        f"Trades: {total_trades}"
        f"  │  Wins: {wins}"
        f"  │  Losses: {losses}"
        f"  │  Win Rate: {win_rate:.1%}"
    )
    line2 = f"Total PnL: {pnl_str}  │  Avg PnL: {avg_str}"
    line3 = f"Equity: ₹{equity:,.0f}  │  Return: {ret:+.2%}"

    print()
    print(_box_top(cf))
    print(_box_row(_bold(label), cf))
    print(_box_div(cf))
    print(_box_row(line1, cf))
    print(_box_row(line2, cf))
    print(_box_row(line3, cf))
    print(_box_bot(cf))


# ── Backtest header ────────────────────────────────────────────────────────────

def print_backtest_header(
    *,
    symbol: str,
    bars: int,
    csv_path: str,
    initial_equity: float,
    risk_pct: float,
    max_lots: int,
) -> None:
    """Blue-bordered header printed at the start of each backtest run."""
    cf = _blue
    line1 = f"BACKTEST  │  {symbol}  │  {bars:,} bars  │  {csv_path}"
    line2 = (
        f"Capital: ₹{initial_equity:,.0f}"
        f"  │  risk/trade={risk_pct:.1%}"
        f"  │  max_lots={max_lots}"
    )
    print()
    print(_box_top(cf))
    print(_box_row(_bold(line1), cf))
    print(_box_div(cf))
    print(_box_row(line2, cf))
    print(_box_bot(cf))


# ── Backtest results ───────────────────────────────────────────────────────────

def print_backtest_results(
    *,
    bars: int,
    daily_bars_fed: int,
    trade_signals: int,
    entries_made: int,
    session_blocks: int,
    pdl_blocks: int,
    or_blocks: int,
    fvg_blocks: int,
    fvg_filters: int,
    setup_blocks: int,
    total_trades: int,
    wins: int,
    losses: int,
    win_rate: float,
    total_pnl: float,
    avg_pnl: float,
    equity: float,
    initial_equity: float,
    fvg_block_threshold: float = 0.0,
) -> None:
    """Blue-bordered results block printed at the end of each backtest run."""
    cf = _blue
    ret = (equity / max(initial_equity, 1.0)) - 1.0
    pnl_str = f"+₹{total_pnl:,.2f}" if total_pnl >= 0 else f"₹{total_pnl:,.2f}"
    avg_str = f"+₹{avg_pnl:,.2f}" if avg_pnl >= 0 else f"₹{avg_pnl:,.2f}"

    _LW = 32  # label column width

    def _row(label: str, value: str) -> None:
        print(_box_row(f"{label:<{_LW}}{value}", cf))

    print()
    print(_box_top(cf))
    print(_box_row(_bold("BACKTEST RESULTS"), cf))
    print(_box_div(cf))
    _row("Bars processed:",        f"{bars:,}")
    _row("Daily bars fed (MCB):",  str(daily_bars_fed))
    _row("Directional signals:",   str(trade_signals))
    _row("Entries made:",          str(entries_made))
    _row("  Session blocks:",      str(session_blocks))
    _row("  PDH/PDL blocks:",      str(pdl_blocks))
    _row("  OR blocks:",           str(or_blocks))
    _row("  FVG quality blocks:",
         f"{fvg_blocks}  (threshold < {fvg_block_threshold:.2f})")
    _row("  FVG quality filters:", f"{fvg_filters}  (reduced confidence)")
    _row("  Setup blocks:",        str(setup_blocks))
    print(_box_div(cf))
    _row("Completed trades:",      str(total_trades))
    _row("Winning / losing:",      f"{wins} / {losses}")
    _row("Win rate:",              f"{win_rate:.1%}")
    _row("Total P&L:",             pnl_str)
    _row("Average P&L per trade:", avg_str)
    _row("Starting equity:",       f"₹{initial_equity:,.0f}")
    _row("Ending equity:",         f"₹{equity:,.0f}")
    _row("Equity return:",         f"{ret:.2%}")
    print(_box_bot(cf))


# ── Batch backtest header / footer ─────────────────────────────────────────────

def print_batch_header(count: int) -> None:
    """Blue-bordered header for a batch backtest run."""
    cf = _blue
    line = f"BATCH BACKTEST  │  {count} file(s) queued"
    print()
    print(_box_top(cf))
    print(_box_row(_bold(line), cf))
    print(_box_bot(cf))


def print_batch_file(name: str, index: int, total: int) -> None:
    """Thin blue separator for each file in a batch run."""
    cf = _blue
    line = f"FILE {index}/{total}  │  {name}"
    print()
    print(_thin_top(line, cf))
    print(_thin_bot(cf))


def print_batch_footer() -> None:
    """Blue-bordered footer for a completed batch run."""
    cf = _blue
    line = "BATCH BACKTEST COMPLETED"
    print()
    print(_box_top(cf))
    print(_box_row(_bold(line), cf))
    print(_box_bot(cf))


# ── Trade logger messages ──────────────────────────────────────────────────────

def print_trade_logged(
    *,
    symbol: str,
    direction: str,
    entry: float,
    exit_price: float,
    pnl: float,
) -> None:
    """Dim confirmation that a trade was written to the CSV log."""
    pnl_str = f"+₹{pnl:,.2f}" if pnl >= 0 else f"₹{pnl:,.2f}"
    msg = (
        f"[TRADE LOGGED]  {symbol} {direction}"
        f"  entry=₹{entry:.2f}  exit=₹{exit_price:.2f}  pnl={pnl_str}"
    )
    print(_dim(f"\n  {msg}"))


def print_duplicate_suppressed(
    *,
    symbol: str,
    direction: str,
    entry: float,
    exit_price: float,
) -> None:
    """Yellow warning when a duplicate trade write is suppressed."""
    cf = _yellow
    reason = (
        f"{symbol} {direction}  entry=₹{entry:.2f}  exit=₹{exit_price:.2f}"
    )
    print()
    print(_thin_top("DUPLICATE SUPPRESSED", cf))
    print(_thin_row(reason, cf))
    print(_thin_bot(cf))


__all__ = [
    "print_entry_block",
    "print_exit_block",
    "print_broker_open",
    "print_broker_close",
    "print_position_snapshot",
    "print_heartbeat",
    "print_warning_block",
    "print_performance_summary",
    "print_backtest_header",
    "print_backtest_results",
    "print_batch_header",
    "print_batch_file",
    "print_batch_footer",
    "print_trade_logged",
    "print_duplicate_suppressed",
]
