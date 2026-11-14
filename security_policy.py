"""Shared password policy for account creation and password updates."""

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
COMMON_PASSWORDS = frozenset({
    "password", "password123", "password1234", "password123456",
    "password12345678", "passwordpassword", "qwerty123", "qwerty123456",
    "qwertyuiop12", "letmein123", "letmein123456", "welcome123",
    "welcome123456", "admin123", "admin12345678", "changeme123",
    "changeme12345", "iloveyou123", "iloveyou12345", "abc123abc123",
})


def valid_password(password):
    if not isinstance(password, str) or not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        return False
    if not any(char.isalpha() for char in password) or not any(char.isdigit() for char in password):
        return False
    normalized = "".join(char for char in password.casefold() if char.isalnum())
    return normalized not in COMMON_PASSWORDS
