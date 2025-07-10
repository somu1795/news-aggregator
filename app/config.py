"""
Centralized configuration for the News Aggregator application.

This file consolidates static settings to make them easier to manage and
separates them from the main application logic.
"""

# --- Cache Settings ---
CACHE_KEY = "headlines:v2"
CACHE_TTL_SECONDS = 60

# --- Feed Fetching Settings ---
MAX_HEADLINES = 30 # Increased to get a better variety from a single feed
REQUEST_TIMEOUT = 10.0
FETCH_RETRIES = 2
BACKOFF_BASE = 2 # Base for exponential backoff calculation (e.g., 2 ** attempt)

# --- News Sources ---
# A dictionary of news sources. The key is a descriptive name, and the value is the RSS feed URL.
# Using multiple sources provides a more global and resilient feed.
NEWS_SOURCES = {
    "Google News (Custom Search)": 'https://news.google.com/rss/search?q=news%20from%20world%20-ndtv%20-hindustan%20times%20-India%20Today&hl=en-US&gl=US&ceid=US%3Aen',
    "Google News (Science)": 'https://news.google.com/rss/topics/CAAqKggKIiRDQkFTRlFvSUwyMHZNRFp0Y1RjU0JXVnVMVWRDR2dKSlRpZ0FQAQ?hl=en-US&gl=US&ceid=US%3Aen',
}

# --- Source Weighting ---
# Defines the percentage of headlines to pull from each source to create a balanced mix.
# The values should sum to 1.0.
# The keys must match the keys in NEWS_SOURCES.
SOURCE_WEIGHTS = {
    "Google News (Custom Search)": 0.7,
    "Google News (Science)": 0.3,
}