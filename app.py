import streamlit as st
import requests
import time
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta

# === Binance API functions with error handling and rate limiting ===
def make_api_request(url, max_retries=3, retry_delay=1):
    """Make API request with retry logic and proper error handling"""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                st.error(f"Error fetching data: {e}")
                return None
            time.sleep(retry_delay)
    return None

def get_binance_price(symbol: str) -> float:
    """Get current price from Binance with improved error handling"""
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol.upper()}"
    data = make_api_request(url)
    return float(data['price']) if data else None

def get_funding_rate(symbol: str) -> dict:
    """Get funding rate data"""
    # FIXED: Using the correct endpoint for futures funding rate
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol.upper()}"
    data = make_api_request(url)
    if not data or len(data) == 0:
        return None
    
    # The funding rate endpoint returns an array, so we need to get the first item
    funding_data = data[0] if isinstance(data, list) else data
    
    # Get the mark price separately since it's not in the funding rate endpoint
    mark_price_url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol.upper()}"
    mark_price_data = make_api_request(mark_price_url)
    mark_price = float(mark_price_data.get('markPrice', 0)) if mark_price_data else 0
    
    return {
        'last_funding_rate': float(funding_data.get('fundingRate', 0)) * 100,
        'next_funding_time': datetime.fromtimestamp(int(funding_data.get('fundingTime', 0))/1000),
        'mark_price': mark_price
    }

def get_market_data(symbol: str) -> dict:
    """Get additional market data"""
    # FIXED: Using the correct endpoint for futures market data
    url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol.upper()}"
    data = make_api_request(url)
    if not data:
        return None
    
    return {
        'volume': float(data.get('volume', 0)),
        'price_change_percent': float(data.get('priceChangePercent', 0)),
        'high': float(data.get('highPrice', 0)),
        'low': float(data.get('lowPrice', 0))
    }

def get_historical_prices(symbol, interval='1h', limit=24):
    """Get historical price data for charting"""
    # FIXED: Using the correct futures klines endpoint for futures data
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    data = make_api_request(url)
    if not data:
        return None
    
    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 
                                     'close_time', 'quote_asset_volume', 'number_of_trades', 
                                     'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'])
    
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df[['open', 'high', 'low', 'close']] = df[['open', 'high', 'low', 'close']].astype(float)
    return df

# === Risk calculation functions ===
def calculate_liquidation(entry_price, leverage, position_type, maintenance_margin=0.005):
    """Calculate liquidation price with realistic maintenance margin rates"""
    if position_type == "Long":
        return entry_price * (1 - maintenance_margin - (1 / leverage))
    else:
        return entry_price * (1 + maintenance_margin + (1 / leverage))

def calculate_stop_loss(entry_price, stop_loss_percent, position_type):
    """Calculate stop loss price"""
    if position_type == "Long":
        return entry_price * (1 - stop_loss_percent / 100)
    else:
        return entry_price * (1 + stop_loss_percent / 100)

def calculate_take_profit(entry_price, take_profit_percent, position_type):
    """Calculate take profit price"""
    if position_type == "Long":
        return entry_price * (1 + take_profit_percent / 100)
    else:
        return entry_price * (1 - take_profit_percent / 100)

def calculate_r_multiple(take_profit_percent, stop_loss_percent):
    """Calculate risk-reward ratio"""
    return take_profit_percent / stop_loss_percent

def calculate_position_details(entry_price, account_balance, risk_percent, stop_loss_percent, 
                              take_profit_percent, leverage, position_type, maker_fee=0.0002, taker_fee=0.0004):
    """Calculate comprehensive position details with proper risk adjustment for leverage"""
    # Calculate risk amount in dollars
    risk_amount = account_balance * (risk_percent / 100)
    
    # Calculate position size that will lose exactly risk_amount when stopped out
    # Factor in the leverage since price movement is multiplied by leverage
    leveraged_position_size = (risk_amount / (stop_loss_percent / 100)) / leverage
    
    # Calculate margin required (the actual capital allocated)
    margin_required = leveraged_position_size / leverage
    
    # Calculate fees
    entry_fee = leveraged_position_size * taker_fee
    exit_fee_sl = leveraged_position_size * taker_fee
    exit_fee_tp = leveraged_position_size * maker_fee
    total_fees_sl = entry_fee + exit_fee_sl
    total_fees_tp = entry_fee + exit_fee_tp
    
    # Adjust risk amount to account for fees
    adjusted_risk_amount = risk_amount - total_fees_sl
    
    # Recalculate position size with fee adjustment
    adjusted_leveraged_position_size = (adjusted_risk_amount / (stop_loss_percent / 100)) / leverage
    
    # Calculate stop loss and take profit prices
    stop_loss_price = calculate_stop_loss(entry_price, stop_loss_percent, position_type)
    take_profit_price = calculate_take_profit(entry_price, take_profit_percent, position_type)
    liq_price = calculate_liquidation(entry_price, leverage, position_type)
    
    # Calculate potential profit and loss
    if position_type == "Long":
        sl_loss = adjusted_leveraged_position_size * leverage * (stop_loss_price - entry_price) / entry_price
        tp_profit = adjusted_leveraged_position_size * leverage * (take_profit_price - entry_price) / entry_price
    else:
        sl_loss = adjusted_leveraged_position_size * leverage * (entry_price - stop_loss_price) / entry_price
        tp_profit = adjusted_leveraged_position_size * leverage * (entry_price - take_profit_price) / entry_price
    
    # Account for fees
    sl_loss -= total_fees_sl
    tp_profit -= total_fees_tp
    
    # Calculate R multiple and breakeven win rate
    r_multiple = calculate_r_multiple(take_profit_percent, stop_loss_percent)
    breakeven_win_rate = 1 / (1 + r_multiple)
    
    return {
        'position_size': adjusted_leveraged_position_size * leverage,
        'actual_position_size': adjusted_leveraged_position_size,
        'margin_required': adjusted_leveraged_position_size,
        'risk_amount': risk_amount,
        'entry_fee': entry_fee,
        'exit_fee_sl': exit_fee_sl,
        'exit_fee_tp': exit_fee_tp,
        'total_fees_sl': total_fees_sl,
        'total_fees_tp': total_fees_tp,
        'stop_loss_price': stop_loss_price,
        'take_profit_price': take_profit_price,
        'liquidation_price': liq_price,
        'potential_loss': sl_loss,
        'potential_profit': tp_profit,
        'r_multiple': r_multiple,
        'breakeven_win_rate': breakeven_win_rate * 100,
        'account_risk_percent': risk_percent
    }

def plot_price_chart(df, entry_price, stop_loss_price, take_profit_price, liquidation_price, position_type):
    """Create a mobile-friendly price chart with position levels"""
    fig = go.Figure()
    
    # Add price candlesticks
    fig.add_trace(go.Candlestick(
        x=df['timestamp'],
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close'],
        name='Price Action'
    ))
    
    # Add horizontal lines for entry, SL, TP and liquidation
    line_colors = {
        'Entry': 'white',
        'Stop Loss': 'red',
        'Take Profit': 'green',
        'Liquidation': 'purple'
    }
    
    for label, price in [
        ('Entry', entry_price),
        ('Stop Loss', stop_loss_price),
        ('Take Profit', take_profit_price),
        ('Liquidation', liquidation_price)
    ]:
        fig.add_hline(y=price, line_width=1, line_dash="dash", line_color=line_colors[label],
                     annotation_text=f"{label}: {price:.2f}")
    
    # Customize chart for mobile view
    fig.update_layout(
        title=f"Price Chart with Position Levels",
        xaxis_title="Time",
        yaxis_title="Price (USDT)",
        height=300,  # Smaller height for mobile
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        margin=dict(l=10, r=10, t=50, b=30)  # Compact margins for mobile
    )
    
    return fig

def format_funding_time(dt):
    """Format funding time to user-friendly string"""
    now = datetime.now()
    time_diff = dt - now
    hours, remainder = divmod(time_diff.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    
    if dt < now:
        return "Past due"
    else:
        return f"In {hours}h {minutes}m"

# === Main app ===
def main():
    st.set_page_config(
        page_title="Mobile Risk Calculator", 
        layout="centered",  # Better for mobile
        initial_sidebar_state="collapsed"  # Start with sidebar collapsed on mobile
    )
    
    # Custom CSS for mobile
    st.markdown("""
    <style>
    .main-title {
        font-size: 1.5rem;
        font-weight: 700;
        color: #F0B90B;
        text-align: center;
        margin-bottom: 0.5rem;
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 2px;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 4px 4px 0px 0px;
        padding: 8px 12px;
        font-size: 0.8rem;
        background-color: #1E2126;
    }
    .stTabs [aria-selected="true"] {
        background-color: #F0B90B;
        color: black;
    }
    /* Mobile optimizations */
    @media (max-width: 640px) {
        .stButton button {
            width: 100%;
            padding: 0.5rem;
            font-size: 1rem;
        }
        div[data-testid="stVerticalBlock"] > div {
            padding-left: 0.5rem;
            padding-right: 0.5rem;
        }
    }
    </style>
    """, unsafe_allow_html=True)
    
    # App header (smaller for mobile)
    st.markdown('<div class="main-title">📊 Binance Futures Risk Calculator</div>', unsafe_allow_html=True)
    
    # Main inputs section (outside sidebar for mobile)
    st.subheader("Position Parameters")
    
    # Common parameters - using columns for compact layout
    col1, col2 = st.columns(2)
    with col1:
        symbol = st.text_input("Trading Pair", value="BTCUSDT").upper()
    with col2:
        position_type = st.selectbox("Position Type", ["Long", "Short"])
    
    # Account and risk parameters
    st.subheader("Account & Risk")
    col1, col2 = st.columns(2)
    with col1:
        account_balance = st.number_input("Account Balance (USDT)", value=1000.0, step=100.0, min_value=10.0)
    with col2:
        risk_percent = st.slider("Risk %", min_value=0.1, max_value=5.0, value=1.0, step=0.1)
    
    # Position parameters
    st.subheader("Trading Parameters")
    col1, col2 = st.columns(2)
    with col1:
        stop_loss_percent = st.number_input("Stop Loss %", min_value=0.1, max_value=20.0, value=1.5, step=0.1)
    with col2:
        take_profit_percent = st.number_input("Take Profit %", min_value=0.1, max_value=50.0, value=4.5, step=0.1)
    
    leverage = st.slider("Leverage", min_value=1, max_value=125, value=10)
    
    # Fee type as a simple dropdown for mobile
    fee_options = {
        "Low (0.02%/0.04%)": (0.0002, 0.0004),
        "Standard (0.04%/0.06%)": (0.0004, 0.0006),
        "High (0.05%/0.08%)": (0.0005, 0.0008)
    }
    fee_type = st.selectbox("Fee Level", list(fee_options.keys()))
    maker_fee, taker_fee = fee_options[fee_type]
    
    # Simplified advanced options (just a checkbox toggle)
    show_advanced = st.checkbox("Show Advanced Options")
    if show_advanced:
        maintenance_margin = st.slider("Maintenance Margin %", min_value=0.2, max_value=5.0, value=0.5, step=0.1) / 100
    else:
        maintenance_margin = 0.005
    
    # Calculate button - full width for easy tapping on mobile
    calc_button = st.button("📊 Calculate Position", use_container_width=True)
    
    # Main content area
    if calc_button:
        with st.spinner("Fetching data..."):
            # Fetch market data
            entry_price = get_binance_price(symbol)
            funding_data = get_funding_rate(symbol)
            market_data = get_market_data(symbol)
            historical_data = get_historical_prices(symbol)
            
            if not all([entry_price, funding_data, market_data, historical_data is not None]):
                st.error(f"Failed to fetch data for {symbol}. Please check the symbol and try again.")
                return
            
            # Calculate position details
            position_details = calculate_position_details(
                entry_price=entry_price,
                account_balance=account_balance,
                risk_percent=risk_percent,
                stop_loss_percent=stop_loss_percent,
                take_profit_percent=take_profit_percent,
                leverage=leverage,
                position_type=position_type,
                maker_fee=maker_fee,
                taker_fee=taker_fee
            )
            
            # Use tabs with simplified names for mobile space efficiency
            tabs = st.tabs(["Overview", "Price Levels", "Chart", "Market"])
            
            # Tab 1: Overview
            with tabs[0]:
                # Market price and funding rate
                col1, col2 = st.columns(2)
                with col1:
                    st.metric(
                        label="Current Price", 
                        value=f"${entry_price:,.2f}",
                        delta=f"{market_data['price_change_percent']:.2f}%"
                    )
                with col2:
                    st.metric(
                        label="Funding Rate", 
                        value=f"{funding_data['last_funding_rate']:.4f}%",
                        delta=f"Next: {format_funding_time(funding_data['next_funding_time'])}"
                    )
                
                # Position summary - simplified for mobile
                st.subheader(f"{'🟢 Long' if position_type == 'Long' else '🔴 Short'} Position")
                
                # Position size and risk - succinct for mobile
                st.info(f"""
                • Position: ${position_details['position_size']:,.2f}
                • Margin: ${position_details['margin_required']:,.2f}
                • Risk: ${position_details['risk_amount']:,.2f} ({risk_percent:.1f}%)
                • R:R Ratio: 1:{position_details['r_multiple']:.2f}
                • Fees: ${position_details['total_fees_sl']:,.2f}
                """)
            
            # Tab 2: Price Levels
            with tabs[1]:
                # Key price levels with colors for visual hierarchy
                st.subheader("Key Price Levels")
                
                # Entry
                st.markdown(f"""
                **Entry Price:** ${entry_price:,.2f}
                """)
                
                # Stop Loss - red background
                st.markdown(f"""
                <div style="background-color: rgba(255,0,0,0.1); padding: 8px; border-radius: 5px; margin-bottom: 8px;">
                <strong>Stop Loss:</strong> ${position_details['stop_loss_price']:,.2f}<br>
                Loss: ${abs(position_details['potential_loss']):,.2f}
                </div>
                """, unsafe_allow_html=True)
                
                # Take Profit - green background
                st.markdown(f"""
                <div style="background-color: rgba(0,255,0,0.1); padding: 8px; border-radius: 5px; margin-bottom: 8px;">
                <strong>Take Profit:</strong> ${position_details['take_profit_price']:,.2f}<br>
                Profit: ${position_details['potential_profit']:,.2f}
                </div>
                """, unsafe_allow_html=True)
                
                # Liquidation - yellow/warning background
                liq_distance = abs((position_details['liquidation_price'] - entry_price) / entry_price * 100)
                st.markdown(f"""
                <div style="background-color: rgba(255,255,0,0.1); padding: 8px; border-radius: 5px;">
                <strong>Liquidation:</strong> ${position_details['liquidation_price']:,.2f}<br>
                Distance: {liq_distance:.2f}%
                </div>
                """, unsafe_allow_html=True)
                
                # Win rate needed
                st.info(f"Breakeven Win Rate: {position_details['breakeven_win_rate']:.1f}%")
            
            # Tab 3: Chart - simplified for mobile
            with tabs[2]:
                st.plotly_chart(
                    plot_price_chart(
                        historical_data,
                        entry_price,
                        position_details['stop_loss_price'],
                        position_details['take_profit_price'],
                        position_details['liquidation_price'],
                        position_type
                    ),
                    use_container_width=True
                )
            
            # Tab 4: Market Data - simplified for mobile
            with tabs[3]:
                # Market overview
                st.subheader(f"{symbol} Market Data")
                
                # Create two columns for market stats
                col1, col2 = st.columns(2)
                
                with col1:
                    st.metric("24h Volume", f"{market_data['volume']:,.1f}")
                    st.metric("24h High", f"${market_data['high']:,.2f}")
                
                with col2:
                    st.metric("24h Change", f"{market_data['price_change_percent']:.2f}%")
                    st.metric("24h Low", f"${market_data['low']:,.2f}")
                
                # Funding impact estimation
                funding_impact = funding_data['last_funding_rate'] / 100 * position_details['position_size']
                st.info(f"Funding Impact (8h): ${funding_impact:,.2f}")
                
                # Liquidation risk indicator
                if liq_distance < 5:
                    liq_status = "⚠️ DANGER - Very close to liquidation!"
                    liq_color = "red"
                elif liq_distance < 10:
                    liq_status = "⚠️ WARNING - Liquidation risk"
                    liq_color = "orange"
                else:
                    liq_status = "✅ SAFE - Good distance"
                    liq_color = "green"
                
                st.markdown(f"""
                <div style="color: {liq_color}; font-weight: bold; font-size: 16px;">{liq_status}</div>
                """, unsafe_allow_html=True)
    else:
        # Welcome message - simplified for mobile
        st.info("""
        ### Quick Start Guide
        
        1. Enter your trading pair (e.g., BTCUSDT)
        2. Set your account size and risk %
        3. Define stop loss and take profit
        4. Select your leverage
        5. Tap "Calculate Position"
        """)
    
    # Footer - simplified for mobile
    st.caption("This calculator is for educational purposes only. Trading cryptocurrency futures involves significant risk.")

if __name__ == "__main__":
    main()