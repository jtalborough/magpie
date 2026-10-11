"""Memorable random-word labels for bins.

Instead of "C3·R5", a cell gets a name like "avocado" or "antelope" — easy to
say and remember ("it's in avocado"). The grid still tracks col/row for layout;
the word is just the human label. Words are short, distinct, and unambiguous.
"""

from __future__ import annotations

import random
import zlib

WORDS: list[str] = [
    # animals
    "antelope", "badger", "beaver", "bison", "bobcat", "camel", "cheetah", "cobra",
    "condor", "cougar", "coyote", "cricket", "dingo", "dolphin", "donkey", "falcon",
    "ferret", "finch", "gecko", "gibbon", "giraffe", "gopher", "heron", "hornet",
    "ibex", "iguana", "jackal", "jaguar", "kestrel", "koala", "lemur", "lynx",
    "magpie", "marmot", "meerkat", "mongoose", "moose", "narwhal", "newt", "ocelot",
    "osprey", "otter", "panda", "panther", "pelican", "puffin", "quail", "rabbit",
    "raccoon", "raven", "robin", "salmon", "sparrow", "stoat", "swallow", "tapir",
    "toucan", "turtle", "viper", "walrus", "weasel", "wombat", "yak", "zebra",
    # fruits & veg
    "almond", "apricot", "avocado", "banana", "basil", "beet", "carrot", "cashew",
    "cherry", "chili", "clove", "cocoa", "date", "fennel", "fig", "ginger",
    "guava", "hazel", "kiwi", "lemon", "lentil", "lime", "lychee", "mango",
    "melon", "olive", "onion", "papaya", "parsnip", "peach", "pear", "pecan",
    "pepper", "plum", "pumpkin", "quince", "radish", "raisin", "rhubarb", "sage",
    "shallot", "thyme", "tomato", "turnip", "walnut", "wasabi", "yam",
    # nature & objects
    "amber", "anchor", "arrow", "aspen", "atlas", "beacon", "birch", "boulder",
    "brook", "canyon", "cedar", "cinder", "cobble", "comet", "coral", "cove",
    "crater", "dune", "ember", "fjord", "flint", "fossil", "garnet", "geyser",
    "glacier", "granite", "harbor", "ivy", "jade", "jasper", "kelp", "lagoon",
    "lantern", "ledger", "lichen", "marble", "meadow", "mesa", "moss", "nebula",
    "oasis", "opal", "pebble", "pinecone", "pier", "prairie", "quartz", "quill",
    "ravine", "reef", "ripple", "saffron", "sequoia", "shale", "slate", "summit",
    "tangle", "thicket", "topaz", "tundra", "vellum", "willow",
    # tools / shop-ish
    "anvil", "auger", "bolt", "chisel", "clamp", "cog", "dowel", "ferrule",
    "gasket", "gimlet", "hinge", "lathe", "mallet", "pliers", "ratchet", "rivet",
    "spindle", "sprocket", "torque", "vise", "washer", "wedge", "wrench",
]


def label_for_index(parent_id: str, index: int) -> str:
    """Deterministic, unique-within-parent word for the index-th cell of a grid.

    A stable per-parent offset plus the cell index walks the list so no two cells
    in the same drawer share a word (until the grid exceeds the list size)."""
    base = zlib.crc32(parent_id.encode("utf-8")) % len(WORDS)
    i = base + index
    word = WORDS[i % len(WORDS)]
    return word if index < len(WORDS) else f"{word}-{index // len(WORDS) + 1}"


def pick_unused(used: set[str]) -> str:
    """A random word not already used by siblings (for on-demand single cells)."""
    available = [w for w in WORDS if w not in used]
    if available:
        return random.choice(available)
    return f"{random.choice(WORDS)}-{len(used)}"
