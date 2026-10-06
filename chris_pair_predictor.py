"""
Chris Pair Predictor Engine

Core Chris Pair decoding logic for Pick 3, Pick 4, Pick 5.
Numbers are ALWAYS strings to preserve leading zeros.

Example: "0080" stays "0080", never becomes "80" or 80.
"""

from typing import List, Dict, Set, Tuple, Optional
from dataclasses import dataclass
from enum import Enum

# ============================================================
# CHRIS PAIR MAPPINGS (Verified)
# ============================================================

CHRIS_PAIRS: Dict[str, str] = {
    "00": "45", "45": "00",
    "01": "29", "29": "01",
    "11": "23", "23": "11",
    "14": "59", "59": "14",
    "22": "46", "46": "22",
    "33": "24", "24": "33",
    "34": "90", "90": "34",
    "37": "28", "28": "37",
    "39": "48", "48": "39",
    "44": "20", "20": "44",
    "51": "81", "81": "51",
    "53": "27", "27": "53",
    "55": "16", "16": "55",
    "56": "70", "70": "56",
    "66": "12", "12": "66",
    "77": "13", "77": "13",
    "88": "47", "47": "88",
    "99": "78", "78": "99",
}

# Extended followers from specification
CHRIS_PAIR_FOLLOWERS: Dict[str, List[str]] = {
    **{k: [v] for k, v in CHRIS_PAIRS.items()},
    "04": ["50", "52", "55"],
    "06": ["58", "54", "55"],
    "07": ["59"],
}


# ============================================================
# PATTERN CLASSIFICATION
# ============================================================

class PatternType(Enum):
    SINGLE = "SINGLE"
    DOUBLE = "DOUBLE"
    DOUBLE_DOUBLE = "DOUBLE_DOUBLE"
    TRIPLE = "TRIPLE"
    QUAD = "QUAD"


class PressureLevel(Enum):
    NONE = "NONE"
    WATCH = "WATCH"
    PRESSURE = "PRESSURE"
    STRONG_PRESSURE = "STRONG_PRESSURE"


def digit_frequency(number: str) -> Dict[str, int]:
    """Get frequency of each digit in a number string."""
    freq = {}
    for d in number:
        freq[d] = freq.get(d, 0) + 1
    return freq


def classify_pattern(number: str) -> PatternType:
    """Classify a lottery number into pattern type."""
    freq = digit_frequency(number)
    values = list(freq.values())
    
    if 4 in values:
        return PatternType.QUAD
    if 3 in values:
        return PatternType.TRIPLE
    if len(number) == 4 and values.count(2) == 2:
        return PatternType.DOUBLE_DOUBLE
    if 2 in values:
        return PatternType.DOUBLE
    return PatternType.SINGLE


def get_double_digit(number: str) -> Optional[str]:
    """Get the digit that appears twice (if any)."""
    freq = digit_frequency(number)
    for d, count in freq.items():
        if count >= 2:
            return d
    return None


def get_triple_digit(number: str) -> Optional[str]:
    """Get the digit that appears three times (if any)."""
    freq = digit_frequency(number)
    for d, count in freq.items():
        if count == 3:
            return d
    return None


def get_quad_digit(number: str) -> Optional[str]:
    """Get the digit that appears four times (if any)."""
    freq = digit_frequency(number)
    for d, count in freq.items():
        if count == 4:
            return d
    return None


# ============================================================
# CHRIS PAIR DECODER (Core Prediction Engine)
# ============================================================

def get_pair_positions(length: int) -> List[Tuple[int, int]]:
    """Generate all position pairs for a number of given length."""
    positions = []
    for i in range(length):
        for j in range(i + 1, length):
            positions.append((i, j))
    return positions


def run_chris_decoder(input_number: str, digit_count: int = None) -> List[str]:
    """
    Chris Decoder for Pick 3, Pick 4, or Pick 5.
    
    Args:
        input_number: The lottery number (string, preserves leading zeros)
        digit_count: Expected digit count (3, 4, or 5). Auto-detected if None.
    
    Returns:
        List of predicted numbers (strings, preserving leading zeros)
    
    Example:
        run_chris_decoder("112") -> ["166", "223", "232", "661"]
    """
    if digit_count is None:
        digit_count = len(input_number)
    
    # Validation
    if len(input_number) != digit_count:
        return []
    if digit_count < 3 or digit_count > 5:
        return []
    if not input_number.isdigit():
        return []
    
    digits = list(input_number)
    results: Set[str] = set()
    
    for i, j in get_pair_positions(digit_count):
        pair = digits[i] + digits[j]
        reverse_pair = digits[j] + digits[i]
        
        # Check both pair and reverse pair
        for p in [pair, reverse_pair]:
            if p in CHRIS_PAIRS:
                decoded = CHRIS_PAIRS[p]
                # Get leftover digits (excluding positions i and j)
                leftover = "".join(digits[k] for k in range(digit_count) if k not in (i, j))
                
                # Create outputs
                output_a = decoded + leftover
                output_b = leftover + decoded
                
                if len(output_a) == digit_count:
                    results.add(output_a)
                if len(output_b) == digit_count:
                    results.add(output_b)
    
    # Filter and sort results
    valid = [r for r in results if len(r) == digit_count and r.isdigit()]
    return sorted(valid)


# ============================================================
# WATCH NUMBER / PRESSURE ANALYSIS
# ============================================================

@dataclass
class TriggeredRule:
    rule_id: str
    rule_name: str
    rule_type: str
    source_pattern: str
    matched_value: str
    watch_numbers: List[str]
    explanation: str
    confidence: float


@dataclass
class WatchNumber:
    number: str
    signal_count: int
    triggering_rules: List[str]
    pressure_level: str


@dataclass
class ChrisAnalysis:
    source_number: str
    pattern: str
    triggered_rules: List[TriggeredRule]
    watch_numbers: List[WatchNumber]
    pressure_type: str
    total_signals: int
    predictions: List[str]  # Chris decoder outputs


def get_all_pairs(number: str) -> List[str]:
    """Get all 2-digit pairs from a number (position-based)."""
    pairs = []
    for i, j in get_pair_positions(len(number)):
        pairs.append(number[i] + number[j])
    return pairs


def analyze_number(number: str, game: str = "pick4") -> ChrisAnalysis:
    """
    Full Chris Pair analysis of a lottery number.
    
    Args:
        number: The lottery number (string)
        game: "pick3", "pick4", or "pick5"
    
    Returns:
        ChrisAnalysis with predictions, triggered rules, and watch numbers
    """
    # Run the decoder
    digit_count = 3 if game == "pick3" else 4 if game == "pick4" else 5
    predictions = run_chris_decoder(number, digit_count)
    
    # Classify pattern
    pattern = classify_pattern(number)
    
    # Find triggered rules
    triggered_rules: List[TriggeredRule] = []
    all_watch: Dict[str, List[str]] = {}  # number -> rule_ids
    
    # Check pair followers
    pairs = get_all_pairs(number)
    for pair in pairs:
        if pair in CHRIS_PAIR_FOLLOWERS:
            followers = CHRIS_PAIR_FOLLOWERS[pair]
            rule = TriggeredRule(
                rule_id=f"pair_follower_{pair}",
                rule_name=f"{pair} Pair Follower",
                rule_type="PAIR_FOLLOWER",
                source_pattern=pair,
                matched_value=pair,
                watch_numbers=followers,
                explanation=f"Pair {pair} detected → watch for {', '.join(followers)}",
                confidence=0.8
            )
            triggered_rules.append(rule)
            
            for w in followers:
                if w not in all_watch:
                    all_watch[w] = []
                all_watch[w].append(rule.rule_id)
    
    # Check pattern pressure
    if pattern == PatternType.QUAD:
        quad_digit = get_quad_digit(number)
        triggered_rules.append(TriggeredRule(
            rule_id="quad_pressure",
            rule_name="Quad Event",
            rule_type="QUAD_PRESSURE",
            source_pattern="QUAD",
            matched_value=quad_digit * 4 if quad_digit else number,
            watch_numbers=[],
            explanation=f"QUAD {number} fell! Extreme pressure event.",
            confidence=1.0
        ))
    elif pattern == PatternType.TRIPLE:
        triple_digit = get_triple_digit(number)
        if triple_digit:
            quad_watch = triple_digit * 4
            triggered_rules.append(TriggeredRule(
                rule_id="triple_pressure",
                rule_name="Triple Pressure",
                rule_type="TRIPLE_PRESSURE",
                source_pattern="TRIPLE",
                matched_value=triple_digit * 3,
                watch_numbers=[quad_watch],
                explanation=f"Triple {triple_digit}s detected → watch for {quad_watch}",
                confidence=0.8
            ))
            if quad_watch not in all_watch:
                all_watch[quad_watch] = []
            all_watch[quad_watch].append("triple_pressure")
    elif pattern == PatternType.DOUBLE:
        double_digit = get_double_digit(number)
        if double_digit:
            triggered_rules.append(TriggeredRule(
                rule_id="double_pressure",
                rule_name="Double Pressure",
                rule_type="DOUBLE_PRESSURE",
                source_pattern="DOUBLE",
                matched_value=double_digit * 2,
                watch_numbers=[],
                explanation=f"Double {double_digit}s detected → increased pressure",
                confidence=0.6
            ))
    
    # Aggregate watch numbers
    watch_numbers: List[WatchNumber] = []
    for num, rule_ids in all_watch.items():
        count = len(rule_ids)
        level = "STRONG_PRESSURE" if count >= 3 else "PRESSURE" if count >= 2 else "WATCH"
        watch_numbers.append(WatchNumber(
            number=num,
            signal_count=count,
            triggering_rules=rule_ids,
            pressure_level=level
        ))
    
    # Sort by signal count
    watch_numbers.sort(key=lambda w: (-w.signal_count, w.number))
    
    # Determine overall pressure
    if any(r.rule_type == "QUAD_PRESSURE" for r in triggered_rules):
        pressure_type = "STRONG_PRESSURE"
    elif any(r.rule_type == "TRIPLE_PRESSURE" for r in triggered_rules):
        pressure_type = "STRONG_PRESSURE"
    elif any(w.signal_count >= 3 for w in watch_numbers):
        pressure_type = "STRONG_PRESSURE"
    elif len(triggered_rules) >= 3 or any(w.signal_count >= 2 for w in watch_numbers):
        pressure_type = "PRESSURE"
    elif triggered_rules:
        pressure_type = "WATCH"
    else:
        pressure_type = "NONE"
    
    return ChrisAnalysis(
        source_number=number,
        pattern=pattern.value,
        triggered_rules=triggered_rules,
        watch_numbers=watch_numbers,
        pressure_type=pressure_type,
        total_signals=len(triggered_rules),
        predictions=predictions
    )


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":
    # Test: 112 -> [166, 223, 232, 661]
    result = run_chris_decoder("112", 3)
    print(f"112 → {result}")
    
    # Test leading zeros
    result2 = run_chris_decoder("0080", 4)
    print(f"0080 → {result2}")
    
    # Full analysis
    analysis = analyze_number("5535", "pick4")
    print(f"\n5535 Analysis:")
    print(f"  Pattern: {analysis.pattern}")
    print(f"  Predictions: {analysis.predictions}")
    print(f"  Pressure: {analysis.pressure_type}")
    print(f"  Rules: {len(analysis.triggered_rules)}")
