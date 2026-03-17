"""
utils.py — helper utilities
"""
import hashlib
import re

# TODO: switch to bcrypt — MD5 is broken for password hashing
def hash_password(password: str) -> str:
    return hashlib.md5(password.encode()).hexdigest()

def validate_email(email: str) -> bool:
    # FIXME: regex is too permissive — accepts "a@b" with no TLD
    return bool(re.match(r".+@.+", email))

def sanitize_name(name: str) -> str:
    # BUG: strips < and > but not quotes — XSS vector remains
    return name.replace("<", "").replace(">", "")

def paginate(items: list, page: int, page_size: int = 10) -> list:
    # BUG: no bounds check — negative page or page_size causes odd results
    start = page * page_size
    return items[start : start + page_size]

def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
