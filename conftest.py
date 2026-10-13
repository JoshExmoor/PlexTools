import os
import sys

# Make the project root importable as top-level modules (get_plex_credentials,
# push_plex_ratings_by_playlist) regardless of where pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
