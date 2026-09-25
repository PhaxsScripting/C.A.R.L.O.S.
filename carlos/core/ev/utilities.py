"""Bounded local calculation and text helpers. Never evaluates Python code."""

from __future__ import annotations

import ast
import datetime as dt
import math
import operator
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def calculate(expression: str) -> dict:
    if len(expression) > 300:
        raise ValueError("Calculation is too long")
    normalized = expression.casefold().replace("×", "*").replace("÷", "/").replace("^", "**")
    for word, symbol in (
        ("multiplied by", "*"),
        ("divided by", "/"),
        ("plus", "+"),
        ("minus", "-"),
        ("times", "*"),
        ("modulo", "%"),
    ):
        normalized = re.sub(r"\b" + word + r"\b", symbol, normalized)
    normalized = re.sub(
        r"(\d+(?:\.\d+)?)\s*(?:percent|%)\s+of\s+(\d+(?:\.\d+)?)", r"(\1 / 100 * \2)", normalized
    )
    try:
        root = ast.parse(normalized.strip(), mode="eval")
    except (SyntaxError, ValueError) as error:
        raise ValueError("Use arithmetic, parentheses, sqrt, abs or round") from error
    if sum(1 for _ in ast.walk(root)) > 70:
        raise ValueError("Calculation is too complex")
    binary = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
    }

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            value = float(node.value)
        elif isinstance(node, ast.Name) and node.id in {"pi", "e"}:
            value = math.pi if node.id == "pi" else math.e
        elif isinstance(node, ast.UnaryOp) and type(node.op) in {ast.UAdd, ast.USub}:
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow):
                if abs(right) > 12:
                    raise ValueError("Exponent must be between -12 and 12")
                value = math.pow(left, right)
            elif type(node.op) in binary:
                value = binary[type(node.op)](left, right)
            else:
                raise ValueError("Unsupported arithmetic operator")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            args = [visit(item) for item in node.args]
            if node.func.id == "sqrt" and len(args) == 1:
                value = math.sqrt(args[0])
            elif node.func.id == "abs" and len(args) == 1:
                value = abs(args[0])
            elif node.func.id == "round" and len(args) in {1, 2}:
                digits = args[1] if len(args) == 2 else 0
                if digits != int(digits) or not -8 <= digits <= 8:
                    raise ValueError("Rounding precision must be an integer from -8 to 8")
                value = round(args[0], int(digits))
            else:
                raise ValueError("Unsupported calculation function")
        else:
            raise ValueError("Only arithmetic expressions are supported")
        if not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("Calculation result exceeds the supported range")
        return value

    try:
        value = visit(root.body)
    except (OverflowError, ZeroDivisionError) as error:
        raise ValueError("Calculation is outside the supported range or divides by zero") from error
    return {"value": value, "expression": expression, "message": f"The result is {value:.12g}."}


UNITS = {}


def _units(group, factor, *names):
    for name in names:
        UNITS[name] = (group, factor)


_units("length", 1, "m", "meter", "meters", "metre", "metres")
_units("length", 1000, "km", "kilometer", "kilometers", "kilometres")
_units("length", 0.01, "cm", "centimeter", "centimeters")
_units("length", 0.001, "mm", "millimeter", "millimeters")
_units("length", 0.0254, "in", "inch", "inches")
_units("length", 0.3048, "ft", "foot", "feet")
_units("length", 0.9144, "yd", "yard", "yards")
_units("length", 1609.344, "mi", "mile", "miles")
_units("mass", 1, "kg", "kilogram", "kilograms")
_units("mass", 0.001, "g", "gram", "grams")
_units("mass", 0.45359237, "lb", "lbs", "pound", "pounds")
_units("mass", 0.028349523125, "oz", "ounce", "ounces")
_units("time", 1, "s", "sec", "second", "seconds")
_units("time", 60, "min", "minute", "minutes")
_units("time", 3600, "hr", "hour", "hours")
_units("time", 86400, "day", "days")
_units("time", 604800, "week", "weeks")
_units("data", 1, "byte", "bytes")
_units("data", 1000, "kb", "kilobyte", "kilobytes")
_units("data", 1000**2, "mb", "megabyte", "megabytes")
_units("data", 1000**3, "gb", "gigabyte", "gigabytes")
_units("data", 1000**4, "tb", "terabyte", "terabytes")
_units("data", 1024, "kib", "kibibyte", "kibibytes")
_units("data", 1024**2, "mib", "mebibyte", "mebibytes")
_units("data", 1024**3, "gib", "gibibyte", "gibibytes")
_units("volume", 1, "l", "liter", "liters", "litre", "litres")
_units("volume", 0.001, "ml", "milliliter", "milliliters")
_units("volume", 3.785411784, "us gallon", "us gallons")
_units("speed", 1, "m/s", "meters per second")
_units("speed", 1 / 3.6, "km/h", "kph", "kilometers per hour")
_units("speed", 0.44704, "mph", "miles per hour")


def convert(value: float, source: str, target: str) -> dict:
    if not math.isfinite(value) or abs(value) > 1e15:
        raise ValueError("Conversion value is outside the supported range")
    source, target = source.strip().casefold(), target.strip().casefold()
    temperatures = {"c": "c", "celsius": "c", "f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k"}
    if source in temperatures and target in temperatures:
        src, dst = temperatures[source], temperatures[target]
        celsius = (value - 32) * 5 / 9 if src == "f" else value - 273.15 if src == "k" else value
        if celsius < -273.15:
            raise ValueError("Temperature is below absolute zero")
        result = celsius * 9 / 5 + 32 if dst == "f" else celsius + 273.15 if dst == "k" else celsius
    elif source in UNITS and target in UNITS and UNITS[source][0] == UNITS[target][0]:
        result = value * UNITS[source][1] / UNITS[target][1]
    else:
        raise ValueError(
            "Use supported units of the same type. Currency/live rates are not local unit conversions."
        )
    return {
        "value": result,
        "unit": target,
        "message": f"{value:g} {source} is {result:.10g} {target}.",
    }


def world_clock(zone: str) -> dict:
    aliases = {
        "new york": "America/New_York",
        "los angeles": "America/Los_Angeles",
        "chicago": "America/Chicago",
        "denver": "America/Denver",
        "london": "Europe/London",
        "paris": "Europe/Paris",
        "berlin": "Europe/Berlin",
        "tokyo": "Asia/Tokyo",
        "sydney": "Australia/Sydney",
        "utc": "UTC",
        "india": "Asia/Kolkata",
    }
    name = aliases.get(zone.strip().casefold(), zone.strip())
    try:
        now = dt.datetime.now(ZoneInfo(name))
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(
            "Use a city such as London or an exact IANA zone such as America/New_York"
        ) from error
    return {
        "timezone": name,
        "iso": now.isoformat(),
        "message": f"In {zone}, it's {now.strftime('%I:%M %p on %A, %B %d').lstrip('0')} {now.tzname()}.",
    }


def text_stats(content: str) -> dict:
    words = len(re.findall(r"\b[\w]+(?:['’-][\w]+)*\b", content))
    return {
        "words": words,
        "characters": len(content),
        "lines": len(content.splitlines()),
        "message": f"{words} words, {len(content)} characters, {len(content.splitlines())} lines.",
    }
