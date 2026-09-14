"""Weather MCP plugin for Robbie — current conditions + 7-day forecast.

Data source: DWD (Deutscher Wetterdienst) via the free Bright Sky JSON API
(https://api.brightsky.dev), no API key. Bright Sky resolves coordinates to the
*nearest* DWD station and returns its name + distance.
"""
