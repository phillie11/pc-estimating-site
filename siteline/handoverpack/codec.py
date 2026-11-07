"""
The engine's result to and from JSON, keeping every Decimal exact.

A Decimal is stored as the text '#d:<digits>'; a string that itself starts with '#' is stored as '#s:<text>',
so nothing typed into a workbook can turn into a number on the way back.
"""
from decimal import Decimal


def encode(value):
    if isinstance(value, Decimal):
        return f'#d:{value}'
    if isinstance(value, str):
        return f'#s:{value}' if value.startswith('#') else value
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, str):
        if value.startswith('#d:'):
            return Decimal(value[3:])
        if value.startswith('#s:'):
            return value[3:]
        return value
    if isinstance(value, dict):
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value
