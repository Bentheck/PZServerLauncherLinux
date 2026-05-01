from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from app.config import Settings
from app.models import ServerProfile

DEFAULT_WORLD_ITEM_REMOVAL_LIST = (
    "Base.Hat, Base.Glasses, Base.Maggots, Base.Slug, Base.Slug2, Base.Snail, "
    "Base.Worm, Base.Dung_Mouse, Base.Dung_Rat"
)
BUILT_IN_PRESET_ORDER = (
    "Apocalypse",
    "Outbreak",
    "Rising",
    "Extinction",
    "SixMonthsLater",
)


@dataclass(frozen=True, slots=True)
class SandboxOption:
    value: str
    label: str


@dataclass(frozen=True, slots=True)
class SandboxFieldDefinition:
    name: str
    label: str
    key_path: tuple[str, ...]
    field_type: str
    default: str
    options: tuple[SandboxOption, ...] = ()


@dataclass(frozen=True, slots=True)
class SandboxSectionDefinition:
    id: str
    title: str
    category_title: str
    category_order: int
    description: str
    fields: tuple[SandboxFieldDefinition, ...]


@dataclass(slots=True)
class SandboxPresetView:
    id: str
    name: str
    is_builtin: bool
    values: dict[str, str]

    @property
    def display_label(self) -> str:
        return f"{self.name} ({'Shipped' if self.is_builtin else 'Custom'})"


@dataclass(slots=True)
class SandboxFieldView:
    definition: SandboxFieldDefinition
    current_value: str
    preset_value: str | None
    has_preset_value: bool
    matches_preset: bool


@dataclass(slots=True)
class SandboxSectionView:
    definition: SandboxSectionDefinition
    fields: list[SandboxFieldView]
    compared_field_count: int
    matching_field_count: int


@dataclass(slots=True)
class SandboxCategoryView:
    id: str
    title: str
    order: int
    sections: list[SandboxSectionView]
    status_text: str
    matches_preset: bool
    compared_field_count: int
    matching_field_count: int


@dataclass(slots=True)
class SandboxLine:
    kind: str
    raw: str = ""
    indent: str = ""
    section_path: tuple[str, ...] = ()
    key: str = ""
    value: str = ""
    modified: bool = False

    @property
    def entry_path(self) -> tuple[str, ...]:
        return (*self.section_path, self.key)


def _options(*pairs: tuple[str, str]) -> tuple[SandboxOption, ...]:
    return tuple(SandboxOption(value=value, label=label) for value, label in pairs)


def _numbered_options(*labels: str) -> tuple[SandboxOption, ...]:
    return tuple(SandboxOption(value=str(index), label=label) for index, label in enumerate(labels, start=1))


DAY_LENGTH_OPTIONS = _options(
    ("1", "15 Minutes"),
    ("2", "30 Minutes"),
    ("3", "1 Hour"),
    ("4", "1 Hour, 30 Minutes"),
    ("5", "2 Hours"),
    ("6", "3 Hours"),
    ("7", "4 Hours"),
    ("8", "5 Hours"),
    ("9", "6 Hours"),
    ("10", "7 Hours"),
    ("11", "8 Hours"),
    ("12", "9 Hours"),
    ("13", "10 Hours"),
    ("14", "11 Hours"),
    ("15", "12 Hours"),
    ("16", "13 Hours"),
    ("17", "14 Hours"),
    ("18", "15 Hours"),
    ("19", "16 Hours"),
    ("20", "17 Hours"),
    ("21", "18 Hours"),
    ("22", "19 Hours"),
    ("23", "20 Hours"),
    ("24", "21 Hours"),
    ("25", "22 Hours"),
    ("26", "23 Hours"),
    ("27", "Real-time"),
)
TIME_SINCE_APOCALYPSE_OPTIONS = tuple(SandboxOption(value=str(index), label=str(index - 1)) for index in range(1, 14))
MONTH_OPTIONS = _numbered_options(
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
START_TIME_OPTIONS = _numbered_options("7 AM", "9 AM", "12 PM", "2 PM", "5 PM", "9 PM", "12 AM", "2 AM", "5 AM")
ZOMBIE_COUNT_OPTIONS = _options(
    ("1", "Insane"),
    ("2", "Very High"),
    ("3", "High"),
    ("4", "Normal"),
    ("5", "Low"),
    ("6", "None"),
)
CRAWL_UNDER_VEHICLE_OPTIONS = _options(
    ("1", "Crawlers Only"),
    ("2", "Extremely Rare"),
    ("3", "Rare"),
    ("4", "Sometimes"),
    ("5", "Often"),
    ("6", "Very Often"),
    ("7", "Always"),
)
ZOMBIE_POPULATION_OPTIONS = _options(
    ("2.5", "Insane"),
    ("1.6", "Very High"),
    ("1.2", "High"),
    ("0.65", "Normal"),
    ("0.15", "Low"),
    ("0.0", "None"),
)
ZOMBIE_POPULATION_START_OPTIONS = _options(
    ("3.0", "Insane"),
    ("2.0", "Very High"),
    ("1.5", "High"),
    ("1.0", "Normal"),
    ("0.5", "Low"),
    ("0.0", "None"),
)
GENERATOR_SPAWNING_OPTIONS = _options(
    ("1", "None"),
    ("2", "Insanely Rare"),
    ("3", "Extremely Rare"),
    ("4", "Rare"),
    ("5", "Normal"),
    ("6", "Common"),
    ("7", "Abundant"),
)
WATER_SHUTOFF_OPTIONS = _options(
    ("1", "Instant"),
    ("2", "0 - 30 Days"),
    ("3", "0 - 2 Months"),
    ("4", "0 - 6 Months"),
    ("5", "0 - 1 Year"),
    ("6", "0 - 5 Years"),
    ("7", "2 - 6 Months"),
    ("8", "6 - 12 Months"),
    ("9", "Disabled"),
)
ELECTRICITY_SHUTOFF_OPTIONS = _options(
    ("1", "Instant"),
    ("2", "14 - 30 Days"),
    ("3", "14 Days - 2 Months"),
    ("4", "14 Days - 6 Months"),
    ("5", "14 Days - 1 Year"),
    ("6", "14 Days - 5 Years"),
    ("7", "2 - 6 Months"),
    ("8", "6 - 12 Months"),
    ("9", "Disabled"),
)
ALARM_DECAY_OPTIONS = _options(
    ("1", "Instant"),
    ("2", "0 - 30 Days"),
    ("3", "0 - 2 Months"),
    ("4", "0 - 6 Months"),
    ("5", "0 - 1 Year"),
    ("6", "0 - 5 Years"),
)
FREQUENCY_OPTIONS = _options(
    ("1", "Never"),
    ("2", "Extremely Rare"),
    ("3", "Rare"),
    ("4", "Sometimes"),
    ("5", "Often"),
    ("6", "Very Often"),
)
FREQUENCY_WITH_ALWAYS_OPTIONS = _options(
    ("1", "Never"),
    ("2", "Extremely Rare"),
    ("3", "Rare"),
    ("4", "Sometimes"),
    ("5", "Often"),
    ("6", "Very Often"),
    ("7", "Always"),
)
STORY_CHANCE_OPTIONS = _options(
    ("1", "Never"),
    ("2", "Extremely Rare"),
    ("3", "Rare"),
    ("4", "Sometimes"),
    ("5", "Often"),
    ("6", "Very Often"),
    ("7", "Always Tries"),
)
ANIMAL_SPEED_OPTIONS = _options(
    ("1", "Ultra Fast"),
    ("2", "Very Fast"),
    ("3", "Fast"),
    ("4", "Normal"),
    ("5", "Slow"),
    ("6", "Very Slow"),
)
VERY_FAST_TO_VERY_SLOW_OPTIONS = _numbered_options("Very Fast", "Fast", "Normal", "Slow", "Very Slow")
VERY_LOW_TO_VERY_HIGH_OPTIONS = _numbered_options("Very Low", "Low", "Normal", "High", "Very High")
LOW_NORMAL_HIGH_OPTIONS = _numbered_options("Low", "Normal", "High")
NEVER_SOMETIMES_OFTEN_OPTIONS = _options(("1", "Never"), ("2", "Sometimes"), ("3", "Often"))
NONE_LOW_NORMAL_HIGH_OPTIONS = _options(("1", "None"), ("2", "Low"), ("3", "Normal"), ("4", "High"))
NONE_LOW_NORMAL_HIGH_VERY_HIGH_OPTIONS = _options(
    ("1", "None"),
    ("2", "Low"),
    ("3", "Normal"),
    ("4", "High"),
    ("5", "Very High"),
)
TEMPERATURE_OPTIONS = _numbered_options("Very Cold", "Cold", "Normal", "Hot", "Very Hot")
RAIN_OPTIONS = _numbered_options("Very Dry", "Dry", "Normal", "Rainy", "Very Rainy")
ABUNDANCE_OPTIONS = _numbered_options("Very Poor", "Poor", "Normal", "Abundant", "Very Abundant")


def _clean_default(default_source: str) -> str:
    if default_source == "string.Empty":
        return ""
    if default_source == "DefaultWorldItemRemovalList":
        return DEFAULT_WORLD_ITEM_REMOVAL_LIST
    if len(default_source) >= 2 and default_source.startswith('"') and default_source.endswith('"'):
        return default_source[1:-1]
    return default_source


def _resolve_select_default(default_source: str, options: tuple[SandboxOption, ...]) -> str:
    cleaned = _clean_default(default_source)
    for option in options:
        if option.value == cleaned or option.label == cleaned:
            return option.value
    return options[0].value if options else cleaned


def _field(
    slug: str,
    label: str,
    key_path: str,
    field_type: str,
    default_source: str,
    *,
    options: tuple[SandboxOption, ...] = (),
) -> SandboxFieldDefinition:
    default_value = _resolve_select_default(default_source, options) if field_type == "select" else _clean_default(default_source)
    return SandboxFieldDefinition(
        name=slug.replace("-", "_"),
        label=label,
        key_path=tuple(key_path.split(".")),
        field_type=field_type,
        default=default_value,
        options=options,
    )


def _choice_field(slug: str, label: str, key_path: str, default_source: str, options: tuple[SandboxOption, ...]) -> SandboxFieldDefinition:
    return _field(slug, label, key_path, "select", default_source, options=options)


def _int_field(slug: str, label: str, key_path: str, default_source: str) -> SandboxFieldDefinition:
    return _field(slug, label, key_path, "integer", default_source)


def _bool_field(slug: str, label: str, key_path: str, default_source: str) -> SandboxFieldDefinition:
    return _field(slug, label, key_path, "checkbox", default_source)


def _decimal_field(slug: str, label: str, key_path: str, default_source: str) -> SandboxFieldDefinition:
    return _field(slug, label, key_path, "decimal", default_source)


def _textarea_field(slug: str, label: str, key_path: str, default_source: str) -> SandboxFieldDefinition:
    return _field(slug, label, key_path, "textarea", default_source)


def _section(
    section_id: str,
    title: str,
    category_title: str,
    category_order: int,
    description: str,
    *fields: SandboxFieldDefinition,
) -> SandboxSectionDefinition:
    return SandboxSectionDefinition(
        id=section_id,
        title=title,
        category_title=category_title,
        category_order=category_order,
        description=description,
        fields=tuple(fields),
    )


SANDBOX_SECTIONS: tuple[SandboxSectionDefinition, ...] = (
    _section(
        "time.setup",
        "Time",
        "Time",
        1,
        "Start timeline and world pacing.",
        _choice_field("day-length", "Day Length (in real time)", "DayLength", '"1 Hour, 30 Minutes"', DAY_LENGTH_OPTIONS),
        _choice_field("time-since-apo", "Months since the Apocalypse", "TimeSinceApo", '"0"', TIME_SINCE_APOCALYPSE_OPTIONS),
        _choice_field("start-month", "Start Month", "StartMonth", '"July"', MONTH_OPTIONS),
        _int_field("start-day", "Start Day", "StartDay", '"9"'),
        _choice_field("start-time", "Start Hour", "StartTime", '"9 AM"', START_TIME_OPTIONS),
    ),
    _section(
        "zombie.basics",
        "Zombie",
        "Zombie",
        2,
        "Population, distribution, and baseline world pressure.",
        _choice_field("zombies", "Zombie Count", "Zombies", '"Normal"', ZOMBIE_COUNT_OPTIONS),
        _choice_field("distribution", "Zombie Distribution", "Distribution", '"Urban Focused"', _numbered_options("Urban Focused", "Uniform")),
        _bool_field("voronoi-noise", "Voronoi Noise", "ZombieVoronoiNoise", '"true"'),
        _choice_field("zombie-respawn", "Zombie Respawn", "ZombieRespawn", '"None"', _options(("1", "High"), ("2", "Normal"), ("3", "Low"), ("4", "None"))),
        _bool_field("zombie-migration", "Zombie Migration", "ZombieMigrate", '"true"'),
    ),
    _section(
        "zombie.lore",
        "Zombie Lore",
        "Zombie",
        2,
        "Behavior, infection, and special zombie rules.",
        _choice_field("zombie-lore-speed", "Speed", "ZombieLore.Speed", '"Random"', _options(("1", "Sprinters"), ("2", "Fast Shamblers"), ("3", "Shamblers"), ("4", "Random"))),
        _int_field("random-sprinter-amount", "Random Sprinter Amount (%)", "ZombieLore.SprinterPercentage", '"0"'),
        _choice_field("zombie-lore-strength", "Strength", "ZombieLore.Strength", '"Normal"', _options(("1", "Superhuman"), ("2", "Normal"), ("3", "Weak"), ("4", "Random"))),
        _choice_field("zombie-lore-toughness", "Toughness", "ZombieLore.Toughness", '"Random"', _options(("1", "Tough"), ("2", "Normal"), ("3", "Fragile"), ("4", "Random"))),
        _choice_field("zombie-lore-transmission", "Transmission", "ZombieLore.Transmission", '"Blood and Saliva"', _options(("1", "Blood and Saliva"), ("2", "Saliva Only"), ("3", "Everyone\'s Infected"), ("4", "None"))),
        _choice_field("zombie-lore-mortality", "Infection Mortality", "ZombieLore.Mortality", '"2-3 Days"', _options(("1", "Instant"), ("2", "0-30 Seconds"), ("3", "0-1 Minutes"), ("4", "0-12 Hours"), ("5", "2-3 Days"), ("6", "1-2 Weeks"), ("7", "Never"))),
        _choice_field("zombie-lore-reanimate", "Reanimate Time", "ZombieLore.Reanimate", '"0-1 Minutes"', _options(("1", "Instant"), ("2", "0-30 Seconds"), ("3", "0-1 Minutes"), ("4", "0-12 Hours"), ("5", "2-3 Days"), ("6", "1-2 Weeks"))),
        _choice_field("zombie-lore-cognition", "Cognition", "ZombieLore.Cognition", '"Basic Navigation"', _options(("1", "Navigate and Use Doors"), ("2", "Navigate"), ("3", "Basic Navigation"), ("4", "Random"))),
        _int_field("random-door-opening-amount", "Random Door Opening Amount (%)", "ZombieLore.DoorOpeningPercentage", '"0"'),
        _choice_field("crawl-under-vehicle", "Crawl Under Vehicle", "ZombieLore.CrawlUnderVehicle", '"Often"', CRAWL_UNDER_VEHICLE_OPTIONS),
        _choice_field("zombie-lore-memory", "Memory", "ZombieLore.Memory", '"Normal"', _options(("1", "Long"), ("2", "Normal"), ("3", "Short"), ("4", "None"), ("5", "Random"), ("6", "Random between Normal and None"))),
        _choice_field("zombie-lore-sight", "Sight", "ZombieLore.Sight", '"Random between Normal and Poor"', _options(("1", "Eagle"), ("2", "Normal"), ("3", "Poor"), ("4", "Random"), ("5", "Random between Normal and Poor"))),
        _choice_field("zombie-lore-hearing", "Hearing", "ZombieLore.Hearing", '"Random between Normal and Poor"', _options(("1", "Pinpoint"), ("2", "Normal"), ("3", "Poor"), ("4", "Random"), ("5", "Random between Normal and Poor"))),
        _bool_field("new-stealth-system", "New Stealth System", "ZombieLore.SpottedLogic", '"true"'),
        _bool_field("environmental-attacks", "Environmental Attacks", "ZombieLore.ThumpNoChasing", '"false"'),
        _bool_field("damage-construction", "Damage Construction", "ZombieLore.ThumpOnConstruction", '"true"'),
        _choice_field("day-night-zombie-speed-effect", "Day/Night Zombie Speed Effect", "ZombieLore.ActiveOnly", '"Both"', _options(("1", "Both"), ("2", "Night"), ("3", "Day"))),
        _bool_field("zombie-house-alarm-triggering", "Zombie House Alarm Triggering", "ZombieLore.TriggerHouseAlarm", '"true"'),
        _bool_field("drag-down", "Drag Down", "ZombieLore.ZombiesDragDown", '"true"'),
        _bool_field("crawlers-drag-down", "Crawlers Drag Down", "ZombieLore.ZombiesCrawlersDragDown", '"false"'),
        _bool_field("zombie-lunge", "Zombie Lunge", "ZombieLore.ZombiesFenceLunge", '"true"'),
        _choice_field("fake-dead-zombie-reanimation", "Fake Dead Zombie Reanimation", "ZombieLore.DisableFakeDead", '"World Zombies"', _options(("1", "World Zombies"), ("2", "World and Combat Zombies"), ("3", "Never"))),
        _decimal_field("zombie-armor-factor", "Zombie Armor Factor", "ZombieLore.ZombiesArmorFactor", '"2.0"'),
        _int_field("maximum-zombie-armor-defense", "Maximum Zombie Armor Defense", "ZombieLore.ZombiesMaxDefense", '"85"'),
        _int_field("chance-of-attached-weapon", "Chance Of Attached Weapon", "ZombieLore.ChanceOfAttachedWeapon", '"6"'),
        _decimal_field("zombie-fall-damage-multiplier", "Zombie Fall Damage Multiplier", "ZombieLore.ZombiesFallDamage", '"1.0"'),
        _choice_field("player-spawn-area", "Player Spawn Area", "ZombieLore.PlayerSpawnZombieRemoval", '"Inside the building and around it"', _options(("1", "Inside the building and around it"), ("2", "Inside the building"), ("3", "Inside the room"), ("4", "Zombies can spawn anywhere"))),
    ),
    _section(
        "zombie.advanced",
        "Advanced zombie settings",
        "Zombie",
        2,
        "Population curves and rally behavior.",
        _choice_field("population-multiplier", "Population Multiplier", "ZombieConfig.PopulationMultiplier", '"Normal"', ZOMBIE_POPULATION_OPTIONS),
        _choice_field("population-start-multiplier", "Population Start Multiplier", "ZombieConfig.PopulationStartMultiplier", '"Normal"', ZOMBIE_POPULATION_START_OPTIONS),
        _choice_field("population-peak-multiplier", "Population Peak Multiplier", "ZombieConfig.PopulationPeakMultiplier", '"High"', ZOMBIE_POPULATION_START_OPTIONS),
        _int_field("population-peak-day", "Population Peak Day", "ZombieConfig.PopulationPeakDay", '"28"'),
        _decimal_field("respawn-hours", "Respawn Hours", "ZombieConfig.RespawnHours", '"0.0"'),
        _decimal_field("respawn-unseen-hours", "Respawn Unseen Hours", "ZombieConfig.RespawnUnseenHours", '"0.0"'),
        _decimal_field("respawn-multiplier", "Respawn Multiplier", "ZombieConfig.RespawnMultiplier", '"0.0"'),
        _decimal_field("redistribute-hours", "Redistribute Hours", "ZombieConfig.RedistributeHours", '"12.0"'),
        _int_field("follow-sound-distance", "Follow Sound Distance", "ZombieConfig.FollowSoundDistance", '"100"'),
        _int_field("rally-group-size", "Rally Group Size", "ZombieConfig.RallyGroupSize", '"20"'),
        _int_field("rally-group-size-variance", "Rally Group Size Variance", "ZombieConfig.RallyGroupSizeVariance", '"50"'),
        _int_field("rally-travel-distance", "Rally Travel Distance", "ZombieConfig.RallyTravelDistance", '"20"'),
        _int_field("rally-group-separation", "Rally Group Separation", "ZombieConfig.RallyGroupSeparation", '"15"'),
        _int_field("rally-group-radius", "Rally Group Radius", "ZombieConfig.RallyGroupRadius", '"3"'),
        _int_field("zombie-count-before-deletion", "Zombie count before deletion", "ZombieConfig.ZombiesCountBeforeDelete", '"300"'),
    ),
    _section(
        "loot.world",
        "Loot",
        "Loot",
        3,
        "Respawn cadence and global world-loot pressure.",
        _int_field("hours-for-loot-respawn", "Hours for Loot Respawn", "HoursForLootRespawn", '"0"'),
        _int_field("loot-seen-prevent-hours", "Loot Seen Prevent Hours", "SeenHoursPreventLootRespawn", '"0"'),
        _int_field("max-items-for-loot-respawn", "Max Items For Loot Respawn", "MaxItemsForLootRespawn", '"5"'),
        _bool_field("construction-prevents-loot-respawn", "Construction Prevents Loot Respawn", "ConstructionPreventsLootRespawn", '"true"'),
        _int_field("maximum-looted-building-chance", "Maximum Looted Building Chance", "MaximumLooted", '"25"'),
        _int_field("days-until-max-looted-building-chance", "Days Until Max Looted Building Chance", "DaysUntilMaximumLooted", '"90"'),
        _decimal_field("rural-building-looted-chance-multiplier", "Rural Building Looted Chance Multiplier", "RuralLooted", '"0.5"'),
        _int_field("maximum-diminished-loot-percentage", "Maximum Diminished Loot Percentage", "MaximumDiminishedLoot", '"20"'),
        _int_field("days-until-maximum-diminished-loot", "Days Until Maximum Diminished Loot", "DaysUntilMaximumDiminishedLoot", '"3650"'),
        _int_field("maximum-looted-building-rooms", "Maximum Looted Building Rooms", "MaximumLootedBuildingRooms", '"50"'),
    ),
    _section(
        "loot.rarity",
        "Loot rarity",
        "Loot",
        3,
        "Category-specific loot values and cleanup toggles.",
        _decimal_field("perishable-food-loot", "Perishable Food", "FoodLootNew", '"0.8"'),
        _decimal_field("non-perishable-food-loot", "Non-Perishable Food", "CannedFoodLootNew", '"0.6"'),
        _decimal_field("melee-weapons-loot", "Melee Weapons", "WeaponLootNew", '"0.6"'),
        _decimal_field("ranged-weapons-loot", "Ranged Weapons", "RangedWeaponLootNew", '"1.2"'),
        _decimal_field("ammo-loot", "Ammo", "AmmoLootNew", '"0.6"'),
        _decimal_field("medical-loot", "Medical", "MedicalLootNew", '"0.6"'),
        _decimal_field("survival-essentials-loot", "Survival Essentials", "SurvivalGearsLootNew", '"0.6"'),
        _decimal_field("mechanics-loot", "Mechanics", "MechanicsLootNew", '"0.6"'),
        _decimal_field("skill-books-loot", "Skill Books", "SkillBookLoot", '"0.6"'),
        _decimal_field("recipe-resources-loot", "Recipe Resources", "RecipeResourceLoot", '"0.6"'),
        _decimal_field("other-literature-loot", "Other Literature", "LiteratureLootNew", '"0.6"'),
        _decimal_field("clothing-loot", "Clothing", "ClothingLootNew", '"0.6"'),
        _decimal_field("bags-loot", "Bags", "ContainerLootNew", '"0.6"'),
        _decimal_field("keys-loot", "Keys", "KeyLootNew", '"0.4"'),
        _decimal_field("media-loot", "Media", "MediaLootNew", '"0.6"'),
        _decimal_field("mementos-loot", "Mementos", "MementoLootNew", '"0.6"'),
        _decimal_field("cooking-loot", "Cooking", "CookwareLootNew", '"0.6"'),
        _decimal_field("material-loot", "Material", "MaterialLootNew", '"0.6"'),
        _decimal_field("farming-loot", "Farming", "FarmingLootNew", '"0.6"'),
        _decimal_field("tools-loot", "Tools", "ToolLootNew", '"0.6"'),
        _decimal_field("other-loot", "Other", "OtherLootNew", '"0.6"'),
        _choice_field("generators-loot", "Generators", "GeneratorSpawning", '"Rare"', GENERATOR_SPAWNING_OPTIONS),
        _textarea_field("loot-item-removal-list", "Loot Item Removal List", "LootItemRemovalList", "string.Empty"),
        _bool_field("remove-unwanted-story-loot", "Remove Unwanted Story Loot", "RemoveStoryLoot", '"false"'),
        _bool_field("remove-unwanted-zombie-loot", "Remove Unwanted Zombie Loot", "RemoveZombieLoot", '"false"'),
        _decimal_field("rolls-multiplier", "Rolls Multiplier [!]", "RollsMultiplier", '"1.0"'),
        _int_field("zombie-population-loot-effect", "Zombie Population Loot Effect", "ZombiePopLootEffect", '"0"'),
    ),
    _section(
        "world.core",
        "World",
        "World",
        4,
        "Utilities, alarms, generators, and decay systems.",
        _int_field("water-shut-modifier", "Water Shutoff", "WaterShutModifier", '"14"'),
        _int_field("electricity-shut-modifier", "Electricity Shutoff", "ElecShutModifier", '"14"'),
        _choice_field("water-shut", "Water Shutoff", "WaterShut", '"0 - 30 Days"', WATER_SHUTOFF_OPTIONS),
        _choice_field("electricity-shut", "Electricity Shutoff", "ElecShut", '"14 - 30 Days"', ELECTRICITY_SHUTOFF_OPTIONS),
        _choice_field("alarm-battery-decay", "Alarm Battery Decay", "AlarmDecay", '"0 - 30 Days"', ALARM_DECAY_OPTIONS),
        _choice_field("alarm", "House Alarms Frequency", "Alarm", '"Sometimes"', FREQUENCY_OPTIONS),
        _choice_field("locked-houses", "Locked Houses Frequency", "LockedHouses", '"Very Often"', FREQUENCY_OPTIONS),
        _bool_field("fire-spread", "Fire Spread", "FireSpread", '"true"'),
        _bool_field("allow-exterior-generator", "Generator Working in Exterior", "AllowExteriorGenerator", '"true"'),
        _int_field("generator-tile-range", "Generator tile range", "GeneratorTileRange", '"20"'),
        _int_field("generator-vertical-range", "Generator vertical range", "GeneratorVerticalPowerRange", '"3"'),
        _bool_field("infinite-gas-pumps", "Infinite Gas Pumps", "FuelStationGasInfinite", '"false"'),
        _decimal_field("initial-minimum-gas-pump-amount", "Initial Minimum Gas Pump Amount", "FuelStationGasMin", '"0.0"'),
        _decimal_field("initial-maximum-gas-pump-amount", "Initial Maximum Gas Pump Amount", "FuelStationGasMax", '"0.8"'),
        _int_field("initial-gas-pump-empty-chance", "Initial Gas Pump Empty Chance", "FuelStationGasEmptyChance", '"20"'),
        _decimal_field("light-bulb-lifespan", "Light Bulb Lifespan", "LightBulbLifespan", '"2.0"'),
        _choice_field("food-spoilage", "Food Spoilage", "FoodRotSpeed", '"Normal"', VERY_FAST_TO_VERY_SLOW_OPTIONS),
        _choice_field("refrigeration-effectiveness", "Refrigeration Effectiveness", "FridgeFactor", '"Normal"', _options(("1", "Very Low"), ("2", "Low"), ("3", "Normal"), ("4", "High"), ("5", "Very High"), ("6", "No decay"))),
        _int_field("rotten-food-removal", "Rotten Food Removal", "DaysForRottenFoodRemoval", '"-1"'),
        _textarea_field("world-item-removal-list", "World Item Removal List", "WorldItemRemovalList", "DefaultWorldItemRemovalList"),
        _decimal_field("hours-for-world-item-removal", "Hours for Removal List", "HoursForWorldItemRemoval", '"24.0"'),
        _bool_field("item-removal-list-whitelist-toggle", "Removal List as Whitelist", "ItemRemovalListBlacklistToggle", '"false"'),
    ),
    _section(
        "world.basements",
        "Basements",
        "World",
        4,
        "Basement spawn and fire-fuel rules.",
        _choice_field("basement-spawn-frequency", "Basement Spawn Frequency", "Basement.SpawnFrequency", '"Sometimes"', FREQUENCY_WITH_ALWAYS_OPTIONS),
        _int_field("maximum-fire-fuel-hours", "Maximum Fire Fuel Hours", "MaximumFireFuelHours", '"8"'),
    ),
    _section(
        "nature.core",
        "Nature",
        "Nature",
        5,
        "Climate, farming, water, vermin, and resources.",
        _choice_field("night-darkness", "Darkness during night", "NightDarkness", '"Normal"', _options(("1", "Pitch Black"), ("2", "Dark"), ("3", "Normal"), ("4", "Bright"))),
        _choice_field("temperature", "Temperature", "Temperature", '"Normal"', TEMPERATURE_OPTIONS),
        _choice_field("rain", "Rain", "Rain", '"Normal"', RAIN_OPTIONS),
        _choice_field("max-fog-intensity", "Maximum Fog Intensity", "MaxFogIntensity", '"Normal"', _options(("1", "Normal"), ("2", "Moderate"), ("3", "Low"), ("4", "None"))),
        _choice_field("max-rain-fx-intensity", "Maximum Rain FX Intensity", "MaxRainFxIntensity", '"Normal"', _options(("1", "Normal"), ("2", "Moderate"), ("3", "Low"))),
        _choice_field("erosion-speed", "Erosion Speed", "ErosionSpeed", '"Slow (200 Days)"', _options(("1", "Very Fast (20 Days)"), ("2", "Fast (50 Days)"), ("3", "Normal (100 Days)"), ("4", "Slow (200 Days)"), ("5", "Very Slow (500 Days)"))),
        _int_field("erosion-days", "Erosion Days", "ErosionDays", '"0"'),
        _decimal_field("farming", "Farming Speed", "FarmingSpeedNew", '"1.0"'),
        _choice_field("compost-time", "Compost Time", "CompostTime", '"2 Weeks"', _options(("1", "1 Week"), ("2", "2 Weeks"), ("3", "3 Weeks"), ("4", "4 Weeks"), ("5", "6 Weeks"), ("6", "8 Weeks"), ("7", "10 Weeks"), ("8", "12 Weeks"))),
        _choice_field("fishing-abundance", "Fishing Abundance", "FishAbundance", '"Poor"', ABUNDANCE_OPTIONS),
        _choice_field("nature-abundance", "Nature's Abundance", "NatureAbundance", '"Normal"', ABUNDANCE_OPTIONS),
        _choice_field("plant-resilience", "Plant Resilience", "PlantResilience", '"Normal"', _options(("1", "Very High"), ("2", "High"), ("3", "Normal"), ("4", "Low"), ("5", "Very Low"))),
        _decimal_field("plant-abundance", "Farming Abundance", "FarmingAmountNew", '"1.0"'),
        _bool_field("kill-crops-grown-inside", "Kill Crops Grown Inside", "KillInsideCrops", '"true"'),
        _bool_field("plant-growing-seasons", "Plant Growing Seasons", "PlantGrowingSeasons", '"true"'),
        _bool_field("farms-not-on-ground-level", "Farms not on Ground Level [!]", "PlaceDirtAboveground", '"false"'),
        _bool_field("enable-snow-on-ground", "Snow on Ground", "EnableSnowOnGround", '"true"'),
        _bool_field("enable-tainted-water-tooltip", "Enable 'Tainted Water' tooltip", "EnableTaintedWaterText", '"true"'),
        _int_field("maximum-vermin-index", "Maximum Vermin Index", "MaximumRatIndex", '"25"'),
        _int_field("days-until-maximum-vermin-index", "Days Until Maximum Vermin Index", "DaysUntilMaximumRatIndex", '"90"'),
        _decimal_field("clay-chance-lake", "Clay chance - Lake", "ClayLakeChance", '"0.05"'),
        _decimal_field("clay-chance-river", "Clay chance - River", "ClayRiverChance", '"0.05"'),
    ),
    _section(
        "meta.core",
        "Meta",
        "Meta",
        6,
        "Story events, corpse systems, blood, and fence damage.",
        _choice_field("helicopter", "Helicopter", "Helicopter", '"Once"', _options(("1", "Never"), ("2", "Once"), ("3", "Sometimes"), ("4", "Often"))),
        _choice_field("meta-event", "Meta Event", "MetaEvent", '"Sometimes"', NEVER_SOMETIMES_OFTEN_OPTIONS),
        _choice_field("sleeping-event", "Sleeping Event", "SleepingEvent", '"Never"', NEVER_SOMETIMES_OFTEN_OPTIONS),
        _decimal_field("generator-fuel-consumption", "Generator Fuel Consumption", "GeneratorFuelConsumption", '"0.1"'),
        _choice_field("survivor-house-chance", "Randomized Building Chance", "SurvivorHouseChance", '"Rare"', STORY_CHANCE_OPTIONS),
        _choice_field("vehicle-story-chance", "Randomized Road Stories Chance", "VehicleStoryChance", '"Rare"', STORY_CHANCE_OPTIONS),
        _choice_field("zone-story-chance", "Randomized Zone Stories Chance", "ZoneStoryChance", '"Rare"', STORY_CHANCE_OPTIONS),
        _choice_field("annotated-map-chance", "Annotated Map Chance", "AnnotatedMapChance", '"Sometimes"', FREQUENCY_OPTIONS),
        _decimal_field("hours-for-corpse-removal", "Time Before Corpse Removal", "HoursForCorpseRemoval", '"216.0"'),
        _choice_field("decaying-corpse-health-impact", "Decaying Corpse Health Impact", "DecayingCorpseHealthImpact", '"Normal"', _options(("1", "None"), ("2", "Low"), ("3", "Normal"), ("4", "High"), ("5", "Insane"))),
        _bool_field("zombie-health-impact", "Zombie Health Impact", "ZombieHealthImpact", '"false"'),
        _choice_field("blood-level", "Blood Level", "BloodLevel", '"Normal"', _options(("1", "None"), ("2", "Low"), ("3", "Normal"), ("4", "High"), ("5", "Ultra Gore"))),
        _int_field("blood-splat-lifespan-days", "Blood Splat Lifespan Days", "BloodSplatLifespanDays", '"0"'),
        _choice_field("corpse-maggot-spawn", "Corpse Maggot Spawn", "MaggotSpawn", '"In and Around Bodies"', _options(("1", "In and Around Bodies"), ("2", "In Bodies Only"), ("3", "Never"))),
        _choice_field("media-list-meta-knowledge", "Media List Meta Knowledge", "MetaKnowledge", '"Completely hidden"', _options(("1", "Fully revealed"), ("2", "Shown as ???"), ("3", "Completely hidden"))),
        _choice_field("day-night-cycle", "Day / Night Cycle", "DayNightCycle", '"Normal"', _options(("1", "Normal"), ("2", "Endless Day"), ("3", "Endless Night"))),
        _choice_field("climate-cycle", "Climate Cycle", "ClimateCycle", '"Normal"', _options(("1", "Normal"), ("2", "No Weather"), ("3", "Endless Rain"), ("4", "Endless Storm"), ("5", "Endless Snow"), ("6", "Endless Blizzard"))),
        _choice_field("fog-cycle", "Fog Cycle", "FogCycle", '"Normal"', _options(("1", "Normal"), ("2", "No Fog"), ("3", "Endless Fog"))),
        _int_field("zombies-to-damage-fences", "Zombies To Damage Fences", "ZombieLore.FenceThumpersRequired", '"25"'),
        _decimal_field("fence-damage-multiplier", "Fence Damage Multiplier", "ZombieLore.FenceDamageMultiplier", '"1.0"'),
    ),
    _section(
        "meta.map",
        "In-game Map",
        "Meta",
        6,
        "World map discovery and readability.",
        _bool_field("allow-world-map", "Allow World Map", "Map.AllowWorldMap", '"true"'),
        _bool_field("allow-mini-map", "Allow Mini-Map", "Map.AllowMiniMap", '"false"'),
        _bool_field("map-all-known", "All Known On Start", "Map.MapAllKnown", '"false"'),
        _bool_field("light-needed-to-read-map", "Light Needed To Read Map", "Map.MapNeedsLight", '"true"'),
    ),
    _section(
        "character.core",
        "Character",
        "Character",
        7,
        "Character rules, injuries, combat, reading, and progression constraints.",
        _choice_field("stats-decrease", "Stats Decrease", "StatsDecrease", '"Normal"', VERY_FAST_TO_VERY_SLOW_OPTIONS),
        _choice_field("end-regen", "Endurance Regeneration", "EndRegen", '"Normal"', VERY_FAST_TO_VERY_SLOW_OPTIONS),
        _bool_field("nutrition", "Nutrition System", "Nutrition", '"true"'),
        _bool_field("starter-kit", "Starter Kit", "StarterKit", '"false"'),
        _int_field("character-free-points", "Free Trait Points", "CharacterFreePoints", '"0"'),
        _choice_field("player-built-construction-strength", "Player-built Construction Strength", "ConstructionBonusPoints", '"Normal"', VERY_LOW_TO_VERY_HIGH_OPTIONS),
        _choice_field("injury-severity", "Injury Severity", "InjurySeverity", '"Normal"', LOW_NORMAL_HIGH_OPTIONS),
        _bool_field("bone-fracture", "Bone Fracture", "BoneFracture", '"true"'),
        _decimal_field("muscle-strain-factor", "Muscle Strain Factor", "MuscleStrainFactor", '"0.7"'),
        _decimal_field("discomfort-factor", "Discomfort Factor", "DiscomfortFactor", '"0.8"'),
        _decimal_field("wound-infection-damage-factor", "Wound Infection Damage Factor", "WoundInfectionFactor", '"0.0"'),
        _choice_field("clothing-degradation", "Clothing Degradation", "ClothingDegradation", '"Normal"', _options(("1", "Disabled"), ("2", "Slow"), ("3", "Normal"), ("4", "Fast"))),
        _bool_field("no-black-clothes", "No Black Clothes", "NoBlackClothes", '"true"'),
        _choice_field("rear-vulnerability", "Rear Vulnerability", "RearVulnerability", '"High"', _options(("1", "Low"), ("2", "Medium"), ("3", "High"))),
        _bool_field("multi-hit", "Weapon Multi Hit", "MultiHitZombies", '"false"'),
        _choice_field("firearms-use-damage-chance", "Firearms Use Damage Chance", "FirearmUseDamageChance", '"Zombies only"', _options(("1", "Disabled"), ("2", "Zombies only"), ("3", "All types of target"))),
        _decimal_field("firearm-noise-multiplier", "Firearm Noise Multiplier", "FirearmNoiseMultiplier", '"1.0"'),
        _decimal_field("firearm-jam-multiplier", "Firearm Jam Multiplier", "FirearmJamMultiplier", '"1.0"'),
        _decimal_field("firearm-moodle-multiplier", "Firearm Moodle Multiplier", "FirearmMoodleMultiplier", '"1.0"'),
        _decimal_field("firearm-weather-multiplier", "Firearm Weather Multiplier", "FirearmWeatherMultiplier", '"1.0"'),
        _bool_field("firearm-headgear-effect", "Firearm Headgear Effect", "FirearmHeadGearEffect", '"true"'),
        _bool_field("attack-block-movements", "Melee Movement Disruption", "AttackBlockMovements", '"true"'),
        _bool_field("all-clothes-unlocked", "All Clothing Unlocked", "AllClothesUnlocked", '"false"'),
        _choice_field("enable-poisoning", "Enable Poisoning", "EnablePoisoning", '"True"', _options(("1", "True"), ("2", "False"), ("3", "Only bleach poisoning is disabled"))),
        _int_field("literature-cooldown-days", "Literature Cooldown Days", "LiteratureCooldown", '"45"'),
        _choice_field("negative-traits-penalty", "Negative Traits Penalty", "NegativeTraitsPenalty", '"None"', _options(("1", "None"), ("2", "1 point penalty for every 3 negative traits selected"), ("3", "1 point penalty for every 2 negative traits selected"), ("4", "1 point penalty for every negative trait selected after the first"))),
        _decimal_field("minutes-per-page", "Minutes Per Skill Book Page", "MinutesPerPage", '"2.0"'),
        _int_field("maximum-dismantling-xp-level", "Maximum Dismantling XP Level", "LevelForDismantleXPCutoff", '"0"'),
        _int_field("maximum-media-xp-level", "Maximum Media XP Level", "LevelForMediaXPCutoff", '"3"'),
        _bool_field("easy-climbing", "Easy Climbing", "EasyClimbing", '"false"'),
        _bool_field("see-not-known-recipes", "See Not Known Recipes", "SeeNotLearntRecipe", '"true"'),
    ),
    _section(
        "character.xp",
        "XP multipliers",
        "Character",
        7,
        "Global and per-skill multiplier controls.",
        _decimal_field("xp-global-multiplier", "Global Multiplier", "MultiplierConfig.Global", '"1.0"'),
        _bool_field("xp-use-global-multiplier", "Use Global Multiplier", "MultiplierConfig.GlobalToggle", '"true"'),
        _decimal_field("xp-fitness-multiplier", "Fitness Multiplier", "MultiplierConfig.Fitness", '"1.0"'),
        _decimal_field("xp-strength-multiplier", "Strength Multiplier", "MultiplierConfig.Strength", '"1.0"'),
        _decimal_field("xp-sprinting-multiplier", "Sprinting Multiplier", "MultiplierConfig.Sprinting", '"1.0"'),
        _decimal_field("xp-lightfooted-multiplier", "Lightfooted Multiplier", "MultiplierConfig.Lightfoot", '"1.0"'),
        _decimal_field("xp-nimble-multiplier", "Nimble Multiplier", "MultiplierConfig.Nimble", '"1.0"'),
        _decimal_field("xp-sneaking-multiplier", "Sneaking Multiplier", "MultiplierConfig.Sneak", '"1.0"'),
        _decimal_field("xp-axe-multiplier", "Axe Multiplier", "MultiplierConfig.Axe", '"1.0"'),
        _decimal_field("xp-long-blunt-multiplier", "Long Blunt Multiplier", "MultiplierConfig.Blunt", '"1.0"'),
        _decimal_field("xp-short-blunt-multiplier", "Short Blunt Multiplier", "MultiplierConfig.SmallBlunt", '"1.0"'),
        _decimal_field("xp-long-blade-multiplier", "Long Blade Multiplier", "MultiplierConfig.LongBlade", '"1.0"'),
        _decimal_field("xp-short-blade-multiplier", "Short Blade Multiplier", "MultiplierConfig.SmallBlade", '"1.0"'),
        _decimal_field("xp-spear-multiplier", "Spear Multiplier", "MultiplierConfig.Spear", '"1.0"'),
        _decimal_field("xp-maintenance-multiplier", "Maintenance Multiplier", "MultiplierConfig.Maintenance", '"1.0"'),
        _decimal_field("xp-agriculture-multiplier", "Agriculture Multiplier", "MultiplierConfig.Farming", '"1.0"'),
        _decimal_field("xp-animal-care-multiplier", "Animal Care Multiplier", "MultiplierConfig.Husbandry", '"1.0"'),
        _decimal_field("xp-carpentry-multiplier", "Carpentry Multiplier", "MultiplierConfig.Woodwork", '"1.0"'),
        _decimal_field("xp-carving-multiplier", "Carving Multiplier", "MultiplierConfig.Carving", '"1.0"'),
        _decimal_field("xp-cooking-multiplier", "Cooking Multiplier", "MultiplierConfig.Cooking", '"1.0"'),
        _decimal_field("xp-electrical-multiplier", "Electrical Multiplier", "MultiplierConfig.Electricity", '"1.0"'),
        _decimal_field("xp-first-aid-multiplier", "First Aid Multiplier", "MultiplierConfig.Doctor", '"1.0"'),
        _decimal_field("xp-knapping-multiplier", "Knapping Multiplier", "MultiplierConfig.FlintKnapping", '"1.0"'),
        _decimal_field("xp-masonry-multiplier", "Masonry Multiplier", "MultiplierConfig.Masonry", '"1.0"'),
        _decimal_field("xp-mechanics-multiplier", "Mechanics Multiplier", "MultiplierConfig.Mechanics", '"1.0"'),
        _decimal_field("xp-blacksmithing-multiplier", "Blacksmithing Multiplier", "MultiplierConfig.Blacksmith", '"1.0"'),
        _decimal_field("xp-pottery-multiplier", "Pottery Multiplier", "MultiplierConfig.Pottery", '"1.0"'),
        _decimal_field("xp-tailoring-multiplier", "Tailoring Multiplier", "MultiplierConfig.Tailoring", '"1.0"'),
        _decimal_field("xp-welding-multiplier", "Welding Multiplier", "MultiplierConfig.MetalWelding", '"1.0"'),
        _decimal_field("xp-aiming-multiplier", "Aiming Multiplier", "MultiplierConfig.Aiming", '"1.0"'),
        _decimal_field("xp-reloading-multiplier", "Reloading Multiplier", "MultiplierConfig.Reloading", '"1.0"'),
        _decimal_field("xp-fishing-multiplier", "Fishing Multiplier", "MultiplierConfig.Fishing", '"1.0"'),
        _decimal_field("xp-foraging-multiplier", "Foraging Multiplier", "MultiplierConfig.PlantScavenging", '"1.0"'),
        _decimal_field("xp-tracking-multiplier", "Tracking Multiplier", "MultiplierConfig.Tracking", '"1.0"'),
        _decimal_field("xp-trapping-multiplier", "Trapping Multiplier", "MultiplierConfig.Trapping", '"1.0"'),
        _decimal_field("xp-butchering-multiplier", "Butchering Multiplier", "MultiplierConfig.Butchering", '"1.0"'),
        _decimal_field("xp-glassmaking-multiplier", "Glassmaking Multiplier", "MultiplierConfig.Glassmaking", '"1.0"'),
    ),
    _section(
        "vehicles.core",
        "Vehicles",
        "Vehicles",
        8,
        "Vehicle availability, condition, alarms, and collision behavior.",
        _bool_field("enable-vehicles", "Vehicles", "EnableVehicles", '"true"'),
        _bool_field("vehicle-easy-use", "Easy Use", "VehicleEasyUse", '"false"'),
        _choice_field("recently-survivor-vehicles", "Recent Survivor Vehicles", "RecentlySurvivorVehicles", '"Low"', _options(("1", "None"), ("2", "Low"), ("3", "Normal"), ("4", "High"))),
        _decimal_field("zombie-attraction-multiplier", "Zombie Attraction Multiplier", "ZombieAttractionMultiplier", '"1.0"'),
        _choice_field("car-spawn-rate", "Vehicle Spawn Rate", "CarSpawnRate", '"Low"', _options(("1", "None"), ("2", "Very Low"), ("3", "Low"), ("4", "Normal"), ("5", "High"))),
        _choice_field("chance-has-gas", "Chance Has Gas", "ChanceHasGas", '"Low"', _options(("1", "Low"), ("2", "Normal"), ("3", "High"))),
        _choice_field("initial-gas", "Initial Gas", "InitialGas", '"Low"', _options(("1", "Very Low"), ("2", "Low"), ("3", "Normal"), ("4", "High"), ("5", "Very High"), ("6", "Full"))),
        _decimal_field("car-gas-consumption", "Gas Consumption", "CarGasConsumption", '"1.0"'),
        _choice_field("locked-car", "Locked Vehicle Frequency", "LockedCar", '"Sometimes"', FREQUENCY_OPTIONS),
        _choice_field("car-general-condition", "General Condition", "CarGeneralCondition", '"Normal"', VERY_LOW_TO_VERY_HIGH_OPTIONS),
        _bool_field("traffic-jam", "Car Wreck Congestion", "TrafficJam", '"true"'),
        _choice_field("car-alarm", "Vehicle Alarms Frequency", "CarAlarm", '"Rare"', FREQUENCY_OPTIONS),
        _bool_field("player-damage-from-crash", "Player Damage from Crash", "PlayerDamageFromCrash", '"true"'),
        _choice_field("car-damage-on-impact", "Car Damage on Impact", "CarDamageOnImpact", '"Normal"', VERY_LOW_TO_VERY_HIGH_OPTIONS),
        _decimal_field("siren-shutoff-hours", "Siren Shutoff Hours", "SirenShutoffHours", '"0.0"'),
        _choice_field("damage-to-player-from-hit-by-a-car", "Player Damage From Vehicle Impact", "DamageToPlayerFromHitByACar", '"None"', NONE_LOW_NORMAL_HIGH_VERY_HIGH_OPTIONS),
        _bool_field("vehicle-sirens-attract-zombies", "Vehicle Sirens Attract Zombies", "SirenEffectsZombies", '"true"'),
    ),
    _section(
        "livestock.core",
        "Livestock",
        "Livestock",
        9,
        "Animal pacing, spawning, breeding, and trail behavior.",
        _choice_field("livestock-stats-decrease", "Stats Reduction Speed", "AnimalStatsModifier", '"Normal"', ANIMAL_SPEED_OPTIONS),
        _choice_field("pregnancy-time", "Pregnancy Time", "AnimalPregnancyTime", '"Normal"', ANIMAL_SPEED_OPTIONS),
        _choice_field("egg-hatch-time", "Egg Hatch Time", "AnimalEggHatch", '"Normal"', ANIMAL_SPEED_OPTIONS),
        _choice_field("aging-modifier-speed", "Aging Modifier Speed", "AnimalAgeModifier", '"Normal"', ANIMAL_SPEED_OPTIONS),
        _choice_field("milk-increase-speed", "Milk Increase Speed", "AnimalMilkIncModifier", '"Normal"', ANIMAL_SPEED_OPTIONS),
        _choice_field("wool-increase-speed", "Wool Increase Speed", "AnimalWoolIncModifier", '"Normal"', ANIMAL_SPEED_OPTIONS),
        _choice_field("animal-spawn-chance", "Animal Spawn Chance", "AnimalRanchChance", '"Often"', FREQUENCY_WITH_ALWAYS_OPTIONS),
        _int_field("grass-regrowth-time", "Grass Regrowth time", "AnimalGrassRegrowTime", '"240"'),
        _bool_field("meta-predator", "Meta Predator", "AnimalMetaPredator", '"false"'),
        _bool_field("breeding-season", "Breeding Season", "AnimalMatingSeason", '"true"'),
        _bool_field("animals-attract-zombies", "Animals Attract Zombies", "AnimalSoundAttractZombies", '"true"'),
        _choice_field("animal-tracks-chance", "Animal Tracks Chance", "AnimalTrackChance", '"Sometimes"', FREQUENCY_OPTIONS),
        _choice_field("animal-paths-chance", "Animal Paths Chance", "AnimalPathChance", '"Sometimes"', FREQUENCY_OPTIONS),
    ),
)

_OPEN_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{\s*(?:--.*)?$")
_CLOSE_RE = re.compile(r"^(\s*)}\s*,?\s*(?:--.*)?$")
_ENTRY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$")
_LUA_STRING_RE = re.compile(r'^"(.*)"$')


class SandboxVarsDocument:
    def __init__(self, lines: list[SandboxLine]) -> None:
        self.lines = lines

    @classmethod
    def empty(cls) -> "SandboxVarsDocument":
        return cls(
            [
                SandboxLine(kind="open", raw="SandboxVars = {", key="SandboxVars"),
                SandboxLine(kind="close", raw="}", section_path=()),
            ]
        )

    @classmethod
    def parse(cls, content: str) -> "SandboxVarsDocument":
        if "SandboxVars" not in content:
            return cls.empty()

        lines: list[SandboxLine] = []
        context: list[str] = []
        root_seen = False

        for raw_line in content.replace("\r\n", "\n").split("\n"):
            stripped = raw_line.strip()
            if stripped == "":
                lines.append(SandboxLine(kind="blank", raw=""))
                continue

            if stripped.startswith("--"):
                lines.append(SandboxLine(kind="comment", raw=raw_line))
                continue

            open_match = _OPEN_RE.match(raw_line)
            if open_match:
                indent = open_match.group(1)
                key = open_match.group(2)
                if key == "SandboxVars" and not root_seen:
                    root_seen = True
                    lines.append(SandboxLine(kind="open", raw=raw_line, indent=indent, key=key, section_path=()))
                else:
                    opened_path = (*context, key)
                    lines.append(SandboxLine(kind="open", raw=raw_line, indent=indent, key=key, section_path=opened_path))
                    context.append(key)
                continue

            close_match = _CLOSE_RE.match(raw_line)
            if close_match:
                lines.append(
                    SandboxLine(
                        kind="close",
                        raw=raw_line,
                        indent=close_match.group(1),
                        section_path=tuple(context),
                    )
                )
                if context:
                    context.pop()
                continue

            entry = _parse_entry_line(raw_line)
            if entry is not None:
                indent, key, value = entry
                lines.append(
                    SandboxLine(
                        kind="entry",
                        raw=raw_line,
                        indent=indent,
                        section_path=tuple(context),
                        key=key,
                        value=value,
                    )
                )
                continue

            lines.append(SandboxLine(kind="other", raw=raw_line))

        if not root_seen:
            return cls.empty()

        return cls(lines)

    def get(self, path: tuple[str, ...], default: str | None = None) -> str | None:
        for line in self.lines:
            if line.kind == "entry" and line.entry_path == path:
                return line.value

        return default

    def ensure(self, path: tuple[str, ...], value: str) -> bool:
        if self.get(path) is not None:
            return False

        self.set(path, value)
        return True

    def set(self, path: tuple[str, ...], value: str) -> None:
        for line in self.lines:
            if line.kind == "entry" and line.entry_path == path:
                line.value = value
                line.modified = True
                return

        parent_path = path[:-1]
        self._ensure_section(parent_path)
        close_index = self._find_close_index(parent_path)
        indent = "    " * (len(parent_path) + 1)
        self.lines.insert(
            close_index,
            SandboxLine(
                kind="entry",
                indent=indent,
                section_path=parent_path,
                key=path[-1],
                value=value,
                modified=True,
            ),
        )

    def to_text(self) -> str:
        rendered: list[str] = []
        for line in self.lines:
            if line.kind == "entry":
                if line.modified or not line.raw:
                    rendered.append(f"{line.indent}{line.key} = {line.value},")
                else:
                    rendered.append(line.raw)
                continue

            rendered.append(line.raw)

        return "\n".join(rendered).rstrip() + "\n"

    def _ensure_section(self, section_path: tuple[str, ...]) -> None:
        if not section_path:
            return

        if any(line.kind == "open" and line.section_path == section_path for line in self.lines):
            return

        self._ensure_section(section_path[:-1])
        close_index = self._find_close_index(section_path[:-1])
        indent = "    " * len(section_path)
        self.lines[close_index:close_index] = [
            SandboxLine(
                kind="open",
                raw=f"{indent}{section_path[-1]} = {{",
                indent=indent,
                key=section_path[-1],
                section_path=section_path,
            ),
            SandboxLine(
                kind="close",
                raw=f"{indent}}},",
                indent=indent,
                section_path=section_path,
            ),
        ]

    def _find_close_index(self, section_path: tuple[str, ...]) -> int:
        for index, line in enumerate(self.lines):
            if line.kind == "close" and line.section_path == section_path:
                return index

        raise ValueError(f"Could not find closing brace for section path {section_path!r}.")


def _parse_entry_line(raw_line: str) -> tuple[str, str, str] | None:
    match = _ENTRY_RE.match(raw_line)
    if match is None:
        return None

    indent = match.group(1)
    key = match.group(2)
    tail = match.group(3)
    if "--" in tail:
        tail = tail.split("--", 1)[0]

    value = tail.rstrip().removesuffix(",").rstrip()
    return indent, key, value


def _decode_lua_string(raw_value: str) -> str:
    match = _LUA_STRING_RE.match(raw_value.strip())
    if match is None:
        return raw_value

    inner = match.group(1)
    return (
        inner.replace(r"\\", "\\")
        .replace(r"\"", '"')
        .replace(r"\n", "\n")
        .replace(r"\r", "\r")
        .replace(r"\t", "\t")
    )


def _encode_lua_string(value: str) -> str:
    escaped = (
        value.replace("\\", r"\\")
        .replace('"', r"\"")
        .replace("\n", r"\n")
        .replace("\r", r"\r")
        .replace("\t", r"\t")
    )
    return f'"{escaped}"'


def _contains(haystack: str | None, needle: str) -> bool:
    return bool(haystack and needle.lower() in haystack.lower())


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower())
    return normalized.strip("-") or "sandbox"


def _looks_like_numeric_literal(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


class SandboxPresetNode:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.children: dict[str, SandboxPresetNode] = {}


def _format_preset_value(value: str | None) -> str:
    trimmed = value.strip() if value is not None else ""
    lowered = trimmed.lower()
    if lowered in {"true", "false"}:
        return lowered

    if trimmed == "":
        return "0"

    if _looks_like_numeric_literal(trimmed):
        return trimmed

    return _encode_lua_string(trimmed)


def _normalize_preset_value(raw_value: str) -> str:
    trimmed = raw_value.strip()
    string_match = _LUA_STRING_RE.match(trimmed)
    if string_match is not None:
        return _decode_lua_string(trimmed)
    return trimmed


def _read_preset_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    lines = text.replace("\r\n", "\n").split("\n")
    stack: list[str] = []
    root_found = False
    root_closed = False

    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped == "" or stripped.startswith("--"):
            continue

        if not root_found:
            if stripped.startswith("return") and "{" in stripped:
                root_found = True
            continue

        if _CLOSE_RE.match(raw_line):
            if stack:
                stack.pop()
            else:
                root_closed = True
            continue

        open_match = _OPEN_RE.match(raw_line)
        if open_match:
            stack.append(open_match.group(2))
            continue

        entry = _parse_entry_line(raw_line)
        if entry is None:
            continue

        _, key, value = entry
        key_path = ".".join([*stack, key]) if stack else key
        values[key_path] = _normalize_preset_value(value)

    if not root_found or not root_closed:
        raise ValueError("Preset .lua files must use a supported 'return { ... }' layout.")

    return values


def _add_preset_value(root: SandboxPresetNode, key_path: str, value: str) -> None:
    segments = [segment for segment in key_path.split(".") if segment]
    if not segments:
        return

    current = root
    for segment in segments[:-1]:
        current = current.children.setdefault(segment, SandboxPresetNode())
    current.values[segments[-1]] = value


def _write_preset_node(lines: list[str], node: SandboxPresetNode, indent: str) -> None:
    entries: list[tuple[str, str, object]] = []
    entries.extend((key, "value", value) for key, value in node.values.items())
    entries.extend((key, "table", child) for key, child in node.children.items())

    for index, (key, kind, payload) in enumerate(entries):
        trailing = "," if index < len(entries) - 1 else ""
        if kind == "value":
            lines.append(f"{indent}{key} = {_format_preset_value(payload)}{trailing}")
            continue

        lines.append(f"{indent}{key} = {{")
        _write_preset_node(lines, payload, indent + "    ")
        lines.append(f"{indent}}}{trailing}")


def _write_preset_values(values: Mapping[str, str]) -> str:
    root = SandboxPresetNode()
    for key_path, value in values.items():
        _add_preset_value(root, key_path, value)

    lines = ["return {"]
    _write_preset_node(lines, root, "    ")
    lines.append("}")
    return "\n".join(lines) + "\n"


class ProjectZomboidSandboxService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sections = SANDBOX_SECTIONS
        self.fields_by_name: dict[str, SandboxFieldDefinition] = {
            field.name: field
            for section in self.sections
            for field in section.fields
        }
        self.category_sections: dict[str, list[SandboxSectionDefinition]] = {}
        self.category_orders: dict[str, int] = {}
        for section in self.sections:
            self.category_sections.setdefault(section.category_title, []).append(section)
            self.category_orders[section.category_title] = section.category_order

    def path_for_profile(self, profile: ServerProfile) -> Path:
        return Path(profile.cache_directory) / "Server" / f"{profile.server_name}_SandboxVars.lua"

    def built_in_preset_root(self) -> Path:
        return Path(__file__).resolve().parent.parent / "assets" / "sandbox-presets" / "b42"

    def custom_preset_root(self) -> Path:
        path = self.settings.data_root / "sandbox-presets" / "b42" / "custom"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def load_values(self, profile: ServerProfile) -> dict[str, str]:
        document, _ = self._load_document(profile)
        values: dict[str, str] = {}
        for field in self.fields_by_name.values():
            raw_value = document.get(field.key_path)
            values[field.name] = self._from_document_value(field, raw_value) if raw_value is not None else field.default
        return values

    def normalize_values(
        self,
        submitted_values: Mapping[str, object],
        current_values: Mapping[str, str],
        *,
        visible_fields: Iterable[str] | None = None,
    ) -> dict[str, str]:
        visible = set(visible_fields) if visible_fields is not None else set(self.fields_by_name)
        normalized: dict[str, str] = {}
        for field in self.fields_by_name.values():
            current_value = current_values.get(field.name, field.default)
            if field.name not in visible:
                normalized[field.name] = current_value
                continue
            normalized[field.name] = self._normalize_value(field, submitted_values.get(field.name), current_value)
        return normalized

    def save_values(self, profile: ServerProfile, submitted_values: Mapping[str, object]) -> Path:
        normalized_values = self.normalize_values(submitted_values, self.load_values(profile))
        return self.save_editor_values(profile, normalized_values)

    def save_editor_values(self, profile: ServerProfile, editor_values: Mapping[str, str]) -> Path:
        document, path = self._load_document(profile)
        for field in self.fields_by_name.values():
            document.set(field.key_path, self._to_document_value(field, editor_values.get(field.name, field.default)))

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document.to_text(), encoding="utf-8")
        return path

    def list_presets(self) -> list[SandboxPresetView]:
        presets: list[SandboxPresetView] = []

        order_lookup = {name.lower(): index for index, name in enumerate(BUILT_IN_PRESET_ORDER)}
        built_in_root = self.built_in_preset_root()
        if built_in_root.exists():
            for path in sorted(
                built_in_root.glob("*.lua"),
                key=lambda item: (order_lookup.get(item.stem.lower(), 10_000), item.stem.lower()),
            ):
                preset = self._try_load_preset(path, is_builtin=True)
                if preset is not None:
                    presets.append(preset)

        for path in sorted(self.custom_preset_root().glob("*.lua"), key=lambda item: item.stem.lower()):
            preset = self._try_load_preset(path, is_builtin=False)
            if preset is not None:
                presets.append(preset)

        return presets

    @staticmethod
    def resolve_preset(presets: list[SandboxPresetView], preset_id: str | None) -> SandboxPresetView | None:
        if not presets:
            return None
        if preset_id:
            for preset in presets:
                if preset.id == preset_id:
                    return preset
        return presets[0]

    def apply_preset_values(self, current_values: Mapping[str, str], preset: SandboxPresetView | None) -> dict[str, str]:
        values = {field.name: current_values.get(field.name, field.default) for field in self.fields_by_name.values()}
        if preset is None:
            return values
        for field_name, field_value in preset.values.items():
            if field_name in self.fields_by_name:
                values[field_name] = field_value
        return values

    def reset_category_to_preset(
        self,
        current_values: Mapping[str, str],
        preset: SandboxPresetView | None,
        category_id: str,
    ) -> dict[str, str]:
        values = dict(self.apply_preset_values(current_values, None))
        if preset is None:
            return values

        field_names = {
            field.name
            for title, sections in self.category_sections.items()
            if _slug(title) == category_id
            for section in sections
            for field in section.fields
        }
        for field_name in field_names:
            if field_name in preset.values:
                values[field_name] = preset.values[field_name]
        return values

    def save_custom_preset(self, name: str, editor_values: Mapping[str, str]) -> SandboxPresetView:
        file_stem = self._sanitize_preset_name(name)
        built_in_conflict = any(path.stem.lower() == file_stem.lower() for path in self.built_in_preset_root().glob("*.lua"))
        if built_in_conflict:
            raise ValueError(f"'{file_stem}' is already reserved by a shipped sandbox preset.")

        raw_values = self._load_base_preset_raw_values()
        for field in self.fields_by_name.values():
            raw_values[".".join(field.key_path)] = editor_values.get(field.name, field.default)
        raw_values.setdefault("Version", "6")

        path = self.custom_preset_root() / f"{file_stem}.lua"
        path.write_text(_write_preset_values(raw_values), encoding="utf-8")
        preset = self._try_load_preset(path, is_builtin=False)
        if preset is None:
            raise ValueError(f"Sandbox preset '{file_stem}' could not be loaded after save.")
        return preset

    def delete_custom_preset(self, preset_id: str) -> bool:
        is_builtin, file_stem = self._parse_preset_id(preset_id)
        if is_builtin:
            raise ValueError("Only custom sandbox presets can be deleted.")

        path = self.custom_preset_root() / f"{file_stem}.lua"
        if not path.exists():
            return False
        path.unlink()
        return True

    def build_category_views(
        self,
        values: Mapping[str, str],
        preset: SandboxPresetView | None,
        *,
        search_text: str = "",
        active_category_id: str = "",
    ) -> list[SandboxCategoryView]:
        query = search_text.strip().lower()
        categories: list[SandboxCategoryView] = []

        for category_title in sorted(self.category_sections, key=lambda item: (self.category_orders[item], item.lower())):
            category_id = _slug(category_title)
            if active_category_id and active_category_id != category_id:
                continue

            section_views: list[SandboxSectionView] = []
            compared_total = 0
            matching_total = 0

            for section in self.category_sections[category_title]:
                all_fields = [self._build_field_view(field, values, preset) for field in section.fields]
                compared_total += sum(1 for field in all_fields if field.has_preset_value)
                matching_total += sum(1 for field in all_fields if field.has_preset_value and field.matches_preset)

                visible_fields = [
                    field
                    for field in all_fields
                    if self._matches_search(section, field, query)
                ]
                if not visible_fields:
                    continue

                section_views.append(
                    SandboxSectionView(
                        definition=section,
                        fields=visible_fields,
                        compared_field_count=sum(1 for field in all_fields if field.has_preset_value),
                        matching_field_count=sum(1 for field in all_fields if field.has_preset_value and field.matches_preset),
                    )
                )

            if not section_views:
                continue

            matches_preset = compared_total == 0 or compared_total == matching_total
            status_text = (
                f"{sum(len(section.fields) for section in section_views)} field(s)"
                if compared_total == 0
                else (f"Matches preset ({matching_total}/{compared_total})" if matches_preset else f"Changed ({matching_total}/{compared_total})")
            )
            categories.append(
                SandboxCategoryView(
                    id=category_id,
                    title=category_title,
                    order=self.category_orders[category_title],
                    sections=section_views,
                    status_text=status_text,
                    matches_preset=matches_preset,
                    compared_field_count=compared_total,
                    matching_field_count=matching_total,
                )
            )

        return categories

    def _load_document(self, profile: ServerProfile) -> tuple[SandboxVarsDocument, Path]:
        path = self.path_for_profile(profile)
        if path.exists():
            document = SandboxVarsDocument.parse(path.read_text(encoding="utf-8"))
        else:
            document = SandboxVarsDocument.empty()

        changed = document.ensure(("VERSION",), "5")
        for field in self.fields_by_name.values():
            changed = document.ensure(field.key_path, self._to_document_value(field, field.default)) or changed

        if changed or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(document.to_text(), encoding="utf-8")

        return document, path

    def _load_base_preset_raw_values(self) -> dict[str, str]:
        built_in_root = self.built_in_preset_root()
        preferred = built_in_root / "Apocalypse.lua"
        source_path = preferred if preferred.exists() else next(iter(sorted(built_in_root.glob("*.lua"))), None)
        if source_path is None:
            return {}
        return _read_preset_values(source_path.read_text(encoding="utf-8"))

    def _try_load_preset(self, path: Path, *, is_builtin: bool) -> SandboxPresetView | None:
        try:
            raw_values = _read_preset_values(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

        editor_values: dict[str, str] = {}
        for field in self.fields_by_name.values():
            key_path = ".".join(field.key_path)
            if key_path not in raw_values:
                continue
            editor_values[field.name] = self._normalize_preset_field_value(field, raw_values[key_path])

        return SandboxPresetView(
            id=f"{'builtin' if is_builtin else 'user'}:{path.stem}",
            name=self._humanize_built_in_label(path.stem) if is_builtin else path.stem,
            is_builtin=is_builtin,
            values=editor_values,
        )

    def _build_field_view(
        self,
        field: SandboxFieldDefinition,
        values: Mapping[str, str],
        preset: SandboxPresetView | None,
    ) -> SandboxFieldView:
        current_value = values.get(field.name, field.default)
        has_preset_value = preset is not None and field.name in preset.values
        preset_value = preset.values.get(field.name) if preset is not None else None
        return SandboxFieldView(
            definition=field,
            current_value=current_value,
            preset_value=preset_value,
            has_preset_value=has_preset_value,
            matches_preset=(not has_preset_value) or current_value == preset_value,
        )

    @staticmethod
    def _matches_search(section: SandboxSectionDefinition, field: SandboxFieldView, query: str) -> bool:
        if query == "":
            return True
        return (
            _contains(section.title, query)
            or _contains(section.description, query)
            or _contains(field.definition.label, query)
            or _contains(field.current_value, query)
            or any(_contains(option.label, query) or _contains(option.value, query) for option in field.definition.options)
        )

    @staticmethod
    def _sanitize_preset_name(value: str) -> str:
        trimmed = value.strip()
        if trimmed == "":
            raise ValueError("Preset name is required.")

        invalid_characters = set('<>:"/\\|?*')
        normalized = "".join(" " if char in invalid_characters else char for char in trimmed)
        normalized = " ".join(normalized.split()).strip().rstrip(".")
        if normalized == "":
            raise ValueError("Preset name must contain at least one usable letter or number.")
        return normalized

    @staticmethod
    def _parse_preset_id(preset_id: str) -> tuple[bool, str]:
        if ":" not in preset_id:
            raise ValueError("Sandbox preset id is invalid.")
        prefix, file_stem = preset_id.split(":", 1)
        if not file_stem:
            raise ValueError("Sandbox preset id is invalid.")
        return prefix.lower() == "builtin", file_stem

    @staticmethod
    def _humanize_built_in_label(file_stem: str) -> str:
        pieces: list[str] = []
        for index, char in enumerate(file_stem):
            if index > 0 and char.isupper() and (file_stem[index - 1].islower() or file_stem[index - 1].isdigit()):
                pieces.append(" ")
            pieces.append(char)
        return "".join(pieces)

    def _normalize_preset_field_value(self, field: SandboxFieldDefinition, raw_value: str) -> str:
        if field.field_type == "textarea":
            return raw_value
        if field.field_type == "select":
            for option in field.options:
                if raw_value == option.value or raw_value == option.label:
                    return option.value
            return field.default
        if field.field_type == "checkbox":
            return "true" if raw_value.strip().lower() == "true" else "false"
        return raw_value

    @staticmethod
    def _normalize_value(field: SandboxFieldDefinition, submitted_value: object | None, current_value: str) -> str:
        if field.field_type == "checkbox":
            return "true" if submitted_value not in {None, "", False} else "false"

        raw_text = str(submitted_value).strip() if submitted_value is not None else ""
        candidate = raw_text or current_value or field.default

        if field.field_type == "select":
            allowed = {option.value for option in field.options}
            if candidate in allowed:
                return candidate
            return current_value if current_value in allowed else field.default

        if field.field_type == "integer":
            try:
                return str(int(candidate))
            except (TypeError, ValueError):
                return field.default

        if field.field_type == "decimal":
            try:
                float(candidate)
                return candidate
            except (TypeError, ValueError):
                return field.default

        return candidate

    @staticmethod
    def _from_document_value(field: SandboxFieldDefinition, raw_value: str) -> str:
        if field.field_type == "textarea":
            return _decode_lua_string(raw_value)
        return raw_value

    @staticmethod
    def _to_document_value(field: SandboxFieldDefinition, value: str) -> str:
        if field.field_type == "textarea":
            return _encode_lua_string(value)
        return value
