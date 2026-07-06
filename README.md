# BTC/USDT Machine Learning Futures Trading Bot

A production-grade, modular automated ML trading bot for **BTC/USDT** that
executes mock **LONG and SHORT** trades on the **Binance USDⓈ-M Futures Testnet**
and ships with a real-time, interactive **Streamlit** dashboard.

> Educational / sandbox use only. It trades exclusively against the Binance
> **Futures Testnet** (fake funds). Do **not** point it at live keys.

This build is a **multi-class directional** strategy: the model predicts LONG,
SHORT, or CASH, so it can profit in both rising and falling markets.

---

## Architecture


| File                 | Responsibility                                                                            |
| -------------------- | ----------------------------------------------------------------------------------------- |
| `config.py`          | Global settings, credentials, futures endpoints, leverage/margin, logging.                |
| `data_pipeline.py`   | Pulls 4 years of 1h **futures** candles, caches to `historical_btc.parquet`.              |
| `feature_factory.py` | Technical indicators, support/resistance, **3-class** triple-barrier target.              |
| `model_brain.py`     | Trains multi-class `XGBClassifier` (`multi:softprob`), dual-direction backtest + metrics. |
| `bot_loop.py`        | Threaded futures execution engine (long/short + flip), bracket manager, CSV logging.      |
| `dashboard.py`       | Wide dark Streamlit dashboard (long/short metrics, dual gauges, candlesticks, logs).      |


Data flow:

```
data_pipeline -> feature_factory -> model_brain (train + save model)
                                          |
                                          v
                       bot_loop (live thread) -> bot_status_log.csv -> dashboard
```

---

## 1. Setup (virtual environment + dependencies)

From the project root (`Crypto_Bot/`):

```bash
# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate

# Upgrade pip and install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

## 2. Configure Futures Testnet credentials

1. Create free **Futures** API keys at [https://testnet.binancefuture.com](https://testnet.binancefuture.com)
  (this is different from the spot testnet).
2. Copy the example env file and fill in your keys:

```bash
cp .env.example .env
# then edit .env and set API_KEY / SECRET_KEY
```

Credentials are read from environment variables / `.env` first, falling back to
the placeholder defaults in `config.py`. Leverage and margin mode are
configurable via `LEVERAGE` (default `1`) and `MARGIN_TYPE` (default `ISOLATED`).

## 3. Download data & train the model

```bash
# (optional) pre-download and cache 4 years of candles
python data_pipeline.py

# train, backtest, and save xgboost_trading_model.json
python model_brain.py
```

`model_brain.py` prints an out-of-sample report (Accuracy, Precision, Strategy
Net Profit vs Buy & Hold, Max Drawdown) and writes the model artifact.

## 4. Run the live bot (optional, standalone)

```bash
python bot_loop.py
```

This connects to the Futures Testnet, sets leverage/margin, scores the latest
candles every few seconds, opens a single LONG or SHORT (+1.5% / -1.0% bracket),
flips direction when the signal reverses, and appends every iteration to
`bot_status_log.csv`. Press `Ctrl+C` to stop.

## 5. Launch the dashboard

```bash
streamlit run dashboard.py
```

Open the URL printed in the terminal (usually [http://localhost:8501](http://localhost:8501)).
Use the sidebar **Start Bot / Stop Bot** buttons to run the live trading thread
directly from the dashboard process — no need to run `bot_loop.py` separately.

---

## Data sourcing (important)

The Binance **Testnet only retains a short candle history**, which is far too
little to train a model. The pipeline therefore:

- Pulls **futures market data** (4 years of historical candles + live signal
candles) from the **public mainnet futures** REST API (`fapi.binance.com`) —
no authentication required for klines.
- Routes all **order execution** to the **Futures Testnet** (mock trades).

This is controlled by `USE_MAINNET_DATA` in `config.py` (default `True`).

## Strategy logic (multi-class directional)

- **3-class target** (triple-barrier over a 24h window):
  - `2` **LONG**  — price hits **+1.5%** before **-1.0%**.
  - `1` **SHORT** — price hits **-1.5%** before **+1.0%**.
  - `0` **CASH**  — neither boundary triggered (choppy/sideways).
- **Model**: `XGBClassifier` with `objective='multi:softprob'`, `num_class=3`.
Class imbalance is offset with balanced per-sample weights at fit time.
- **Per-direction thresholds**: training sweeps thresholds on a held-out
validation slice and persists the profit-maximising long/short thresholds to
`decision_threshold.json`. The live bot reads these.
- **Trend regime filter** (`USE_TREND_FILTER`, default on): longs only when
price is **above EMA200**, shorts only when **below EMA200**.
- **Entry**: if `P(LONG) > long_threshold` (trend up) → open LONG (`side=BUY`);
if `P(SHORT) > short_threshold` (trend down) → open SHORT (`side=SELL`). An
opposing open position is closed first (flip).
- **Exit (bracket)**: reduce-only market order at **±1.5%** take-profit /
**∓1.0%** stop-loss relative to entry, direction-aware.
- **Sizing**: each entry uses **10%** of available margin × `LEVERAGE`.

### Reading the metrics

Results depend heavily on the out-of-sample window. The default 20% test slice
lands in a severe bear market (buy & hold ≈ **−47%**). With shorting enabled the
strategy preserves capital — e.g. ~~**−2%** with ~**9% max drawdown** and a high
**short precision (~~74%)** — beating buy & hold by ~45 points. Adding short
trades is what lets the bot exploit downtrends instead of only surviving them.
Judge the bot on **risk-adjusted** performance vs the benchmark, not raw return.

## Notes & troubleshooting

- **Rate limits**: historical data is cached to parquet; delete
`historical_btc.parquet` to force a fresh download.
- `**Model artifact not found`**: run `python model_brain.py` before starting
the bot.
- **Leverage / margin**: tune `LEVERAGE` and `MARGIN_TYPE` in `.env` or
`config.py`. Default is a safe `1x` ISOLATED.
- **Min notional**: orders below the futures minimum notional are skipped; fund
your Futures Testnet USDT wallet accordingly.
- **Live auto-refresh**: install `streamlit-autorefresh` (already in
`requirements.txt`); otherwise use the sidebar **Manual Refresh** button.

```

```

