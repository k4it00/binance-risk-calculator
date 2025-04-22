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
    """Get funding rate data with more comprehensive information"""
    url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol.upper()}"
    data = make_api_request(url)
    if not data:
        return None
    
    return {
        'last_funding_rate': float(data.get('lastFundingRate', 0)) * 100,
        'next_funding_time': datetime.fromtimestamp(data.get('nextFundingTime', 0)/1000),
        'mark_price': float(data.get('markPrice', 0))
    }

def get_market_data(symbol: str) -> dict:
    """Get additional market data for better analysis"""
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
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval={interval}&limit={limit}"
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
    
    # Calculate price movement from entry to stop loss
    if position_type == "Long":
        price_movement_percent = stop_loss_percent
    else:
        price_movement_percent = stop_loss_percent
    
    # Calculate position size that will lose exactly risk_amount when stopped out
    # Factor in the leverage since price movement is multiplied by leverage
    leveraged_position_size = (risk_amount / (price_movement_percent / 100)) / leverage
    
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
    adjusted_leveraged_position_size = (adjusted_risk_amount / (price_movement_percent / 100)) / leverage
    
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
        'position_size': adjusted_leveraged_position_size * leverage,  # Total position size with leverage
        'actual_position_size': adjusted_leveraged_position_size,      # Position size without leverage
        'margin_required': adjusted_leveraged_position_size,           # Capital actually being risked
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


# === Streamlit UI functions ===
def plot_price_chart(df, entry_price, stop_loss_price, take_profit_price, liquidation_price, position_type):
    """Create a price chart with position levels"""
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
    
    # Customize chart
    fig.update_layout(
        title=f"Price Chart with {'Long' if position_type == 'Long' else 'Short'} Position Levels",
        xaxis_title="Time",
        yaxis_title="Price (USDT)",
        height=500,
        xaxis_rangeslider_visible=False,
        template="plotly_dark"
    )
    
    return fig

def display_win_probability_chart(r_ratio):
    """Display win probability chart based on R ratio"""
    win_rates = [10, 20, 30, 40, 50, 60, 70, 80, 90]
    expected_values = [(win_rate/100 * r_ratio) - ((100-win_rate)/100) for win_rate in win_rates]
    
    fig = go.Figure()
    
    fig.add_trace(go.Bar(
        x=win_rates,
        y=expected_values,
        marker_color=['red' if ev < 0 else 'green' for ev in expected_values],
        text=[f"{ev:.2f}" for ev in expected_values],
        textposition='auto'
    ))
    
    fig.update_layout(
        title="Expected Value by Win Rate",
        xaxis_title="Win Rate (%)",
        yaxis_title="Expected Value (R)",
        height=300,
        template="plotly_dark"
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
    st.set_page_config(page_title="Binance Futures Risk Calculator", layout="wide")
    
    # Define custom CSS
    st.markdown("""
    <style>
    .main-title {
        font-size: 2.5rem;
        font-weight: 700;
        color: #F0B90B;
        text-align: center;
        margin-bottom: 1rem;
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 2px;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 4px 4px 0px 0px;
        padding: 10px 20px;
        background-color: #1E2126;
    }
    .stTabs [aria-selected="true"] {
        background-color: #F0B90B;
        color: black;
    }
    </style>
    """, unsafe_allow_html=True)
    
    # App header
    st.markdown('<div class="main-title">📊 Binance Futures Risk Calculator Pro</div>', unsafe_allow_html=True)
    
    # Sidebar inputs
    with st.sidebar:
        st.subheader("Position Parameters")
        
        # Common parameters
        symbol = st.text_input("Trading Pair", value="BTCUSDT").upper()
        position_type = st.radio("Position Type", ["Long", "Short"])
        
        # Account and risk parameters
        st.subheader("Account & Risk Parameters")
        account_balance = st.number_input("Account Balance (USDT)", value=1000.0, step=100.0, min_value=10.0)
        risk_percent = st.slider("Risk % of Account", min_value=0.1, max_value=5.0, value=1.0, step=0.1)
        
        # Position parameters
        st.subheader("Position Parameters")
        col1, col2 = st.columns(2)
        with col1:
            stop_loss_percent = st.number_input("Stop Loss %", min_value=0.1, max_value=20.0, value=1.5, step=0.1)
        with col2:
            take_profit_percent = st.number_input("Take Profit %", min_value=0.1, max_value=50.0, value=4.5, step=0.1)
        
        leverage = st.slider("Leverage", min_value=1, max_value=125, value=10)
        
        # Trading fee type
        fee_type = st.radio("Fee Level", ["Maker/Taker (0.02%/0.04%)", "Standard (0.04%/0.06%)", "High (0.05%/0.08%)"])
        
        if fee_type == "Maker/Taker (0.02%/0.04%)":
            maker_fee, taker_fee = 0.0002, 0.0004
        elif fee_type == "Standard (0.04%/0.06%)":
            maker_fee, taker_fee = 0.0004, 0.0006
        else:
            maker_fee, taker_fee = 0.0005, 0.0008
        
        # Advanced settings toggle
        show_advanced = st.checkbox("Show Advanced Options")
        
        if show_advanced:
            maintenance_margin = st.slider("Maintenance Margin %", min_value=0.2, max_value=5.0, value=0.5, step=0.1) / 100
        else:
            maintenance_margin = 0.005
        
        # Add a "Calculate" button
        calc_button = st.button("📊 Calculate Position", use_container_width=True)
    
    # Main content area
    if calc_button:
        with st.spinner("Fetching latest market data..."):
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
            
            # Create tabs for different information sections
            tabs = st.tabs(["Position Overview", "Analysis", "Chart", "Market Data"])
            
            # Tab 1: Position Overview
            with tabs[0]:
                # Market price and key metrics
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric(
                        label="Current Price", 
                        value=f"${entry_price:,.2f}",
                        delta=f"{market_data['price_change_percent']:.2f}%"
                    )
                with col2:
                    st.metric(
                        label="24h High/Low", 
                        value=f"${market_data['high']:,.2f}",
                        delta=f"Low: ${market_data['low']:,.2f}"
                    )
                with col3:
                    st.metric(
                        label="Funding Rate", 
                        value=f"{funding_data['last_funding_rate']:.4f}%",
                        delta=f"Next: {format_funding_time(funding_data['next_funding_time'])}"
                    )
                
                # Position details
                st.subheader(f"{'🟢 Long' if position_type == 'Long' else '🔴 Short'} Position Details")
                
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"""
                    ### Size & Margin
                    - **Position Size**: ${position_details['position_size']:,.2f}
                    - **Margin Required**: ${position_details['margin_required']:,.2f}
                    - **Leverage**: {leverage}x
                    - **Account Risk**: ${position_details['risk_amount']:,.2f} ({risk_percent:.1f}%)
                    """)
                
                with col2:
                    st.markdown(f"""
                    ### Fees & Costs
                    - **Entry Fee**: ${position_details['entry_fee']:,.2f}
                    - **Exit Fee (SL)**: ${position_details['exit_fee_sl']:,.2f}
                    - **Exit Fee (TP)**: ${position_details['exit_fee_tp']:,.2f}
                    - **Total Fees (worst case)**: ${position_details['total_fees_sl']:,.2f}
                    """)
                
                # Key price levels
                st.subheader("Key Price Levels")
                col1, col2, col3, col4 = st.columns(4)
                
                with col1:
                    st.markdown(f"""
                    ### Entry
                    **${entry_price:,.2f}**
                    """)
                
                with col2:
                    sl_color = "🔴" if position_details['potential_loss'] < 0 else "🟢"
                    st.markdown(f"""
                    ### Stop Loss {sl_color}
                    **${position_details['stop_loss_price']:,.2f}**
                    Loss: ${abs(position_details['potential_loss']):,.2f}
                    """)
                
                with col3:
                    tp_color = "🟢" if position_details['potential_profit'] > 0 else "🔴"
                    st.markdown(f"""
                    ### Take Profit {tp_color}
                    **${position_details['take_profit_price']:,.2f}**
                    Profit: ${position_details['potential_profit']:,.2f}
                    """)
                
                with col4:
                    st.markdown(f"""
                    ### Liquidation ⚠️
                    **${position_details['liquidation_price']:,.2f}**
                    Distance: {abs(((position_details['liquidation_price'] - entry_price) / entry_price) * 100):,.2f}%
                    """)
            
            # Tab 2: Analysis
            with tabs[1]:
                col1, col2 = st.columns(2)
                
                with col1:
                    st.subheader("Risk/Reward Analysis")
                    st.markdown(f"""
                    - **Risk/Reward Ratio**: 1:{position_details['r_multiple']:.2f}
                    - **Breakeven Win Rate**: {position_details['breakeven_win_rate']:.1f}%
                    - **Potential Loss**: ${abs(position_details['potential_loss']):,.2f}
                    - **Potential Profit**: ${position_details['potential_profit']:,.2f}
                    """)
                    
                    st.markdown("""
                    ### Strategy Guidelines
                    
                    For consistent profitability:
                    - Aim for R:R ratios of at least 1:2
                    - Keep risk per trade between 1-2% of account
                    - Use technical analysis to place stop losses at logical levels
                    - Consider reducing position size during high volatility
                    """)
                
                with col2:
                    st.subheader("Win Rate Analysis")
                    st.plotly_chart(display_win_probability_chart(position_details['r_multiple']))
            
            # Tab 3: Chart
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
            
            # Tab 4: Market Data
            with tabs[3]:
                col1, col2 = st.columns(2)
                
                with col1:
                    st.subheader("Market Overview")
                    st.markdown(f"""
                    ### {symbol} Market Data
                    - **24h Volume**: {market_data['volume']:,.2f}
                    - **24h Price Change**: {market_data['price_change_percent']:.2f}%
                    - **24h High**: ${market_data['high']:,.2f}
                    - **24h Low**: ${market_data['low']:,.2f}
                    """)
                    
                    st.subheader("Funding Information")
                    next_funding_time = funding_data['next_funding_time'].strftime("%Y-%m-%d %H:%M:%S")
                    st.markdown(f"""
                    - **Current Rate**: {funding_data['last_funding_rate']:.4f}%
                    - **Next Funding Time**: {next_funding_time}
                    - **Funding Impact (8h)**: ${(funding_data['last_funding_rate'] / 100 * position_details['position_size']):,.2f}
                    """)
                
                with col2:
                    st.subheader("Position Health")
                    
                    # Calculate distance to liquidation
                    liq_distance = abs((position_details['liquidation_price'] - entry_price) / entry_price * 100)
                    
                    # Set thresholds
                    if liq_distance < 5:
                        liq_status = "⚠️ DANGER - Very close to liquidation!"
                        liq_color = "red"
                    elif liq_distance < 10:
                        liq_status = "⚠️ WARNING - Liquidation risk present"
                        liq_color = "orange"
                    elif liq_distance < 20:
                        liq_status = "✓ ACCEPTABLE - Moderate safety buffer"
                        liq_color = "yellow"
                    else:
                        liq_status = "✅ SAFE - Good distance from liquidation"
                        liq_color = "green"
                    
                    st.markdown(f"""
                    ### Liquidation Risk
                    <div style="color: {liq_color}; font-weight: bold; font-size: 18px;">{liq_status}</div>
                    """, unsafe_allow_html=True)
                    
                    # Create liquidation distance progress bar
                    st.progress(min(1.0, max(0.0, 1.0 - (liq_distance * 0.05))))
                    st.caption(f"Distance to liquidation: {liq_distance:.2f}%")
                    
                    # Position cost analysis
                    st.subheader("Cost Analysis")
                    st.markdown(f"""
                    ### Position Costs
                    - **Margin Required**: ${position_details['margin_required']:,.2f}
                    - **Fees as % of Margin**: {(position_details['total_fees_sl'] / position_details['margin_required'] * 100):,.2f}%
                    """)
    else:
        # Show welcome message when calculator is not yet run
        st.markdown("""
        ## Welcome to the Binance Futures Risk Calculator Pro
        
        This advanced tool helps you:
        
        1. **Calculate optimal position sizing** based on your risk tolerance
        2. **Visualize key price levels** including stop loss, take profit, and liquidation
        3. **Analyze risk-reward ratios** and potential outcomes
        4. **Track funding rates** and market conditions
        
        To get started:
        1. Enter your trading pair in the sidebar (e.g., BTCUSDT, ETHUSDT)
        2. Configure your account size and risk parameters
        3. Set your desired stop loss, take profit and leverage
        4. Click "Calculate Position" to see comprehensive analysis
        
        **This calculator is for educational purposes only. Always do your own research and trade responsibly.**
        """)
        
        # Display tips section
        st.info("""
        ### Tips for Responsible Trading
        
        - Never risk more than 1-2% of your account per trade
        - Use leverage cautiously - higher leverage means higher liquidation risk
        - Always use stop losses to protect your capital
        - Consider the impact of funding rates on long-term positions
        - Monitor market volatility and adjust position sizes accordingly
        """)
    
    # Footer
    st.markdown("---")
    st.markdown("""
    <div style="text-align: center; color: gray; font-size: 0.8rem;">
    This calculator is for educational purposes only. Trading cryptocurrency futures involves significant risk.
    </div>
    """, unsafe_allow_html=True)

if __name__ == "__main__":
    main()
    
#streamlit run riskmanagement.py 
