"""What kinds of item the full sell loop may buy as store deals: what's worth trading, not just
cheap. By the item's game id (its category) and name:

- throwing stars, except those an NPC sells (Subi, Wolbi): buying them from players to sell on
  only works on players who don't know the NPC sells them
- scrolls, except HP, MP, Magic Def. and Evasion ones, and the DEF scrolls that hardly trade
  (Cape Weapon Def., Bottomwear DEF)
- Elixirs
- ETC items used in a crafting recipe (craft level 8 and under) or that are a catalyst, except:
  - monster drops (40000xx), whose prices are mostly people finishing a quest, unless named in
    allow_drops
  - ingots and gems made from ore, when they sell for under refined_premium times what making
    them costs: crafters make those for experience and sell at cost, so there's nothing to flip

Everything else (other USE items, chairs and setup items, quest items, equipment) isn't bought.
"""
import json
import re
from pathlib import Path

RECIPES_FILE = Path(__file__).parent / "data" / "recipes_meowdb.json"
MAX_CRAFT_LEVEL = 8
NPC_STARS = ("Subi Throwing Stars", "Wolbi Throwing Stars")
BAD_SCROLLS = re.compile(r"\b(HP|MP)\b|Magic Def|Evasion|Cape Weapon Def|Bottomwear DEF", re.I)


def load_recipes(path=None):
    """{ingredient name: [recipes using it]}, {made item: its recipe}, {catalyst names}; empty when
    there's no recipe file."""
    try:
        recipes = json.loads(Path(path or RECIPES_FILE).read_text("utf-8"))["recipes"]
    except (OSError, ValueError, KeyError):
        return {}, {}, set()
    recipes = [r for r in recipes if r.get("craft_level", 0) <= MAX_CRAFT_LEVEL]
    uses, makes = {}, {}
    for r in recipes:
        makes.setdefault(r["name"], r)
        for g in r["ingredients"]:
            uses.setdefault(g["name"], []).append(r["name"])
    catalysts = {r["catalyst"]["name"] for r in recipes if r.get("catalyst")}
    return uses, makes, catalysts


def craft_cost(recipe, reference):
    """What making one costs at reference prices (ingredients and fee, catalyst left out: refining
    ore doesn't use one), or None when an ingredient has no price."""
    cost = recipe.get("fee") or 0
    for g in recipe["ingredients"]:
        each = reference(g["name"])
        if not each:
            return None
        cost += each * g["qty"]
    return cost / (recipe.get("makes") or 1)


def slot_kind(name, ids, consumables=()):
    """Which of the store's reserved slots an item takes: "consumable", "scroll" or "other"."""
    if name in consumables:
        return "consumable"
    return "scroll" if str(ids.get(name, "")).startswith("204") else "other"


def fits(kind, counts, reserved, max_items):
    """Whether one more item of this kind has a slot: its kind's reserved slots first, then the
    ones nobody has reserved. counts: {kind: slots taken}."""
    if counts.get(kind, 0) < reserved.get(kind, 0):
        return True
    general = max_items - sum(reserved.values())
    used = sum(max(0, n - reserved.get(k, 0)) for k, n in counts.items())
    return used < general


class Rules:
    def __init__(self, ids, reference, allow_drops=(), refined_premium=1.25, recipes=None):
        """ids: name -> game item id. reference(name): its price, or None."""
        self.ids, self.reference = ids, reference
        self.allow_drops = set(allow_drops)
        self.refined_premium = refined_premium
        self.uses, self.makes, self.catalysts = recipes if recipes is not None else load_recipes()

    def why_not(self, name):
        """Why this item isn't a store deal at all, or "" if it can be one."""
        game_id = self.ids.get(name)
        if game_id is None:
            return "unknown item"
        kind = str(game_id)
        if kind.startswith("207"):
            return "an NPC sells it" if name in NPC_STARS else ""
        if kind.startswith("204"):
            return "not a scroll worth trading" if BAD_SCROLLS.search(name) else ""
        if kind.startswith("2"):
            return "" if "Elixir" in name else "not a USE item worth trading"
        if not kind.startswith("4"):
            return "not an ETC or USE item worth trading"
        if name not in self.uses and name not in self.catalysts:
            return "not used in crafting"
        if kind.startswith("40000") and name not in self.allow_drops:
            return "a monster drop"
        if kind.startswith(("40101", "40201")) and name in self.makes:
            price, cost = self.reference(name), craft_cost(self.makes[name], self.reference)
            if not price or not cost or price < self.refined_premium * cost:
                return "made from ore and sold at about what making it costs"
        return ""
