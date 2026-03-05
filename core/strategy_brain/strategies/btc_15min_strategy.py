"""
5-Minute BTC Trading Strategy
Replaces the 15-minute strategy with a shorter-horizon variant.

Key changes from 15m:
- Decision loop interval: 5 min (was 15 min)
- min_score for fusion raised to 65 (was 60) — noisier market needs stronger signal
- Spike detector lookback: 15 periods (was 20)
- Extreme sentiment thresholds: 20/80 (was 25/75) — only very extreme readings
"""
import asyncio
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from collections import deque
from loguru import logger
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from core.strategy_brain.signal_processors.spike_detector import SpikeDetectionProcessor
from core.strategy_brain.signal_processors.sentiment_processor import SentimentProcessor
from core.strategy_brain.signal_processors.divergence_processor import PriceDivergenceProcessor
from core.strategy_brain.fusion_engine.signal_fusion import get_fusion_engine, FusedSignal
from core.strategy_brain.signal_processors.base_processor import SignalDirection


class BTCStrategy5Min:
    """
    5-minute BTC trading strategy.

    Tighter thresholds, shorter lookbacks, and down-weighted
    slow-updating signals (sentiment, Deribit PCR) vs the 15m version.
    """

    def __init__(
        self,
        max_position_size: Decimal = Decimal("10.0"),
        stop_loss_pct: float = 0.25,    # tighter SL for faster markets
        take_profit_pct: float = 0.15,  # tighter TP for faster markets
        max_positions: int = 2,
    ):
        self.max_position_size = max_position_size
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.max_positions = max_positions

        # Tighter settings for 5m
        self.spike_detector = SpikeDetectionProcessor(
            spike_threshold=0.04,
            lookback_periods=15,
            velocity_threshold=0.04,
        )

        self.sentiment_processor = SentimentProcessor(
            extreme_fear_threshold=20,
            extreme_greed_threshold=80,
        )

        self.divergence_processor = PriceDivergenceProcessor(
            divergence_threshold=0.05,
            momentum_threshold=0.002,
            extreme_prob_threshold=0.72,
            low_prob_threshold=0.28,
        )

        self.fusion_engine = get_fusion_engine()
        self.price_history = deque(maxlen=60)

        self._current_price: Optional[Decimal] = None
        self._spot_price_consensus: Optional[Decimal] = None
        self._sentiment_score: Optional[float] = None

        self.open_positions: List[Dict[str, Any]] = []
        self._is_running = False
        self._last_decision_time: Optional[datetime] = None
        self._signals_processed = 0
        self._trades_executed = 0
        self._total_pnl = Decimal("0")

        logger.info(
            f"Initialized 5-Min BTC Strategy: "
            f"max_position=${max_position_size}, "
            f"SL={stop_loss_pct:.0%}, TP={take_profit_pct:.0%}"
        )

    async def start(self) -> None:
        if self._is_running:
            return
        self._is_running = True
        asyncio.create_task(self._decision_loop())

    async def stop(self) -> None:
        self._is_running = False

    def update_market_data(self, price: Decimal, spot_consensus=None, sentiment=None):
        self._current_price = price
        self.price_history.append(price)
        if spot_consensus:
            self._spot_price_consensus = spot_consensus
        if sentiment is not None:
            self._sentiment_score = sentiment

    async def _decision_loop(self) -> None:
        while self._is_running:
            try:
                await self._wait_for_next_interval()
                await self._make_decision()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in decision loop: {e}")
                await asyncio.sleep(30)

    async def _wait_for_next_interval(self) -> None:
        """Wait until next 5-minute mark."""
        now = datetime.now()
        minutes_past = now.minute % 5
        wait_minutes = 5 if minutes_past == 0 else 5 - minutes_past
        next_time = (now + timedelta(minutes=wait_minutes)).replace(second=0, microsecond=0)
        wait_seconds = (next_time - now).total_seconds()
        logger.info(f"Waiting {wait_seconds:.0f}s until next 5-min decision ({next_time.strftime('%H:%M')})")
        await asyncio.sleep(wait_seconds)

    async def _make_decision(self) -> None:
        if not self._current_price:
            return

        signals = self._process_signals()
        if not signals:
            return

        # Raised min_score to 65 (was 60) — noisier 5m market needs stronger consensus
        fused = self.fusion_engine.fuse_signals(signals, min_signals=1, min_score=65.0)
        if not fused or not fused.is_actionable:
            return

        if len(self.open_positions) >= self.max_positions:
            logger.warning(f"Max positions ({self.max_positions}) reached")
            return

        await self._execute_trade(fused)
        self._last_decision_time = datetime.now()

    def _process_signals(self) -> List:
        signals = []
        if len(self.price_history) < 15:   # reduced from 20
            return signals

        metadata = {}
        if self._spot_price_consensus:
            metadata['spot_price'] = float(self._spot_price_consensus)
        if self._sentiment_score is not None:
            metadata['sentiment_score'] = self._sentiment_score

        for processor in [self.spike_detector, self.sentiment_processor]:
            sig = processor.process(self._current_price, list(self.price_history), metadata)
            if sig:
                signals.append(sig)

        if self._spot_price_consensus:
            sig = self.divergence_processor.process(self._current_price, list(self.price_history), metadata)
            if sig:
                signals.append(sig)

        self._signals_processed += len(signals)
        return signals

    async def _execute_trade(self, signal: FusedSignal) -> None:
        position_size = min(self.max_position_size, Decimal("10.0"))
        entry_price = self._current_price

        if signal.direction == SignalDirection.BULLISH:
            stop_loss = entry_price * Decimal(str(1 - self.stop_loss_pct))
            take_profit = entry_price * Decimal(str(1 + self.take_profit_pct))
        else:
            stop_loss = entry_price * Decimal(str(1 + self.stop_loss_pct))
            take_profit = entry_price * Decimal(str(1 - self.take_profit_pct))

        position = {
            "id": f"pos_{datetime.now().timestamp()}",
            "direction": signal.direction.value,
            "entry_price": entry_price,
            "size": position_size,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "entry_time": datetime.now(),
            "signal_score": signal.score,
            "status": "open",
        }
        self.open_positions.append(position)
        self._trades_executed += 1
        logger.info(f"Position opened: {signal.direction.value} entry=${float(entry_price):,.4f}")

    def get_statistics(self) -> Dict[str, Any]:
        return {
            "is_running": self._is_running,
            "signals_processed": self._signals_processed,
            "trades_executed": self._trades_executed,
            "open_positions": len(self.open_positions),
            "total_pnl": float(self._total_pnl),
            "last_decision": self._last_decision_time.isoformat() if self._last_decision_time else None,
        }


_strategy_instance = None

def get_btc_strategy() -> BTCStrategy5Min:
    global _strategy_instance
    if _strategy_instance is None:
        _strategy_instance = BTCStrategy5Min()
    return _strategy_instance
