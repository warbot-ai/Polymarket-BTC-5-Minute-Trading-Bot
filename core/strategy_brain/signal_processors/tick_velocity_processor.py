"""
Tick Velocity Signal Processor — 5-Minute Edition

Updated from 15m: uses 30s/15s windows instead of 60s/30s.

WHY SHORTER WINDOWS FOR 5-MIN MARKETS:
  In a 15-minute market, looking back 60 seconds means looking at the first
  6.7% of the market — still very early, lots of noise.

  In a 5-minute market at the 3:30 trade window:
    - 60 seconds ago = the 2:30 mark = the first 50% of the market
    - That early price is near 0.50 and not yet informative
    - 30s ago = 3:00 mark = price is settling, more signal
    - 15s ago = 3:15 mark = price is nearly where we're trading

  Using 30s/15s gives us velocity of the SETTLING phase of the market,
  which is far more predictive than the opening noise phase.

THRESHOLDS:
  velocity_threshold_30s: 0.008 (0.8% in 30s) — was 0.010 for 15m
    Lower because 5m probabilities move faster; 0.8% is already significant
  velocity_threshold_15s: 0.005 (0.5% in 15s) — new, replaces 60s as secondary
    Very short-term momentum just before the trade window
"""
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Optional, Dict, Any, List
from loguru import logger

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from core.strategy_brain.signal_processors.base_processor import (
    BaseSignalProcessor, TradingSignal, SignalType, SignalDirection, SignalStrength,
)


class TickVelocityProcessor(BaseSignalProcessor):
    """
    Measures Polymarket probability velocity — 5-minute edition.

    Uses 30s/15s windows (vs 60s/30s for 15m) because at the 3:30 trade
    window, 60s ago is still in the noisy market-open phase.
    """

    def __init__(
        self,
        velocity_threshold_60s: float = 0.012,   # kept for API compatibility
        velocity_threshold_30s: float = 0.008,   # primary threshold (was 0.010)
        velocity_threshold_15s: float = 0.005,   # new secondary threshold for 5m
        min_ticks: int = 5,
        min_confidence: float = 0.55,
    ):
        super().__init__("TickVelocity")

        self.velocity_threshold_60s = velocity_threshold_60s
        self.velocity_threshold_30s = velocity_threshold_30s
        self.velocity_threshold_15s = velocity_threshold_15s
        self.min_ticks = min_ticks
        self.min_confidence = min_confidence

        logger.info(
            f"Initialized Tick Velocity Processor (5m edition): "
            f"30s_threshold={velocity_threshold_30s:.1%}, "
            f"15s_threshold={velocity_threshold_15s:.1%}"
        )

    def _get_price_at(self, tick_buffer: List[Dict], seconds_ago: float, now: datetime) -> Optional[float]:
        """Find the tick price closest to `seconds_ago` seconds before now."""
        target = now - timedelta(seconds=seconds_ago)
        best, best_diff = None, float('inf')

        for tick in tick_buffer:
            ts = tick['ts']
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            diff = abs((ts - target).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best = float(tick['price'])

        # For 5m markets, allow ±10s tolerance (was ±15s for 15m)
        return best if best_diff <= 10 else None

    def process(
        self,
        current_price: Decimal,
        historical_prices: list,
        metadata: Dict[str, Any] = None,
    ) -> Optional[TradingSignal]:
        if not self.is_enabled or not metadata:
            return None

        tick_buffer = metadata.get("tick_buffer")
        if not tick_buffer or len(tick_buffer) < self.min_ticks:
            return None

        now = datetime.now(timezone.utc)
        curr = float(current_price)

        # Use 30s and 15s windows for 5-minute markets
        price_30s = self._get_price_at(tick_buffer, 30, now)
        price_15s = self._get_price_at(tick_buffer, 15, now)
        price_60s = self._get_price_at(tick_buffer, 60, now)  # fallback only

        if price_30s is None and price_15s is None:
            logger.debug("TickVelocity: no ticks in 15-30s window")
            return None

        # Compute velocities
        vel_30s = ((curr - price_30s) / price_30s) if price_30s else None
        vel_15s = ((curr - price_15s) / price_15s) if price_15s else None
        vel_60s = ((curr - price_60s) / price_60s) if price_60s else None

        # Acceleration: is the move speeding up in the last 15s vs 15-30s?
        acceleration = 0.0
        if vel_30s is not None and vel_15s is not None:
            vel_first_15s = vel_30s - vel_15s   # approximate first-half velocity
            acceleration = vel_15s - vel_first_15s

        logger.info(
            f"TickVelocity (5m): curr={curr:.4f}, "
            f"vel_30s={vel_30s*100:+.3f}% " if vel_30s else "vel_30s=N/A "
            f"vel_15s={vel_15s*100:+.3f}% " if vel_15s else "vel_15s=N/A "
            f"accel={acceleration*100:+.4f}%"
        )

        # Primary signal: use 15s velocity if available, else 30s, else 60s
        if vel_15s is not None and abs(vel_15s) >= self.velocity_threshold_15s:
            primary_vel = vel_15s
            threshold = self.velocity_threshold_15s
        elif vel_30s is not None and abs(vel_30s) >= self.velocity_threshold_30s:
            primary_vel = vel_30s
            threshold = self.velocity_threshold_30s
        elif vel_60s is not None and abs(vel_60s) >= self.velocity_threshold_60s:
            primary_vel = vel_60s
            threshold = self.velocity_threshold_60s
        else:
            logger.debug("TickVelocity: below all thresholds — no signal")
            return None

        direction = SignalDirection.BULLISH if primary_vel > 0 else SignalDirection.BEARISH
        abs_vel = abs(primary_vel)

        if abs_vel >= 0.04:
            strength = SignalStrength.VERY_STRONG
        elif abs_vel >= 0.025:
            strength = SignalStrength.STRONG
        elif abs_vel >= 0.012:
            strength = SignalStrength.MODERATE
        else:
            strength = SignalStrength.WEAK

        confidence = min(0.82, 0.55 + (abs_vel / threshold - 1) * 0.12)

        # Acceleration bonus
        accel_same_dir = (acceleration > 0 and primary_vel > 0) or (acceleration < 0 and primary_vel < 0)
        if accel_same_dir and abs(acceleration) > 0.003:
            confidence = min(0.88, confidence + 0.06)
            logger.info(f"TickVelocity: acceleration bonus ({acceleration*100:+.4f}%)")

        # Conflict penalty: if 30s and 15s disagree, reduce confidence
        if vel_30s is not None and vel_15s is not None and (vel_30s > 0) != (vel_15s > 0):
            confidence *= 0.80
            logger.info("TickVelocity: velocity reversal — confidence reduced")

        if confidence < self.min_confidence:
            return None

        signal = TradingSignal(
            timestamp=datetime.now(),
            source=self.name,
            signal_type=SignalType.MOMENTUM,
            direction=direction,
            strength=strength,
            confidence=confidence,
            current_price=current_price,
            metadata={
                "velocity_30s": round(vel_30s, 6) if vel_30s else None,
                "velocity_15s": round(vel_15s, 6) if vel_15s else None,
                "velocity_60s": round(vel_60s, 6) if vel_60s else None,
                "acceleration": round(acceleration, 6),
                "primary_window": "15s" if vel_15s is not None and abs(vel_15s) >= self.velocity_threshold_15s
                                  else "30s" if vel_30s is not None else "60s",
                "ticks_in_buffer": len(tick_buffer),
            }
        )

        self._record_signal(signal)
        logger.info(
            f"Generated {direction.value.upper()} signal (TickVelocity/5m): "
            f"vel={primary_vel*100:+.3f}%, accel={acceleration*100:+.4f}%, "
            f"conf={confidence:.2%}, score={signal.score:.1f}"
        )
        return signal
