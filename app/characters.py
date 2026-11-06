"""Parse a D&D Beyond character export (character-service v5 `data`) into a compact sheet.

D&D Beyond sends ingredients, not totals: base scores plus every modifier from race,
class, background, feats and items. This module adds them up the way the sheet does for
the common cases. AC is marked approximate (spell effects like Mage Armor aren't in the
export as active effects).
"""
import html
import math
import re

ABILITIES = ["str", "dex", "con", "int", "wis", "cha"]
ABILITY_NAMES = {"str": "Strength", "dex": "Dexterity", "con": "Constitution",
                 "int": "Intelligence", "wis": "Wisdom", "cha": "Charisma"}
SCORE_SLUG = {"strength-score": "str", "dexterity-score": "dex", "constitution-score": "con",
              "intelligence-score": "int", "wisdom-score": "wis", "charisma-score": "cha"}
SKILLS = {
    "acrobatics": "dex", "animal-handling": "wis", "arcana": "int", "athletics": "str",
    "deception": "cha", "history": "int", "insight": "wis", "intimidation": "cha",
    "investigation": "int", "medicine": "wis", "nature": "int", "perception": "wis",
    "performance": "cha", "persuasion": "cha", "religion": "int", "sleight-of-hand": "dex",
    "stealth": "dex", "survival": "wis",
}
ALIGNMENTS = {1: "Lawful Good", 2: "Neutral Good", 3: "Chaotic Good", 4: "Lawful Neutral",
              5: "True Neutral", 6: "Chaotic Neutral", 7: "Lawful Evil", 8: "Neutral Evil",
              9: "Chaotic Evil"}
ARMOR_LIGHT, ARMOR_MEDIUM, ARMOR_HEAVY, ARMOR_SHIELD = 1, 2, 3, 4


def mod(score: int) -> int:
    return (score - 10) // 2


def signed(n: int) -> str:
    return f"+{n}" if n >= 0 else str(n)


def strip_html(s: str | None, limit: int | None = None) -> str:
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>|</p>|</li>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


def _active_modifiers(d: dict) -> list[dict]:
    """All modifiers that currently apply. Item modifiers only count for equipped items
    (and attuned ones, if the item needs attunement)."""
    active_items = {i["definition"]["id"] for i in d.get("inventory", [])
                    if i.get("equipped") and (not i["definition"].get("canAttune") or i.get("isAttuned"))}
    out = []
    for group, mods in (d.get("modifiers") or {}).items():
        for m in mods or []:
            if group == "item" and m.get("componentId") not in active_items:
                continue
            out.append(m)
    return out


def parse(d: dict) -> dict:
    mods = _active_modifiers(d)

    def find(type_=None, sub=None):
        return [m for m in mods if (type_ is None or m.get("type") == type_)
                and (sub is None or m.get("subType") == sub)]

    def total_bonus(sub: str) -> int:
        return sum(m.get("value") or 0 for m in find("bonus", sub))

    # ---- level / proficiency
    classes = d.get("classes") or []
    level = sum(c.get("level", 0) for c in classes) or 1
    prof = math.ceil(level / 4) + 1

    # ---- ability scores
    scores = {}
    for i, ab in enumerate(ABILITIES, 1):
        base = next((s["value"] for s in d.get("stats", []) if s["id"] == i), 10) or 10
        bonus = next((s["value"] for s in d.get("bonusStats", []) if s["id"] == i), 0) or 0
        override = next((s["value"] for s in d.get("overrideStats", []) if s["id"] == i), None)
        slug = [k for k, v in SCORE_SLUG.items() if v == ab][0]
        score = base + bonus + total_bonus(slug)
        for m in find("set", slug):                       # e.g. Gauntlets of Ogre Power
            score = max(score, m.get("value") or 0)
        scores[ab] = override if override else score
    mods_by = {ab: mod(v) for ab, v in scores.items()}

    # ---- hit points
    hp_per_level = total_bonus("hit-points-per-level")
    hp_max = (d.get("baseHitPoints") or 0) + mods_by["con"] * level + hp_per_level * level \
        + (d.get("bonusHitPoints") or 0)
    if d.get("overrideHitPoints"):
        hp_max = d["overrideHitPoints"]
    hp_now = hp_max - (d.get("removedHitPoints") or 0)

    # ---- armor class (approximate)
    equipped = [i for i in d.get("inventory", []) if i.get("equipped")]
    armor = [i for i in equipped if i["definition"].get("armorTypeId") in (ARMOR_LIGHT, ARMOR_MEDIUM, ARMOR_HEAVY)]
    shield = [i for i in equipped if i["definition"].get("armorTypeId") == ARMOR_SHIELD]
    ac_note = []
    if armor:
        a = max(armor, key=lambda i: i["definition"].get("armorClass") or 0)
        base_ac = a["definition"].get("armorClass") or 10
        t = a["definition"]["armorTypeId"]
        dex_part = mods_by["dex"] if t == ARMOR_LIGHT else min(mods_by["dex"], 2) if t == ARMOR_MEDIUM else 0
        ac = base_ac + dex_part + total_bonus("armored-armor-class")
        ac_note.append(a["definition"]["name"])
    else:
        ac = 10 + mods_by["dex"]
        for m in find("set", "unarmored-armor-class"):   # Barbarian / Monk Unarmored Defense
            stat = m.get("statId")
            extra = mods_by[ABILITIES[stat - 1]] if stat else 0
            ac = max(ac, 10 + mods_by["dex"] + extra + (m.get("value") or 0))
            ac_note.append("Unarmored Defense")
        ac += total_bonus("unarmored-armor-class")
    if shield:
        ac += shield[0]["definition"].get("armorClass") or 2
        ac_note.append("shield")
    ac += total_bonus("armor-class")

    # ---- proficiencies
    prof_subs = {m["subType"] for m in find("proficiency")}
    expert_subs = {m["subType"] for m in find("expertise")}
    half_subs = {m["subType"] for m in find("half-proficiency")}

    def prof_bonus_for(sub: str) -> int:
        if sub in expert_subs:
            return prof * 2
        if sub in prof_subs:
            return prof
        if sub in half_subs or "ability-checks" in half_subs:
            return prof // 2
        return 0

    skills = {}
    for sk, ab in SKILLS.items():
        b = mods_by[ab] + prof_bonus_for(sk) + total_bonus(sk)
        skills[sk] = {"bonus": b, "ability": ab,
                      "prof": "expertise" if sk in expert_subs else "proficient" if sk in prof_subs else ""}
    saves = {}
    for ab in ABILITIES:
        sub = f"{ABILITY_NAMES[ab].lower()}-saving-throws"
        saves[ab] = {"bonus": mods_by[ab] + (prof if sub in prof_subs else 0) + total_bonus(sub),
                     "prof": sub in prof_subs}

    other_profs = sorted({m.get("friendlySubtypeName") for m in find("proficiency")
                          if m["subType"] not in SKILLS and not m["subType"].endswith("saving-throws")
                          and m.get("friendlySubtypeName")})
    languages = sorted({m.get("friendlySubtypeName") for m in find("language") if m.get("friendlySubtypeName")})
    senses = [f"{m.get('friendlySubtypeName')} {m.get('value')} ft." for m in find("set-base")
              if m.get("value") and "vision" in (m.get("subType") or "") or m.get("subType") in ("tremorsense", "blindsight", "truesight")]
    defenses = {k: sorted({m.get("friendlySubtypeName") for m in find(k) if m.get("friendlySubtypeName")})
                for k in ("resistance", "immunity", "vulnerability", "advantage")}

    speeds = ((d.get("race") or {}).get("weightSpeeds") or {}).get("normal") or {}
    walk = speeds.get("walk") or 30
    for m in find("set", "innate-speed-walking"):          # e.g. Wood Elf 35 ft.
        walk = max(walk, m.get("value") or 0)
    walk += total_bonus("speed") + total_bonus("speed-walking") + total_bonus("unarmored-movement") * (not armor)
    heavy = [i for i in armor if i["definition"].get("armorTypeId") == ARMOR_HEAVY]
    if heavy and scores["str"] < (heavy[0]["definition"].get("strengthRequirement") or 0):
        walk -= 10                                            # too weak for the heavy armor

    # ---- classes, spellcasting
    class_list, casting = [], []
    for c in classes:
        cdef = c.get("definition") or {}
        sub = (c.get("subclassDefinition") or {}).get("name")
        class_list.append({"name": cdef.get("name"), "level": c.get("level"), "subclass": sub,
                           "hit_die": cdef.get("hitDice")})
        ab_id = cdef.get("spellCastingAbilityId") or (c.get("subclassDefinition") or {}).get("spellCastingAbilityId")
        if ab_id:
            ab = ABILITIES[ab_id - 1]
            casting.append({"class": cdef.get("name"), "ability": ab,
                            "save_dc": 8 + prof + mods_by[ab] + total_bonus("spell-save-dc"),
                            "attack": prof + mods_by[ab] + total_bonus("spell-attacks")})

    spells = []
    class_names = {c["id"]: (c.get("definition") or {}).get("name") for c in classes}
    for cs in d.get("classSpells") or []:
        src = class_names.get(cs.get("characterClassId"), "Class")
        for s in cs.get("spells") or []:
            sd = s.get("definition") or {}
            lvl = sd.get("level", 0)
            spells.append({"name": sd.get("name"), "level": lvl, "source": src,
                           "prepared": bool(s.get("prepared") or s.get("alwaysPrepared") or lvl == 0)})
    for group, lst in (d.get("spells") or {}).items():
        for s in lst or []:
            sd = s.get("definition") or {}
            spells.append({"name": sd.get("name"), "level": sd.get("level", 0),
                           "source": group.title(), "prepared": True})
    seen, uniq = set(), []
    for s in sorted(spells, key=lambda s: (s["level"], s["name"] or "")):
        if s["name"] and (s["name"], s["source"]) not in seen:
            seen.add((s["name"], s["source"])); uniq.append(s)

    # ---- features
    features = []
    for c in classes:
        for f in c.get("classFeatures") or []:
            fd = f.get("definition") or {}
            if (fd.get("requiredLevel") or 1) <= c.get("level", 1) and fd.get("name"):
                features.append({"name": fd["name"], "source": (c.get("definition") or {}).get("name"),
                                 "text": strip_html(fd.get("snippet") or fd.get("description"), 300)})
    for t in (d.get("race") or {}).get("racialTraits") or []:
        td = t.get("definition") or {}
        if td.get("name") and not td.get("hideInSheet"):
            features.append({"name": td["name"], "source": (d.get("race") or {}).get("fullName"),
                             "text": strip_html(td.get("snippet") or td.get("description"), 300)})
    for f in d.get("feats") or []:
        fd = f.get("definition") or {}
        if fd.get("name"):
            features.append({"name": fd["name"], "source": "Feat",
                             "text": strip_html(fd.get("snippet") or fd.get("description"), 300)})
    seen_f, feats_uniq = set(), []
    for f in features:
        if f["name"] not in seen_f and f["name"] not in ("Hit Points", "Proficiencies", "Ability Score Improvement"):
            seen_f.add(f["name"]); feats_uniq.append(f)

    inventory = [{"name": i["definition"].get("name"), "qty": i.get("quantity", 1),
                  "equipped": bool(i.get("equipped")), "attuned": bool(i.get("isAttuned"))}
                 for i in d.get("inventory", []) if (i.get("definition") or {}).get("name")]

    bg_raw = d.get("background") or {}
    bg = bg_raw.get("definition") or {}
    if not bg.get("name") and bg_raw.get("hasCustomBackground"):
        # Custom backgrounds keep the real names under features/characteristics backgrounds;
        # the custom "name" is often just the character's name.
        cb = bg_raw.get("customBackground") or {}
        parts = [cb.get("name") if cb.get("name") and cb.get("name") != d.get("name") else None,
                 (cb.get("featuresBackground") or {}).get("name"),
                 (cb.get("characteristicsBackground") or {}).get("name")]
        names = list(dict.fromkeys(p for p in parts if p))
        bg = {"name": " / ".join(names) or "Custom"}
    traits = {k: strip_html(v) for k, v in (d.get("traits") or {}).items() if v}
    notes = {k: strip_html(v) for k, v in (d.get("notes") or {}).items() if v}
    race = (d.get("race") or {}).get("fullName") or ""
    cls_text = " / ".join(f"{c['name']}{' (' + c['subclass'] + ')' if c['subclass'] else ''} {c['level']}"
                          for c in class_list)

    return {
        "ddb_id": d.get("id"), "name": d.get("name"), "player": d.get("username") or "",
        "avatar": (d.get("decorations") or {}).get("avatarUrl"),
        "race": race, "classes": class_list, "class_text": cls_text, "level": level,
        "summary": f"Level {level} {race} {cls_text}".strip(),
        "background": bg.get("name"), "alignment": ALIGNMENTS.get(d.get("alignmentId")),
        "abilities": {ab: {"score": scores[ab], "mod": mods_by[ab]} for ab in ABILITIES},
        "proficiency_bonus": prof,
        "hp": {"max": hp_max, "current": hp_now, "temp": d.get("temporaryHitPoints") or 0},
        "ac": ac, "ac_note": ", ".join(ac_note), "speed": walk,
        "initiative": mods_by["dex"] + total_bonus("initiative"),
        "passive_perception": 10 + skills["perception"]["bonus"],
        "saves": saves, "skills": skills, "senses": senses, "languages": languages,
        "other_proficiencies": other_profs, "defenses": defenses,
        "spellcasting": casting, "spells": uniq, "features": feats_uniq, "inventory": inventory,
        "currencies": d.get("currencies") or {}, "inspiration": bool(d.get("inspiration")),
        "conditions": [c.get("id") for c in d.get("conditions") or []],
        "traits": traits, "notes": notes,
        "modified": d.get("dateModified"),
    }


def sheet_text(s: dict) -> str:
    """Player-safe Markdown summary used for search and the AI."""
    ab = " · ".join(f"{a.upper()} {v['score']} ({signed(v['mod'])})" for a, v in s["abilities"].items())
    saves = ", ".join(f"{a.upper()} {signed(v['bonus'])}" for a, v in s["saves"].items() if v["prof"])
    skills = ", ".join(f"{k.replace('-', ' ').title()} {signed(v['bonus'])}{'*' if v['prof'] == 'expertise' else ''}"
                       for k, v in s["skills"].items() if v["prof"])
    lines = [
        f"## {s['name']} — {s['summary']}",
        f"Player: {s['player']}. Background: {s['background'] or '—'}. Alignment: {s['alignment'] or '—'}.",
        f"**AC** {s['ac']} (approx.{'; ' + s['ac_note'] if s['ac_note'] else ''}) · **HP** {s['hp']['max']} max "
        f"· **Speed** {s['speed']} ft. · **Initiative** {signed(s['initiative'])} · "
        f"**Proficiency bonus** {signed(s['proficiency_bonus'])} · **Passive Perception** {s['passive_perception']}",
        f"**Abilities:** {ab}",
        f"**Saving throw proficiencies:** {saves or 'none'}",
        f"**Skill proficiencies:** {skills or 'none'}",
    ]
    if s["senses"]:
        lines.append(f"**Senses:** {', '.join(s['senses'])}")
    if s["languages"]:
        lines.append(f"**Languages:** {', '.join(s['languages'])}")
    if s["other_proficiencies"]:
        lines.append(f"**Other proficiencies:** {', '.join(s['other_proficiencies'])}")
    for k, v in s["defenses"].items():
        if v:
            lines.append(f"**{k.title()}:** {', '.join(v)}")
    for c in s["spellcasting"]:
        lines.append(f"**Spellcasting ({c['class']}):** {ABILITY_NAMES[c['ability']]}, "
                     f"spell save DC {c['save_dc']}, spell attack {signed(c['attack'])}")
    if s["spells"]:
        by_level: dict[int, list[str]] = {}
        for sp in s["spells"]:
            by_level.setdefault(sp["level"], []).append(sp["name"] + ("" if sp["prepared"] else " (not prepared)"))
        for lvl, names in sorted(by_level.items()):
            lines.append(f"**{'Cantrips' if lvl == 0 else f'Level {lvl} spells'}:** {', '.join(names)}")
    if s["features"]:
        lines.append("**Features and traits:**")
        lines += [f"- **{f['name']}** ({f['source']}): {f['text']}" for f in s["features"]]
    eq = [f"{i['name']}{' ×' + str(i['qty']) if i['qty'] and i['qty'] > 1 else ''}{' (equipped)' if i['equipped'] else ''}"
          for i in s["inventory"]]
    if eq:
        lines.append(f"**Equipment:** {', '.join(eq)}")
    coins = ", ".join(f"{v} {k}" for k, v in s["currencies"].items() if v)
    if coins:
        lines.append(f"**Coins:** {coins}")
    if s["traits"]:
        lines += [f"**{k.replace('personalityTraits', 'Personality').title()}:** {v}" for k, v in s["traits"].items()]
    return "\n".join(lines)


def private_text(s: dict) -> str:
    """Backstory and personal notes: DM search only."""
    if not s["notes"]:
        return ""
    labels = {"backstory": "Backstory", "allies": "Allies", "enemies": "Enemies",
              "organizations": "Organizations", "personalPossessions": "Personal possessions",
              "otherHoldings": "Other holdings", "otherNotes": "Other notes"}
    body = "\n\n".join(f"**{labels.get(k, k)}:** {v}" for k, v in s["notes"].items())
    return f"## {s['name']} — backstory and notes\n\n{body}"
