# test_confidence.py
import pandas as pd
from analysis.signals import composite_strategy

# یک نمونه داده
test_data = {
    'Price': 50000,
    'close': 50000,
    'RSI': 45,
    'MACD': 100,
    'MACD Signal': 95,
    'ADX': 30,
    '+DI': 25,
    '-DI': 15,
    'EMA50': 49000,
    'EMA200': 48000,
    'BB Upper': 52000,
    'BB Lower': 48000,
    'ATR %': 3.5,
    'Relative_Volume': 1.5,
    '24h Change (%)': 2.5,
    'Market Cap': 1000000000,
}

result = composite_strategy(test_data)

print("Signal:", result.get('signal'))
print("Score:", result.get('score'))
print("Confidence:", result.get('confidence'))  # باید بین 0-100 باشد
print("Quality:", result.get('quality'))
print("Tradable:", result.get('tradable_long'))