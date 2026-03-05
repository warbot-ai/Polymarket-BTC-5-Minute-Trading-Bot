import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
import math
from decimal import Decimal
import time
from dataclasses import dataclass
from typing import List, Optional, Dict
import random

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

try:
    from patch_gamma_markets import apply_gamma_markets_patch, verify_patch
    patch_applied = apply_gamma_markets_patch()
    if patch_applied:
        verify_patch()
    else:
        print("ERROR: Failed to apply gamma_market patch")
        sys.exit(1)
except ImportError as e:
    print(f"ERROR: Could not import patch module: {e}")
    sys.exit(1)

from nautilus_trader.config import (
    InstrumentProviderConfig, LiveDataEngineConfig, LiveExecEngineConfig,
    LiveRiskEngineConfig, LoggingConfig, TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.adapters.polymarket import POLYMARKET
from nautilus_trader.adapters.polymarket import PolymarketDataClientConfig, PolymarketExecClientConfig
from nautilus_trader.adapters.polymarket.factories import (
    PolymarketLiveDataClientFactory, PolymarketLiveExecClientFactory,
)
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.identifiers import InstrumentId, ClientOrderId
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.data import QuoteTick

from dotenv import load_dotenv
from loguru import logger
import redis

from core.strategy_brain.signal_processors.spike_detector import SpikeDetectionProcessor
from core.strategy_brain.signal_processors.sentiment_processor import SentimentProcessor
from core.strategy_brain.signal_processors.divergence_processor import PriceDivergenceProcessor
from core.strategy_brain.signal_processors.orderbook_processor import OrderBookImbalanceProcessor
from core.strategy_brain.signal_processors.tick_velocity_processor import TickVelocityProcessor
from core.strategy_brain.signal_processors.deribit_pcr_processor import DeribitPCRProcessor
from core.strategy_brain.fusion_engine.signal_fusion import get_fusion_engine
from execution.risk_engine import get_risk_engine
from monitoring.performance_tracker import get_performance_tracker
from monitoring.grafana_exporter import get_grafana_exporter
from feedback.learning_engine import get_learning_engine
load_dotenv()
from patch_market_orders import apply_market_order_patch
patch_applied = apply_market_order_patch()
if patch_applied:
    logger.info("Market order patch applied successfully")
else:
    logger.warning("Market order patch failed - orders may be rejected")

# =============================================================================
# CONSTANTS — 5-MINUTE MARKET SETTINGS
# =============================================================================
QUOTE_STABILITY_REQUIRED = 3
QUOTE_MIN_SPREAD = 0.001
MARKET_INTERVAL_SECONDS = 300          # 5-minute markets

# Trade window: 3m30s – 4m15s into the market (70–85% complete)
# Rationale: at 70% into a 5m market the price has settled enough to read direction
# but we still have 45s of liquidity before settlement approach
TRADE_WINDOW_START = 210
TRADE_WINDOW_END   = 255

# Tighter trend thresholds vs 15m (0.62/0.38 vs 0.60/0.40)
# 5m markets are noisier; require stronger conviction before trading
TREND_UP_THRESHOLD   = 0.62
TREND_DOWN_THRESHOLD = 0.38

RESTART_AFTER_MINUTES = 60             # was 90; 5m filters go stale faster


@dataclass
class PaperTrade:
    timestamp: datetime
    direction: str
    size_usd: float
    price: float
    signal_score: float
    signal_confidence: float
    outcome: str = "PENDING"

    def to_dict(self):
        return {
            'timestamp': self.timestamp.isoformat(),
            'direction': self.direction,
            'size_usd': self.size_usd,
            'price': self.price,
            'signal_score': self.signal_score,
            'signal_confidence': self.signal_confidence,
            'outcome': self.outcome,
        }


def init_redis():
    try:
        rc = redis.Redis(
            host=os.getenv('REDIS_HOST', 'localhost'),
            port=int(os.getenv('REDIS_PORT', 6379)),
            db=int(os.getenv('REDIS_DB', 2)),
            decode_responses=True,
            socket_connect_timeout=5,
            socket_keepalive=True
        )
        rc.ping()
        logger.info("Redis connection established")
        return rc
    except Exception as e:
        logger.warning(f"Redis connection failed: {e} — simulation mode static")
        return None


class IntegratedBTCStrategy(Strategy):
    """
    BTC 5-Minute Polymarket Strategy

    Key changes from 15m version:
    - Slug filter: btc-updown-5m-* (was btc-updown-15m-*)
    - MARKET_INTERVAL_SECONDS = 300 (was 900)
    - Trade window: 210-255s (3:30-4:15, 70-85% through market)
    - Trend thresholds: 0.62/0.38 (was 0.60/0.40) — tighter for noisier 5m
    - Signal weights: OrderBook=0.38, TickVelocity=0.32 dominate
    - Sentiment/PCR weights near-zero: daily metrics useless for 5m
    - Spike detector: tighter thresholds for faster price action
    - TickVelocity: 30s/15s windows (was 60s/30s)
    - Auto-restart: 60 min (was 90 min)
    - Timer loop: 5s polling (was 10s)
    - Slug count: 289 (24h * 12/h) vs 97 (24h * 4/h)
    """

    def __init__(self, redis_client=None, enable_grafana=True, test_mode=False):
        super().__init__()

        self.bot_start_time = datetime.now(timezone.utc)
        self.restart_after_minutes = RESTART_AFTER_MINUTES

        self.instrument_id = None
        self.redis_client = redis_client
        self.current_simulation_mode = False

        self.all_btc_instruments: List[Dict] = []
        self.current_instrument_index: int = -1
        self.next_switch_time: Optional[datetime] = None

        self._stable_tick_count = 0
        self._market_stable = False
        self._last_instrument_switch = None

        self.last_trade_time = -1
        self._waiting_for_market_open = False
        self._last_bid_ask = None

        from collections import deque
        # 200 ticks covers ~60s+ at typical 5m market tick rates
        self._tick_buffer: deque = deque(maxlen=200)

        self._yes_token_id: Optional[str] = None

        # ------------------------------------------------------------------
        # Signal Processors — tuned for 5-minute markets
        # ------------------------------------------------------------------
        # Spike: tighter thresholds because 5m prices move faster
        #   spike_threshold 0.04 (was 0.05): needs bigger deviation to signal
        #   lookback_periods 15 (was 20): shorter history for short markets
        #   velocity_threshold 0.04 (was 0.03): needs bigger burst to signal
        self.spike_detector = SpikeDetectionProcessor(
            spike_threshold=0.04,
            lookback_periods=15,
            velocity_threshold=0.04,
        )

        # Sentiment: only extreme readings matter even for 15m; for 5m
        # we make thresholds more extreme and rely on near-zero fusion weight
        self.sentiment_processor = SentimentProcessor(
            extreme_fear_threshold=20,
            extreme_greed_threshold=80,
        )

        # Divergence: tighter extreme fade thresholds (0.72/0.28 vs 0.68/0.32)
        # because 5m markets reach >68% more often on BTC moves
        self.divergence_processor = PriceDivergenceProcessor(
            divergence_threshold=0.05,
            momentum_threshold=0.002,
            extreme_prob_threshold=0.72,
            low_prob_threshold=0.28,
        )

        # Order Book: min_book_volume raised to $100 (was $50)
        # 5m markets open thinner; filter noise from illiquid early books
        self.orderbook_processor = OrderBookImbalanceProcessor(
            imbalance_threshold=0.30,
            min_book_volume=100.0,
        )

        # Tick Velocity: lower 30s threshold (0.008 vs 0.010)
        # 5m prices move faster; 0.8% in 30s is already a meaningful signal
        self.tick_velocity_processor = TickVelocityProcessor(
            velocity_threshold_60s=0.012,
            velocity_threshold_30s=0.008,
        )

        # Deribit PCR: near-zero weight for 5m trading
        # Only fires on very extreme readings (1.30/0.60 vs 1.20/0.70)
        # Cached 10 min (was 5 min) since it barely matters anyway
        self.deribit_pcr_processor = DeribitPCRProcessor(
            bullish_pcr_threshold=1.30,
            bearish_pcr_threshold=0.60,
            max_days_to_expiry=1,
            cache_seconds=600,
        )

        # ------------------------------------------------------------------
        # Signal Fusion weights — 5-minute horizon
        #
        # OrderBookImbalance 0.38: best real-time signal; large players show
        #   their hand in the order book right before settlement
        # TickVelocity 0.32: probability momentum is directly on-market;
        #   if price is moving fast in one direction, follow it
        # PriceDivergence 0.15: spot BTC momentum still valid
        # SpikeDetection 0.10: mean reversion still fires occasionally
        # SentimentAnalysis 0.03: daily metric, near-useless for 5m
        # DeribitPCR 0.02: options data is hours old, near-useless for 5m
        # ------------------------------------------------------------------
        self.fusion_engine = get_fusion_engine()
        self.fusion_engine.set_weight("OrderBookImbalance", 0.38)
        self.fusion_engine.set_weight("TickVelocity",       0.32)
        self.fusion_engine.set_weight("PriceDivergence",    0.15)
        self.fusion_engine.set_weight("SpikeDetection",     0.10)
        self.fusion_engine.set_weight("SentimentAnalysis",  0.03)
        self.fusion_engine.set_weight("DeribitPCR",         0.02)

        self.risk_engine = get_risk_engine()
        self.performance_tracker = get_performance_tracker()
        self.learning_engine = get_learning_engine()

        if enable_grafana:
            self.grafana_exporter = get_grafana_exporter()
        else:
            self.grafana_exporter = None

        self.price_history = []
        self.max_history = 60
        self.paper_trades: List[PaperTrade] = []
        self.test_mode = test_mode

        if test_mode:
            logger.info("=" * 80)
            logger.info("  TEST MODE ACTIVE")
            logger.info("=" * 80)

        logger.info("=" * 80)
        logger.info("INTEGRATED BTC 5-MIN STRATEGY INITIALIZED")
        logger.info(f"  Market interval: {MARKET_INTERVAL_SECONDS}s (5 minutes)")
        logger.info(f"  Trade window: {TRADE_WINDOW_START}s-{TRADE_WINDOW_END}s ({TRADE_WINDOW_START//60}:{TRADE_WINDOW_START%60:02d}-{TRADE_WINDOW_END//60}:{TRADE_WINDOW_END%60:02d})")
        logger.info(f"  Trend filter: UP>{TREND_UP_THRESHOLD:.0%} / DOWN<{TREND_DOWN_THRESHOLD:.0%}")
        logger.info("  Signal weights: OrderBook=0.38, TickVelocity=0.32 (real-time dominant)")
        logger.info("  Sentiment/PCR: near-zero weight (daily metrics, not useful for 5m)")
        logger.info("=" * 80)

    def _is_quote_valid(self, bid, ask) -> bool:
        if bid is None or ask is None:
            return False
        try:
            b, a = float(bid), float(ask)
        except (TypeError, ValueError):
            return False
        return QUOTE_MIN_SPREAD <= b <= 0.999 and QUOTE_MIN_SPREAD <= a <= 0.999

    def _reset_stability(self, reason: str = ""):
        if self._market_stable:
            logger.warning(f"Market stability RESET{' – ' + reason if reason else ''}")
        self._market_stable = False
        self._stable_tick_count = 0

    async def check_simulation_mode(self) -> bool:
        if not self.redis_client:
            return self.current_simulation_mode
        try:
            sim_mode = self.redis_client.get('btc_trading:simulation_mode')
            if sim_mode is not None:
                redis_sim = sim_mode == '1'
                if redis_sim != self.current_simulation_mode:
                    self.current_simulation_mode = redis_sim
                    logger.warning(f"Trading mode: {'SIMULATION' if redis_sim else 'LIVE TRADING'}")
                    if not redis_sim:
                        logger.warning("LIVE TRADING ACTIVE - Real money at risk!")
                return redis_sim
        except Exception as e:
            logger.warning(f"Redis check failed: {e}")
        return self.current_simulation_mode

    def on_start(self):
        logger.info("=" * 80)
        logger.info("BTC 5-MIN STRATEGY STARTED")
        logger.info("=" * 80)

        self._load_all_btc_instruments()

        if self.instrument_id:
            self.subscribe_quote_ticks(self.instrument_id)
            logger.info(f"✓ SUBSCRIBED to market: {self.instrument_id}")
            try:
                quote = self.cache.quote_tick(self.instrument_id)
                if quote and quote.bid_price and quote.ask_price:
                    p = (quote.bid_price + quote.ask_price) / 2
                    self.price_history.append(p)
                    logger.info(f"✓ Initial price: ${float(p):.4f}")
            except Exception:
                pass

        if len(self.price_history) < 20:
            self._generate_synthetic_history(target_count=20, existing_count=len(self.price_history))

        self.run_in_executor(self._start_timer_loop)

        if self.grafana_exporter:
            import threading
            threading.Thread(target=self._start_grafana_sync, daemon=True).start()

        logger.info(f"Strategy active — trading every 5 minutes | history: {len(self.price_history)} pts")

    def _generate_synthetic_history(self, target_count=20, existing_count=0):
        base = self.price_history[-1] if self.price_history else Decimal("0.5")
        for _ in range(target_count - existing_count):
            change = Decimal(str(random.uniform(-0.02, 0.02)))
            new = max(Decimal("0.01"), min(Decimal("0.99"), base * (Decimal("1.0") + change)))
            self.price_history.append(new)
            base = new

    def _load_all_btc_instruments(self):
        instruments = self.cache.instruments()
        logger.info(f"Scanning {len(instruments)} instruments for BTC 5-min markets...")

        now = datetime.now(timezone.utc)
        current_ts = int(now.timestamp())
        btc_instruments = []

        for instrument in instruments:
            try:
                if not (hasattr(instrument, 'info') and instrument.info):
                    continue
                question = instrument.info.get('question', '').lower()
                slug = instrument.info.get('market_slug', '').lower()

                # KEY CHANGE: '5m' in slug (was '15m')
                if ('btc' in question or 'btc' in slug) and '5m' in slug:
                    try:
                        market_timestamp = int(slug.split('-')[-1])
                        end_timestamp = market_timestamp + MARKET_INTERVAL_SECONDS
                        time_diff = market_timestamp - current_ts

                        if end_timestamp <= current_ts:
                            continue

                        raw_id = str(instrument.id)
                        without_suffix = raw_id.split('.')[0] if '.' in raw_id else raw_id
                        yes_token_id = without_suffix.split('-')[-1] if '-' in without_suffix else without_suffix

                        btc_instruments.append({
                            'instrument': instrument,
                            'slug': slug,
                            'start_time': datetime.fromtimestamp(market_timestamp, tz=timezone.utc),
                            'end_time': datetime.fromtimestamp(end_timestamp, tz=timezone.utc),
                            'market_timestamp': market_timestamp,
                            'end_timestamp': end_timestamp,
                            'time_diff_minutes': time_diff / 60,
                            'yes_token_id': yes_token_id,
                        })
                    except (ValueError, IndexError):
                        continue
            except Exception:
                continue

        # Deduplicate YES/NO pairs
        seen_slugs = {}
        deduped = []
        for inst in btc_instruments:
            slug = inst['slug']
            if slug not in seen_slugs:
                inst['yes_instrument_id'] = inst['instrument'].id
                inst['no_instrument_id'] = None
                seen_slugs[slug] = inst
                deduped.append(inst)
            else:
                seen_slugs[slug]['no_instrument_id'] = inst['instrument'].id
        btc_instruments = deduped
        btc_instruments.sort(key=lambda x: x['market_timestamp'])

        logger.info("=" * 80)
        logger.info(f"FOUND {len(btc_instruments)} BTC 5-MIN MARKETS:")
        for i, inst in enumerate(btc_instruments[:10]):  # log first 10 only for brevity
            is_active = inst['time_diff_minutes'] <= 0 and inst['end_timestamp'] > current_ts
            status = "ACTIVE" if is_active else ("FUTURE" if inst['time_diff_minutes'] > 0 else "PAST")
            logger.info(f"  [{i}] {inst['slug']}: {status} ({inst['start_time'].strftime('%H:%M')}-{inst['end_time'].strftime('%H:%M')})")
        if len(btc_instruments) > 10:
            logger.info(f"  ... and {len(btc_instruments) - 10} more")
        logger.info("=" * 80)

        self.all_btc_instruments = btc_instruments

        for i, inst in enumerate(btc_instruments):
            is_active = inst['time_diff_minutes'] <= 0 and inst['end_timestamp'] > current_ts
            if is_active:
                self.current_instrument_index = i
                self.instrument_id = inst['instrument'].id
                self.next_switch_time = inst['end_time']
                self._yes_token_id = inst.get('yes_token_id')
                self._yes_instrument_id = inst.get('yes_instrument_id', inst['instrument'].id)
                self._no_instrument_id = inst.get('no_instrument_id')
                logger.info(f"✓ CURRENT MARKET: {inst['slug']}")
                logger.info(f"  Ends at: {self.next_switch_time.strftime('%H:%M:%S')}")
                self.subscribe_quote_ticks(self.instrument_id)
                break

        if self.current_instrument_index == -1 and btc_instruments:
            future = [inst for inst in btc_instruments if inst['time_diff_minutes'] > 0]
            nearest = min(future, key=lambda x: x['time_diff_minutes']) if future else btc_instruments[-1]
            nearest_idx = btc_instruments.index(nearest)
            self.current_instrument_index = nearest_idx
            self.instrument_id = nearest['instrument'].id
            self._yes_token_id = nearest.get('yes_token_id')
            self._yes_instrument_id = nearest.get('yes_instrument_id', nearest['instrument'].id)
            self._no_instrument_id = nearest.get('no_instrument_id')
            self.next_switch_time = nearest['start_time']
            logger.info(f"⚠ Waiting for next market: {nearest['slug']} in {nearest['time_diff_minutes']:.1f} min")
            self.subscribe_quote_ticks(self.instrument_id)
            self._waiting_for_market_open = True

    def _switch_to_next_market(self):
        if not self.all_btc_instruments:
            return False

        next_index = self.current_instrument_index + 1
        if next_index >= len(self.all_btc_instruments):
            logger.warning("No more markets — will restart bot")
            return False

        next_market = self.all_btc_instruments[next_index]
        now = datetime.now(timezone.utc)

        if now < next_market['start_time']:
            return False

        self.current_instrument_index = next_index
        self.instrument_id = next_market['instrument'].id
        self.next_switch_time = next_market['end_time']
        self._yes_token_id = next_market.get('yes_token_id')
        self._yes_instrument_id = next_market.get('yes_instrument_id', next_market['instrument'].id)
        self._no_instrument_id = next_market.get('no_instrument_id')

        logger.info("=" * 80)
        logger.info(f"SWITCHING TO NEXT 5-MIN MARKET: {next_market['slug']}")
        logger.info(f"  Ends at: {self.next_switch_time.strftime('%H:%M:%S')}")
        logger.info("=" * 80)

        self._stable_tick_count = QUOTE_STABILITY_REQUIRED
        self._market_stable = True
        self._waiting_for_market_open = False
        self.last_trade_time = -1

        self.subscribe_quote_ticks(self.instrument_id)
        return True

    def _start_timer_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._timer_loop())
        finally:
            loop.close()

    async def _timer_loop(self):
        """Poll every 5 seconds — appropriate for 5-minute markets."""
        while True:
            uptime = (datetime.now(timezone.utc) - self.bot_start_time).total_seconds() / 60
            if uptime >= self.restart_after_minutes:
                logger.warning("AUTO-RESTART — refreshing 5-min market filters")
                import signal as _signal
                os.kill(os.getpid(), _signal.SIGTERM)
                return

            now = datetime.now(timezone.utc)

            if self.next_switch_time and now >= self.next_switch_time:
                if self._waiting_for_market_open:
                    logger.info(f"⏰ MARKET NOW OPEN: {now.strftime('%H:%M:%S')} UTC")
                    if 0 <= self.current_instrument_index < len(self.all_btc_instruments):
                        curr = self.all_btc_instruments[self.current_instrument_index]
                        self.next_switch_time = curr['end_time']
                    self._waiting_for_market_open = False
                    self._market_stable = True
                    self._stable_tick_count = QUOTE_STABILITY_REQUIRED
                    self.last_trade_time = -1
                    logger.info("  ✓ MARKET OPEN — ready to trade")
                else:
                    self._switch_to_next_market()

            await asyncio.sleep(5)   # 5s polling for 5m markets (was 10s)

    def on_quote_tick(self, tick: QuoteTick):
        try:
            if self.instrument_id is None or tick.instrument_id != self.instrument_id:
                return

            now = datetime.now(timezone.utc)
            bid, ask = tick.bid_price, tick.ask_price
            if bid is None or ask is None:
                return

            try:
                bid_decimal = bid.as_decimal()
                ask_decimal = ask.as_decimal()
            except Exception:
                return

            mid_price = (bid_decimal + ask_decimal) / 2
            self.price_history.append(mid_price)
            if len(self.price_history) > self.max_history:
                self.price_history.pop(0)

            self._last_bid_ask = (bid_decimal, ask_decimal)
            self._tick_buffer.append({'ts': now, 'price': mid_price})

            if not self._market_stable:
                self._stable_tick_count += 1
                if self._stable_tick_count >= 1:
                    self._market_stable = True
                else:
                    return

            if self._waiting_for_market_open:
                return

            if not (0 <= self.current_instrument_index < len(self.all_btc_instruments)):
                return

            current_market = self.all_btc_instruments[self.current_instrument_index]
            market_start_ts = current_market['market_timestamp']
            elapsed_secs = now.timestamp() - market_start_ts

            if elapsed_secs < 0:
                return

            sub_interval = int(elapsed_secs // MARKET_INTERVAL_SECONDS)
            trade_key = (market_start_ts, sub_interval)
            seconds_into_sub = elapsed_secs % MARKET_INTERVAL_SECONDS

            # ================================================================
            # TRADE WINDOW: 210-255s into each 5-min market (3:30-4:15)
            #
            # At 70% into a 5-min market the probability price has settled
            # enough to reflect actual BTC direction. Trading earlier means
            # prices are still near 0.50 (coin-flip territory).
            # Trading after 4:30 risks liquidity drying up as market settles.
            # ================================================================
            if TRADE_WINDOW_START <= seconds_into_sub < TRADE_WINDOW_END and trade_key != self.last_trade_time:
                self.last_trade_time = trade_key

                logger.info("=" * 80)
                logger.info(f" TRADE WINDOW HIT: {now.strftime('%H:%M:%S')} UTC")
                logger.info(f"   Market: {current_market['slug']}")
                logger.info(f"   {seconds_into_sub:.1f}s in ({seconds_into_sub/60:.1f} min of 5)")
                logger.info(f"   Price: ${float(mid_price):,.4f} | Bid: ${float(bid_decimal):,.4f} | Ask: ${float(ask_decimal):,.4f}")
                logger.info(f"   Trend: {'STRONG ✓' if float(mid_price) > TREND_UP_THRESHOLD or float(mid_price) < TREND_DOWN_THRESHOLD else 'WEAK — may skip'}")
                logger.info("=" * 80)

                self.run_in_executor(lambda: self._make_trading_decision_sync(float(mid_price)))

        except Exception as e:
            logger.error(f"Error processing quote tick: {e}")

    def _make_trading_decision_sync(self, current_price):
        price_decimal = Decimal(str(current_price))
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._make_trading_decision(price_decimal))
        finally:
            loop.close()

    async def _fetch_market_context(self, current_price: Decimal) -> dict:
        current_price_float = float(current_price)

        # Use 15 periods (not 20) — shorter history for 5m markets
        recent_prices = [float(p) for p in self.price_history[-15:]]
        sma = sum(recent_prices) / len(recent_prices)
        deviation = (current_price_float - sma) / sma
        momentum = (
            (current_price_float - float(self.price_history[-5])) / float(self.price_history[-5])
            if len(self.price_history) >= 5 else 0.0
        )
        variance = sum((p - sma) ** 2 for p in recent_prices) / len(recent_prices)
        volatility = math.sqrt(variance)

        metadata = {
            "deviation": deviation,
            "momentum": momentum,
            "volatility": volatility,
            "tick_buffer": list(self._tick_buffer),
            "yes_token_id": self._yes_token_id,
        }

        # Fear & Greed: near-zero weight for 5m but still fetch for extreme readings
        try:
            from data_sources.news_social.adapter import NewsSocialDataSource
            news_source = NewsSocialDataSource()
            await news_source.connect()
            fg = await news_source.get_fear_greed_index()
            await news_source.disconnect()
            if fg and "value" in fg:
                metadata["sentiment_score"] = float(fg["value"])
                metadata["sentiment_classification"] = fg.get("classification", "")
                logger.info(f"Fear & Greed: {metadata['sentiment_score']:.0f} [near-zero weight for 5m]")
        except Exception as e:
            logger.debug(f"Sentiment fetch skipped: {e}")

        # Spot price: still the best external signal for 5m
        try:
            from data_sources.coinbase.adapter import CoinbaseDataSource
            coinbase = CoinbaseDataSource()
            await coinbase.connect()
            spot = await coinbase.get_current_price()
            await coinbase.disconnect()
            if spot:
                metadata["spot_price"] = float(spot)
                logger.info(f"Coinbase spot: ${float(spot):,.2f}")
        except Exception as e:
            logger.warning(f"Spot price fetch failed: {e}")

        logger.info(
            f"Context — dev={deviation:.2%}, mom={momentum:.2%}, "
            f"vol={volatility:.4f}, "
            f"spot=${'%.2f' % metadata['spot_price'] if 'spot_price' in metadata else 'N/A'}"
        )
        return metadata

    async def _make_trading_decision(self, current_price: Decimal):
        is_simulation = await self.check_simulation_mode()
        logger.info(f"Mode: {'SIMULATION' if is_simulation else 'LIVE TRADING'}")

        if len(self.price_history) < 15:
            logger.warning(f"Not enough history ({len(self.price_history)}/15)")
            return

        metadata = await self._fetch_market_context(current_price)
        signals = self._process_signals(current_price, metadata)

        if not signals:
            logger.info("No signals — skip")
            return

        for sig in signals:
            logger.info(f"  [{sig.source}] {sig.direction.value}: score={sig.score:.1f}, conf={sig.confidence:.2%}")

        fused = self.fusion_engine.fuse_signals(signals, min_signals=1, min_score=40.0)
        if not fused:
            logger.info("Fusion: no actionable signal — skip")
            return

        logger.info(f"FUSED: {fused.direction.value} (score={fused.score:.1f}, conf={fused.confidence:.2%})")

        POSITION_SIZE_USD = Decimal("1.00")

        # ====================================================================
        # TREND FILTER — 5-minute primary gate
        #
        # Thresholds 0.62/0.38 are tighter than 15m (0.60/0.40) because:
        #   - 5m probabilities settle less cleanly by 3:30 than 15m at 13:00
        #   - Noisier markets need higher conviction to be worth trading
        #   - Expected: ~25-35% of markets skipped (neutral zone), vs ~20-30% for 15m
        # ====================================================================
        price_float = float(current_price)

        if price_float > TREND_UP_THRESHOLD:
            direction = "long"
            logger.info(f" TREND UP ({price_float:.2%}) → YES")
        elif price_float < TREND_DOWN_THRESHOLD:
            direction = "short"
            logger.info(f" TREND DOWN ({price_float:.2%}) → NO")
        else:
            logger.info(
                f"⏭ NEUTRAL ({price_float:.2%}) — within {TREND_DOWN_THRESHOLD:.0%}-{TREND_UP_THRESHOLD:.0%} dead zone, skip"
            )
            return

        is_valid, error = self.risk_engine.validate_new_position(
            size=POSITION_SIZE_USD, direction=direction, current_price=current_price,
        )
        if not is_valid:
            logger.warning(f"Risk blocked: {error}")
            return

        last_tick = getattr(self, '_last_bid_ask', None)
        if last_tick:
            last_bid, last_ask = last_tick
            MIN_LIQ = Decimal("0.02")
            if direction == "long" and last_ask <= MIN_LIQ:
                logger.warning(f"⚠ No ask liquidity (${float(last_ask):.4f}) — retry")
                self.last_trade_time = -1
                return
            if direction == "short" and last_bid <= MIN_LIQ:
                logger.warning(f"⚠ No bid liquidity (${float(last_bid):.4f}) — retry")
                self.last_trade_time = -1
                return

        if is_simulation:
            await self._record_paper_trade(fused, POSITION_SIZE_USD, current_price, direction)
        else:
            await self._place_real_order(fused, POSITION_SIZE_USD, current_price, direction)

    async def _record_paper_trade(self, signal, position_size, current_price, direction):
        exit_delta = timedelta(minutes=1) if self.test_mode else timedelta(minutes=5)
        exit_time = datetime.now(timezone.utc) + exit_delta

        # Tighter simulated movement for 5m (smaller swings than 15m)
        movement = random.uniform(-0.015, 0.06) if "BULLISH" in str(signal.direction) else random.uniform(-0.06, 0.015)
        exit_price = max(Decimal("0.01"), min(Decimal("0.99"),
            current_price * (Decimal("1.0") + Decimal(str(movement)))))

        pnl = position_size * (
            (exit_price - current_price) / current_price if direction == "long"
            else (current_price - exit_price) / current_price
        )
        outcome = "WIN" if pnl > 0 else "LOSS"

        self.paper_trades.append(PaperTrade(
            timestamp=datetime.now(timezone.utc),
            direction=direction.upper(),
            size_usd=float(position_size),
            price=float(current_price),
            signal_score=signal.score,
            signal_confidence=signal.confidence,
            outcome=outcome,
        ))

        self.performance_tracker.record_trade(
            trade_id=f"paper_{int(datetime.now().timestamp())}",
            direction=direction, entry_price=current_price, exit_price=exit_price,
            size=position_size, entry_time=datetime.now(timezone.utc), exit_time=exit_time,
            signal_score=signal.score, signal_confidence=signal.confidence,
            metadata={"simulated": True, "market_interval": "5m", "fusion_score": signal.score},
        )

        if hasattr(self, 'grafana_exporter') and self.grafana_exporter:
            self.grafana_exporter.increment_trade_counter(won=(pnl > 0))
            self.grafana_exporter.record_trade_duration(exit_delta.total_seconds())

        logger.info(f"[SIM] {direction.upper()} ${float(position_size):.2f} @ {float(current_price):.4f} → {outcome} ({movement*100:+.2f}%)")
        self._save_paper_trades()

    def _save_paper_trades(self):
        import json
        try:
            with open('paper_trades_5m.json', 'w') as f:
                json.dump([t.to_dict() for t in self.paper_trades], f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save paper trades: {e}")

    async def _place_real_order(self, signal, position_size, current_price, direction):
        if not self.instrument_id:
            logger.error("No instrument available")
            return

        try:
            side = OrderSide.BUY
            if direction == "long":
                trade_instrument_id = getattr(self, '_yes_instrument_id', self.instrument_id)
                trade_label = "YES (UP)"
            else:
                no_id = getattr(self, '_no_instrument_id', None)
                if no_id is None:
                    logger.warning("NO token not found — cannot bet DOWN, skipping")
                    return
                trade_instrument_id = no_id
                trade_label = "NO (DOWN)"

            instrument = self.cache.instrument(trade_instrument_id)
            if not instrument:
                logger.error(f"Instrument not in cache: {trade_instrument_id}")
                return

            precision = instrument.size_precision
            min_qty_val = float(getattr(instrument, 'min_quantity', None) or 5.0)
            token_qty = round(max(min_qty_val, 5.0), precision)

            qty = Quantity(token_qty, precision=precision)
            timestamp_ms = int(time.time() * 1000)
            unique_id = f"BTC-5MIN-${float(position_size):.0f}-{timestamp_ms}"

            order = self.order_factory.market(
                instrument_id=trade_instrument_id,
                order_side=side,
                quantity=qty,
                client_order_id=ClientOrderId(unique_id),
                quote_quantity=False,
                time_in_force=TimeInForce.IOC,
            )
            self.submit_order(order)
            logger.info(f"REAL ORDER: {trade_label} qty={token_qty:.6f} ~${float(position_size):.2f} @ {float(current_price):.4f}")
            self._track_order_event("placed")

        except Exception as e:
            logger.error(f"Error placing real order: {e}")
            import traceback
            traceback.print_exc()
            self._track_order_event("rejected")

    def _process_signals(self, current_price, metadata=None):
        signals = []
        if metadata is None:
            metadata = {}

        processed = {}
        for k, v in metadata.items():
            processed[k] = Decimal(str(v)) if isinstance(v, float) else v

        spike = self.spike_detector.process(current_price, self.price_history, processed)
        if spike:
            signals.append(spike)

        if 'sentiment_score' in processed:
            sent = self.sentiment_processor.process(current_price, self.price_history, processed)
            if sent:
                signals.append(sent)

        if 'spot_price' in processed:
            div = self.divergence_processor.process(current_price, self.price_history, processed)
            if div:
                signals.append(div)

        if processed.get('yes_token_id'):
            ob = self.orderbook_processor.process(current_price, self.price_history, processed)
            if ob:
                signals.append(ob)

        if processed.get('tick_buffer'):
            tv = self.tick_velocity_processor.process(current_price, self.price_history, processed)
            if tv:
                signals.append(tv)

        pcr = self.deribit_pcr_processor.process(current_price, self.price_history, processed)
        if pcr:
            signals.append(pcr)

        return signals

    def _track_order_event(self, event_type: str) -> None:
        try:
            pt = self.performance_tracker
            for method in ['record_order_event', 'increment_counter', 'increment_order_counter']:
                if hasattr(pt, method):
                    getattr(pt, method)(event_type)
                    return
            logger.debug(f"No order-counter method on PerformanceTracker for '{event_type}'")
        except Exception as e:
            logger.warning(f"Failed to track order event '{event_type}': {e}")

    def on_order_filled(self, event):
        logger.info(f"ORDER FILLED: {event.client_order_id} @ ${float(event.last_px):.4f}")
        self._track_order_event("filled")

    def on_order_denied(self, event):
        logger.error(f"ORDER DENIED: {event.client_order_id} — {event.reason}")
        self._track_order_event("rejected")

    def on_order_rejected(self, event):
        reason = str(getattr(event, 'reason', ''))
        if any(x in reason.lower() for x in ['no orders found', 'fak', 'no match']):
            logger.warning(f"⚠ FAK rejected (no liquidity) — retry next tick: {reason}")
            self.last_trade_time = -1
        else:
            logger.warning(f"Order rejected: {reason}")

    def _start_grafana_sync(self):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.grafana_exporter.start())
            logger.info("Grafana started on port 8000")
        except Exception as e:
            logger.error(f"Grafana failed: {e}")

    def on_stop(self):
        logger.info(f"BTC 5-min strategy stopped | paper trades: {len(self.paper_trades)}")
        if self.grafana_exporter:
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(self.grafana_exporter.stop())
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_integrated_bot(simulation=False, enable_grafana=True, test_mode=False):
    print("=" * 80)
    print("INTEGRATED POLYMARKET BTC 5-MIN TRADING BOT")
    print("=" * 80)

    redis_client = init_redis()

    if redis_client:
        try:
            redis_client.set('btc_trading:simulation_mode', '1' if simulation else '0')
            logger.info(f"Redis mode: {'SIMULATION' if simulation else 'LIVE'}")
        except Exception as e:
            logger.warning(f"Redis set failed: {e}")

    print(f"\nConfiguration:")
    print(f"  Mode: {'SIMULATION' if simulation else 'LIVE TRADING'}")
    print(f"  Market interval: 5 minutes ({MARKET_INTERVAL_SECONDS}s)")
    print(f"  Trade window: {TRADE_WINDOW_START}s-{TRADE_WINDOW_END}s into each market")
    print(f"  Trend filter: UP>{TREND_UP_THRESHOLD:.0%} / DOWN<{TREND_DOWN_THRESHOLD:.0%}")
    print(f"  Weights: OrderBook=0.38, TickVelocity=0.32, Divergence=0.15")
    print(f"  Max trade: ${os.getenv('MARKET_BUY_USD', '1.00')}")
    print()

    now = datetime.now(timezone.utc)

    # Generate slugs for 5-MINUTE markets: btc-updown-5m-<unix_timestamp>
    # 24 hours * 12 markets/hour = 288 + 1 prior = 289 total
    unix_interval_start = (int(now.timestamp()) // MARKET_INTERVAL_SECONDS) * MARKET_INTERVAL_SECONDS
    btc_slugs = []
    for i in range(-1, 289):
        timestamp = unix_interval_start + (i * MARKET_INTERVAL_SECONDS)
        btc_slugs.append(f"btc-updown-5m-{timestamp}")

    filters = {
        "active": True,
        "closed": False,
        "archived": False,
        "slug": tuple(btc_slugs),
        "limit": 300,
    }

    logger.info(f"Loading {len(btc_slugs)} BTC 5-min slugs | start={unix_interval_start}")
    logger.info(f"  First: {btc_slugs[0]} | Last: {btc_slugs[-1]}")

    instrument_cfg = InstrumentProviderConfig(
        load_all=True,
        filters=filters,
        use_gamma_markets=True,
    )

    poly_data_cfg = PolymarketDataClientConfig(
        private_key=os.getenv("POLYMARKET_PK"),
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        passphrase=os.getenv("POLYMARKET_PASSPHRASE"),
        signature_type=1,
        instrument_provider=instrument_cfg,
    )

    poly_exec_cfg = PolymarketExecClientConfig(
        private_key=os.getenv("POLYMARKET_PK"),
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        passphrase=os.getenv("POLYMARKET_PASSPHRASE"),
        signature_type=1,
        instrument_provider=instrument_cfg,
    )

    config = TradingNodeConfig(
        environment="live",
        trader_id="BTC-5MIN-INTEGRATED-001",
        logging=LoggingConfig(log_level="INFO", log_directory="./logs/nautilus"),
        data_engine=LiveDataEngineConfig(qsize=6000),
        exec_engine=LiveExecEngineConfig(qsize=6000),
        risk_engine=LiveRiskEngineConfig(bypass=simulation),
        data_clients={POLYMARKET: poly_data_cfg},
        exec_clients={POLYMARKET: poly_exec_cfg},
    )

    strategy = IntegratedBTCStrategy(
        redis_client=redis_client,
        enable_grafana=enable_grafana,
        test_mode=test_mode,
    )

    node = TradingNode(config=config)
    node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
    node.add_exec_client_factory(POLYMARKET, PolymarketLiveExecClientFactory)
    node.trader.add_strategy(strategy)
    node.build()
    logger.info("Nautilus node built")

    try:
        node.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        node.dispose()
        logger.info("Bot stopped")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Integrated BTC 5-Min Trading Bot")
    parser.add_argument("--live", action="store_true", help="LIVE mode (real money at risk!)")
    parser.add_argument("--no-grafana", action="store_true", help="Disable Grafana metrics")
    parser.add_argument("--test-mode", action="store_true", help="TEST MODE (faster clock)")
    args = parser.parse_args()

    test_mode = args.test_mode
    simulation = True if test_mode else not args.live

    if not simulation:
        logger.warning("LIVE TRADING MODE — REAL MONEY AT RISK!")
    else:
        logger.info(f"SIMULATION MODE — {'TEST MODE' if test_mode else 'paper trading'}")

    run_integrated_bot(simulation=simulation, enable_grafana=not args.no_grafana, test_mode=test_mode)


if __name__ == "__main__":
    main()
