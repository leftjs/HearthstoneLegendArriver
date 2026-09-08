"""Choose One recognition is deliberately separate from Discover."""

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def choose_one_card_ids():
    # Read the project's local metadata only; never download in the click path.
    with (Path(__file__).resolve().parents[2] / 'cards.json').open(encoding='utf8') as source:
        return frozenset(card['id'] for card in json.load(source)
                         if 'CHOOSE_ONE' in card.get('mechanics', []))
