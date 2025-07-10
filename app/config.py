"""
Centralized configuration for the News Aggregator application.

This file consolidates static settings to make them easier to manage and
separates them from the main application logic.
"""

# --- Cache Settings ---
CACHE_KEY = "headlines:v2"
CACHE_TTL_SECONDS = 60

# --- Feed Fetching Settings ---
MAX_HEADLINES = 20 # Increased to get a better variety from a single feed
REQUEST_TIMEOUT = 10.0
FETCH_RETRIES = 2
BACKOFF_BASE = 2 # Base for exponential backoff calculation (e.g., 2 ** attempt)

# --- News Source ---
# Using a single aggregated feed from Google News.
GOOGLE_NEWS_URL = 'https://news.google.com/rss/search?q=news%20from%20world%20-ndtv%20-hindustan%20times%20-India%20Today&hl=en-US&gl=US&ceid=US%3Aen'